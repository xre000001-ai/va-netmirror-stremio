"""
VA × NetMirror — a CONFIGURABLE stream-only Stremio addon that merges two
native-HLS sources behind one configuration page:

  ▶️ VA Player   — streamdata.vaplayer.ru/api.php (imdb+type → 3 HLS
                   masters via nextgencloudfabric referer).  Ported from
                   the CinemaVIP addon (Node).
  🎬 NetMirror   — the full netmirror machinery (thash bootstrap, newtv +
                   embed engines, exit pool) imported as nm_core.
  📦 MovieBox    — the full moviebox machinery (aoneroom api, edge-cache
                   cookie scheme, exit pool) imported as mb_core; its /hls
                   playlist routes are served by this addon (text only).

The /configure page lets the user pick which sources feed the addon and
issues an encoded token; /{token}/manifest.json installs that selection.
No token = all sources ON.  Stream cards are DIRECT provider links —
zero addon bandwidth (tiny JSON + tiny playlists only).

Sections: 1 config · 2 token · 3 VA source · 4 merge · 5 config page ·
6 server.
"""

import base64
import gzip
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import requests

import nm_core  # the battle-tested netmirror machinery (import-safe)
import mb_core  # the battle-tested moviebox machinery (import-safe)


def _mb_boot():
    """Start mb_core's background machinery (pool refresh + trainer +
    token prewarm) exactly like its main() does, minus the HTTP server
    and the keepalive self-ping (this addon has its own port)."""
    try:
        mb_core._FREE_POOL_ON[0] = True
        threading.Thread(target=mb_core._free_pool_loop, daemon=True).start()
        threading.Thread(target=mb_core._pool_train_loop, daemon=True).start()
        threading.Thread(target=mb_core._boot_prewarm, daemon=True).start()
    except Exception as exc:
        print("mb background boot skipped: %s" % exc, flush=True)


_mb_boot()

# ------------------------------------------------------------------ 1 config
VERSION = "1.3.5"
BRAND = "VA × NetMirror"
PORT = int(os.environ.get("PORT", "7000"))
VN_PUBLIC_URL = os.environ.get("VN_PUBLIC_URL", "").rstrip("/")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

VA_API = "https://streamdata.vaplayer.ru/api.php"
VA_ORIGIN = "https://nextgencloudfabric.com"
VA_TIMEOUT = 12.0

DEFAULTS = {"va": True, "nm": True, "mb_hls": True, "mb_h5dl": True,
            "mb_h5play": True, "mb_web": True}

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA})

MANIFEST_BASE = {
    "id": "va.netmirror.stremio",
    "version": VERSION,
    "name": BRAND,
    "description": (
        "Three stream sources behind one configuration page: ▶️ VA Player "
        "(3 HLS servers) + 🎬 NetMirror (Netflix/Hotstar/Prime mirrors) + "
        "📦 MovieBox (multi-audio HLS). Open any movie or series from your "
        "catalogs — direct streams appear. Pick your sources on the "
        "configure page."),
    "logo": ("https://image.tmdb.org/t/p/w500/"
             "9O1Iy9odqMlHfBCXg7xw3fPnXnz.jpg"),
    "types": ["movie", "series"],
    "resources": ["stream"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "behaviorHints": {"configurable": True,
                      "configurationRequired": False},
}


def manifest_for(cfg):
    m = dict(MANIFEST_BASE)
    on = [n for n, key in (
              ("▶️ VA", "va"), ("🎬 NM", "nm"), ("📦 MB-HLS", "mb_hls"),
              ("📦 MB-DL", "mb_h5dl"), ("📦 MB-1080", "mb_h5play"),
              ("📦 MB-Web", "mb_web"))
          if cfg.get(key)]
    m["description"] = ("Sources: %s. Open any movie or series from your "
                        "catalogs — direct native-HLS streams appear. "
                        "Reconfigure any time." % (", ".join(on) or "none"))
    return m


# ------------------------------------------------------------------ 2 token
def encode_cfg(cfg):
    raw = json.dumps(cfg, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cfg(token):
    """v1.2.0: granular MovieBox selection — mb_hls (cookie DASH ladder,
    the original slow-but-everywhere path) and mb_file (H5 signed MP4s,
    fast + web-playable) are separate toggles.  Legacy tokens: 2-key
    (va/nm) and 3-key (va/nm/mb) both map mb -> BOTH mb toggles."""
    try:
        pad = "=" * (-len(token) % 4)
        cfg = json.loads(base64.urlsafe_b64decode(token + pad).decode())
        if not isinstance(cfg, dict):
            return None
        out = {"va": bool(cfg.get("va", True)),
               "nm": bool(cfg.get("nm", True))}
        if "mb_h5dl" in cfg or "mb_h5play" in cfg or "mb_web" in cfg:
            # v1.3.0 granular: each MovieBox API separately
            out["mb_hls"] = bool(cfg.get("mb_hls", True))
            out["mb_h5dl"] = bool(cfg.get("mb_h5dl", True))
            out["mb_h5play"] = bool(cfg.get("mb_h5play", True))
            out["mb_web"] = bool(cfg.get("mb_web", True))
        elif "mb_file" in cfg:
            # v1.2.0 token: mb_file covered all three file APIs
            f = bool(cfg.get("mb_file", True))
            out["mb_hls"] = bool(cfg.get("mb_hls", True))
            out["mb_h5dl"] = f
            out["mb_h5play"] = f
            out["mb_web"] = f
        else:
            mb = bool(cfg.get("mb", True))
            out["mb_hls"] = mb
            out["mb_h5dl"] = mb
            out["mb_h5play"] = mb
            out["mb_web"] = mb
        return out
    except Exception:
        return None


def cfg_from_path(path):
    """'/cfg-<token>/...' or no token -> (cfg, rest)."""
    if path.startswith("/cfg-"):
        token, _, rest = path[5:].partition("/")
        cfg = decode_cfg(token)
        if cfg:
            return cfg, "/" + rest if rest else "/"
        return dict(DEFAULTS), path
    return dict(DEFAULTS), path


# --------------------------------------------------------------- 3 VA source
_VA_CACHE = {}


def va_streams(identifier, media_type, season=None, episode=None):
    """▶️ VA Player: imdb id -> native HLS masters (positive-probed)."""
    key = (identifier, media_type, season, episode)
    hit = _VA_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    params = {"imdb": identifier,
              "type": "tv" if media_type == "series" else "movie"}
    referer = "%s/embed/%s/%s" % (VA_ORIGIN, params["type"], identifier)
    if media_type == "series" and season and episode:
        params["season"] = str(season)
        params["episode"] = str(episode)
        referer += "/%s/%s" % (season, episode)
    cards = []
    try:
        r = HTTP.get(VA_API, params=params, timeout=VA_TIMEOUT,
                     headers={"Referer": referer, "Origin": VA_ORIGIN})
        if r.status_code == 200:
            data = (r.json() or {}).get("data") or {}
            title = str(data.get("title") or "")
            if str((r.json() or {}).get("status_code")) == "200":
                for i, u in enumerate(data.get("stream_urls") or []):
                    ok = _va_probe(u)
                    cards.append({
                        "name": "▶️ VA · Server %d%s" % (i + 1,
                                                         "" if ok else " ·?"),
                        "title": "%s\nHLS · plays in the Stremio app" % title,
                        "url": u,
                        "behaviorHints": {"notWebReady": False},
                        "_va": True,
                    })
    except Exception:
        cards = []
    # keep positives 20 min; empty answers 3 min (retry sooner)
    _VA_CACHE[key] = (time.time() + (1200 if cards else 180), cards)
    return cards


def _va_probe(url):
    """Positive-only gate: the master must really answer HLS."""
    try:
        r = HTTP.get(url, timeout=10, stream=True,
                     headers={"Referer": VA_ORIGIN + "/"})
        ok = r.status_code == 200 and "#EXTM3U" in next(
            r.iter_content(64), b"").decode("utf-8", "ignore")
        r.close()
        return ok
    except Exception:
        return False


# ------------------------------------------------------------------ 4 merge
def build_streams(cfg, media_type, identifier, season=None, episode=None,
                  host_base=None):
    streams = []
    notes = []
    if cfg.get("va"):
        va = va_streams(identifier, media_type, season, episode)
        if va:
            streams.extend(va)
        else:
            notes.append("VA: no server answered")
    if cfg.get("nm"):
        try:
            nm = nm_core.streams_for_tt(
                media_type, identifier,
                int(season or 0), int(episode or 0))
            # netmirror cards are direct CDN links; rebase any relative
            # route defensively (none expected in v1.2.6, but cheap).
            for c in nm:
                u = c.get("url") or ""
                if u.startswith("/"):
                    c = dict(c)
                    base = host_base or VN_PUBLIC_URL or \
                        "https://va-netmirror.baby-beamup.club"
                    c["url"] = base + u
                streams.append(c)
        except Exception as exc:
            notes.append("NetMirror error: %s" % str(exc)[:60])
        if not nm_core.streams_for_tt_cached_lenient(
                media_type, identifier, season, episode):
            notes.append("NetMirror: nothing found")
    if any(cfg.get(k) for k in ("mb_hls", "mb_h5dl", "mb_h5play",
                                "mb_web")):
        try:
            mbres = mb_core.build_streams(
                media_type, identifier,
                int(season or 1), int(episode or 1))
            # moviebox build_streams returns {"streams": [...]} (its own
            # /stream route does res.get("streams")) — accept both shapes.
            mb = (mbres.get("streams") or []) \
                if isinstance(mbres, dict) else (mbres or [])
            # v1.3.0: per-API selection — mb_core tags every card
            tagkey = {"mobile-hls": "mb_hls", "h5-dl": "mb_h5dl",
                      "h5-play": "mb_h5play", "webmp4": "mb_web"}
            picked = []
            for c in mb:
                key = tagkey.get(c.get("_api"))
                if key is None:
                    key = "mb_hls" if "/hls/" in (c.get("url") or "") \
                        else "mb_h5dl"
                if cfg.get(key, True):
                    picked.append(c)
            mb = picked
            for c in mb:
                c = dict(c)
                nm_label = c.get("name") or ""
                # moviebox cards lead with ♧ — rebrand so the three
                # sources are tellable apart in the player UI
                if nm_label.startswith("♧"):
                    c["name"] = "📦 " + nm_label[1:].lstrip()
                u = c.get("url") or ""
                if u.startswith("/"):
                    base = host_base or VN_PUBLIC_URL or \
                        "https://va-netmirror.baby-beamup.club"
                    c["url"] = base + u
                streams.append(c)
            if not mb:
                notes.append("MovieBox: nothing found")
        except Exception as exc:
            notes.append("MovieBox error: %s" % str(exc)[:60])
    msg = ""
    if not streams:
        msg = " · ".join(notes) or "no source returned streams"
    return {"streams": streams[:20], "message": msg}


# fallback helper used above so a missing helper never breaks the merge
def _nm_lenient(kind, tt, s, e):
    try:
        return nm_core.streams_for_tt(kind, tt, int(s or 0), int(e or 0))
    except Exception:
        return []


setattr(nm_core, "streams_for_tt_cached_lenient",
        lambda kind, tt, s, e: _nm_lenient(kind, tt, s, e))

# ------------------------------------------------------------ 5 config page
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VA × NetMirror — configure</title>
<style>
body{background:#0b0f17;color:#e8ecf4;font-family:system-ui,sans-serif;
max-width:640px;margin:36px auto;padding:0 20px;line-height:1.55}
h1{font-size:26px} small{color:#8ea0b5}
.src{background:#111a27;border:1px solid #223047;border-radius:12px;
padding:14px 18px;margin:12px 0;display:flex;gap:12px;align-items:center}
.src b{font-size:16px} .src p{margin:2px 0 0;color:#9fb2c6;font-size:13px}
button{background:#e50914;color:#fff;border:0;border-radius:10px;
padding:13px 26px;font-weight:700;font-size:16px;cursor:pointer;margin-top:8px}
code{background:#151c2b;padding:2px 6px;border-radius:6px;font-size:13px}
</style></head><body>
<h1>🎬 VA × NetMirror <small>v__VER__</small></h1>
<p>Pick the sources you want, then install. You can come back and
reconfigure any time.</p>
<div class="src"><input type="checkbox" id="va" checked>
 <div><b>▶️ VA Player</b>
 <p>3 native-HLS servers per title · plays in the Stremio app</p></div></div>
<div class="src"><input type="checkbox" id="nm" checked>
 <div><b>🎬 NetMirror</b>
 <p>Netflix / Hotstar / Prime mirrors · multi-audio HLS + mp4, direct CDN</p></div></div>
<div class="src"><input type="checkbox" id="mb_hls" checked>
 <div><b>📦 MB · API-1 HLS ladder</b>
 <p>api3-6.aoneroom (multi-API race) · quality menu + dubs + SERIES · the original path, stays available</p></div></div>
<div class="src"><input type="checkbox" id="mb_h5dl" checked>
 <div><b>📦 MB · API-2 Download MP4 <small>(fast)</small></b>
 <p>H5 gateway per-dub 360p–1080p signed MP4s (bcdnw) — the app's own download files. Movies.</p></div></div>
<div class="src"><input type="checkbox" id="mb_h5play" checked>
 <div><b>📦 MB · API-3 Play-1080 MP4 <small>(fast)</small></b>
 <p>The exact 1080p MP4 the official web player streams (bcdnxw). Movies.</p></div></div>
<div class="src"><input type="checkbox" id="mb_web" checked>
 <div><b>📦 MB · API-4 Web MP4</b>
 <p>Web-catalog per-resolution signed MP4s, header-free. Movies.</p></div></div>
<button onclick="install()">Install in Stremio</button>
<p id="link" style="margin-top:14px"></p>
<p><small>Cards are direct provider links — the addon relays no media.
Configure = choose sources; the choice travels inside the install URL.</small></p>
<script>
function tok(){const c={va:document.getElementById('va').checked,
nm:document.getElementById('nm').checked,
mb_hls:document.getElementById('mb_hls').checked,
mb_h5dl:document.getElementById('mb_h5dl').checked,
mb_h5play:document.getElementById('mb_h5play').checked,
mb_web:document.getElementById('mb_web').checked};
let b=btoa(JSON.stringify(c)).replace(/=+$/,'');return b}
function install(){const t=tok();
location.href='/cfg-'+t+'/manifest.json'}
</script></body></html>""".replace("__VER__", VERSION)

LOGO = ("https://image.tmdb.org/t/p/w500/"
        "9O1Iy9odqMlHfBCXg7xw3fPnXnz.jpg")

# ------------------------------------------------------------------ 6 server
_START = time.time()
STATS = {"requests": 0, "streams": 0, "cards": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        head = {"Content-Type": ctype,
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=120",
                "Connection": "close"}
        if extra:
            head.update(extra)
        if "gzip" in (self.headers.get("Accept-Encoding") or "") \
                and len(body) > 500 and "text" in ctype:
            body = gzip.compress(body)
            head["Content-Encoding"] = "gzip"
        head["Content-Length"] = str(len(body))
        self.send_response(code)
        for k, v in head.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        STATS["requests"] += 1
        raw = urlparse(self.path)
        path = unquote(raw.path)
        try:
            if path in ("/", "/configure"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, {"ok": True, "addon": BRAND,
                                        "version": VERSION,
                                        "uptime_s": round(time.time() - _START, 1),
                                        "stats": STATS})
            cfg, rest = cfg_from_path(path)
            if rest == "/manifest.json":
                return self._send(200, manifest_for(cfg))
            if rest == "/debug/mb":
                info = {}
                try:
                    info["pool_all"] = len(mb_core._pool_all())
                    info["pool_healthy"] = len(mb_core._pool_healthy())
                    info["plat_ok"] = mb_core._plat_ok()
                    info["has_auth_token"] = mb_core._AUTH_TOKEN is not None
                    info["pool_free_on"] = bool(mb_core._FREE_POOL_ON[0])
                    info["reqlog_tail"] = list(mb_core._REQLOG)[-6:]
                except Exception as exc:
                    info["err"] = str(exc)[:140]
                return self._send(200, info)
            # moviebox quality-menu HLS layer (playlist TEXT only —
            # segments are absolute signed CDN URLs, zero media bytes)
            m = re.fullmatch(
                r"/hls/(\d{5,25})/(\d{1,3})/(\d{1,5})/(master|v\d+|a\d+)\.m3u8",
                rest)
            if m:
                body = mb_core._lazy_hls(m.group(1), int(m.group(2)),
                                         int(m.group(3)), m.group(4))
                if body is None:
                    return self._send(
                        404, "#EXTM3U\n#error no stream for this entry\n",
                        "application/vnd.apple.mpegurl")
                return self._send(200, body,
                                  "application/vnd.apple.mpegurl")
            fwd = self.headers.get("X-Forwarded-Host")
            if VN_PUBLIC_URL:
                # beamup's router forwards a TRUNCATED internal vhost in
                # X-Forwarded-Host (v1.9.13 lesson) — the configured
                # public URL wins
                host_base = VN_PUBLIC_URL
            elif fwd:
                host_base = "https://" + fwd.split(",")[0].strip()
            elif self.headers.get("Host"):
                host_base = "https://" + self.headers.get("Host")
            else:
                host_base = "http://127.0.0.1:%d" % PORT
            m = re.fullmatch(r"/stream/movie/(tt\d+)\.json", rest)
            if m:
                STATS["streams"] += 1
                out = build_streams(cfg, "movie", m.group(1),
                                    host_base=host_base)
                STATS["cards"] += len(out.get("streams") or [])
                return self._send(200, out)
            m = re.fullmatch(r"/stream/series/(tt\d+):(\d+):(\d+)\.json", rest)
            if m:
                STATS["streams"] += 1
                out = build_streams(cfg, "series", m.group(1),
                                    int(m.group(2)), int(m.group(3)),
                                    host_base=host_base)
                STATS["cards"] += len(out.get("streams") or [])
                return self._send(200, out)
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(500, {"error": "internal",
                                    "detail": str(exc)[:160]})


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("%s %s listening on :%d" % (BRAND, VERSION, PORT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
