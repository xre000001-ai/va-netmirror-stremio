#!/usr/bin/env python3
"""
NETMIRROR — Stremio addon (net52.cc family: Netflix / Hotstar+Disney+ /
Prime Video mirrors)
========================================================================

A STREAM-ONLY addon: browse your own catalogs (IMDb, Trakt, …), open any
title, and NETMIRROR streams appear.  No catalogs of its own.

HOW IT WORKS (mechanics from the open-source CloudStream extensions —
Sushan64/NetMirror-Extension and the CNC/phisher repos — re-verified
from scratch on 2026-09-10):

  SITE   net52.cc (Dooplay-style mirror family: net27/net52/net77)
         A fake-recaptcha POST to /verify.php mints a `t_hash_t` cookie
         (~15h) that unlocks the tiny JSON mobile API:
           /mobile/search.php?s={q}          -> [{id, t}]       (NOT ott-filtered)
           /mobile/post.php?id={id}          -> {title, year, episodes[],
                                                season[{s,id,ep}], …}
           /mobile/episodes.php?s=&series=   -> per-season episode list
  NEWTV  the player API base hides behind mobiledetect.* domains:
           GET {mobiledetect-domain}/checknewtv.php -> {token_hash} (b64)
           -> real base (e.g. tv.imgcdn.kim), then
           GET {base}/newtv/player.php?id={id}   headers: Ott: nf|hs|pv
           -> {video_link, referer}
         video_link is a Netflix-style ADAPTIVE master m3u8:
           • 480/720/1080 variants (s*.freecdn4.top, absolute urls)
           • up to 22 AUDIO dubs (Hindi/English/Tamil/…) as separate
             EXT-X-MEDIA playlists (s*.freecdn3.top)
           • WebVTT SUBTITLES (subscdn.top)
         The master itself answers 200 with NO referer; the variant /
         segment / audio / sub CDNs require Referer: https://net52.cc
         (Stremio sends it via behaviorHints.proxyHeaders.request).
         Segments are NOT IP-bound (cross-IP 200 via proxy verified) —
         so every video byte flows CDN -> player DIRECTLY.

  EMBED  net27.cc/api/embed-tmdb/{tmdb}[?type=tv&s=&e=]
  TMDB   (Referer videodownloader.site) -> {streams:[{url,resolution}]
         (signed mp4s), captions:[{name,url}]}.  The mp4 CDN 429s
         datacenter IPs but serves RESIDENTIAL/proxy IPs (206 verified)
         — cards are only listed after a proxy-verified 206.

Metadata: imdb id -> (name, year, tmdb) via the racing resolver
(cinemeta / TMDB find / IMDb suggest — fastest wins), cinemeta's
moviedb_id is the tmdb id primary source.  The public Stremio-default
TMDB key is used for the /find fallback (no user key required).

Everything is DIRECT — zero addon bandwidth, only KBs of JSON/HTML.
stdlib + requests only.   Run:  python3 addon.py [port]   (default 7860)
"""
import json
import base64
import html as html_mod
import os
import re
import secrets
import shutil
import sys
import time
import random
import threading
import traceback
import concurrent.futures as cf
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

import requests

# ----------------------------------------------------------------- config --
NET       = "https://net52.cc"
NET_VERIFY = NET + "/verify.php"
NET_SEARCH = NET + "/mobile/search.php"
NET_POST   = NET + "/mobile/post.php"
NET_EPS    = NET + "/mobile/episodes.php"
NET27     = "https://net27.cc"
EMBED_TMDB = NET27 + "/api/embed-tmdb/"
NET27_REF = "https://videodownloader.site/"

# player platforms (Ott header) -> card label.  Disney+ content ships on
# Hotstar (the CloudStream DisneyPlus provider reuses Ott "hs").
OTTS = (("nf", "NETFLIX"), ("hs", "HOTSTAR"), ("pv", "PRIME"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

VERSION   = "1.2.6"
ADDON_ID  = "netmirror"
ADDON_NAME = "NetMirror"

CINEMETA = "https://v3-cinemeta.strem.io"
TMDB_KEY = "1af06616dcbb28ff03088d87d63211f5"   # public addon default key

BUDGET      = 26.0    # seconds for a cold /stream (throttle/proxy bound); cached after
BG_BUDGET   = 55.0    # background Engine-A retry budget (v1.0.3 late-finish cache)    # seconds for a cold /stream (throttle/proxy bound); cached after
STREAM_TTL  = 3 * 3600
NAME_TTL    = 12 * 3600
NEG_TTL     = 5 * 60
POST_TTL    = 6 * 3600
THASH_TTL   = 12 * 3600
APIBASE_TTL = 24 * 3600

MOBILE_HDRS = {"Referer": NET + "/home",
               "X-Requested-With": "XMLHttpRequest",
               "User-Agent": UA}
# v1.0.7: several free proxy LISTS are merged (auto-rotation needs
# candidate diversity — one list's dead hour is another's good hour)
PROXY_LIST_URLS = [
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=display_proxies&proxy_format=protocolipport"
    "&format=text&timeout=5000",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
]

# the site trips "Too Many Requests" anti-abuse on bursts — mirror the
# CloudStream extension's NetmirrorThrottler (1200ms between backend
# requests).  Two separate lanes: the net52/newtv family, and net27.
class Throttle:
    def __init__(self, min_interval=1.2):
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self, deadline=None):
        while True:
            with self.lock:
                now = time.time()
                w = self.last + self.min_interval - now
                if w <= 0:
                    self.last = now
                    return True
            if deadline and time.time() + w >= deadline:
                return False
            time.sleep(min(w, 0.4))


TH_MAIN = Throttle(1.2)
TH_NET27 = Throttle(1.2)

LOG_RING = []
LOG_LOCK = threading.Lock()


def dbg(msg):
    with LOG_LOCK:
        LOG_RING.append(time.strftime("[%H:%M:%S") + (".%03d] " % int((time.time() % 1) * 1000)) + msg)
        del LOG_RING[:-200]


# ------------------------------------------------------------------ http ----
_S = requests.Session()
_S.headers.update(HDRS)


def _clamp(deadline, per=8.0):
    return max(0.5, min(per, deadline - time.time()))


# ------------------------------------------------------------ t_hash_t ------
# POST verify.php with a fake g-recaptcha-response -> t_hash_t cookie
_THASH = {"v": None, "ts": 0.0}
_THASH_FAIL = {"ts": 0.0}
_THASH_LOCK = threading.Lock()


# v1.0.7 "always auto-rotate": once this egress IP is observed 403'd
# by netmirror (datacenter blocks), every netmirror fetch goes through
# the rotating exit pool directly — no wasted direct attempts.
_NET_BLOCKED = {"v": False}


def _note_net_blocked(code):
    if code == 403:
        _NET_BLOCKED["v"] = True


def _mint_thash(deadline=None):
    try:
        r = _S.post(NET_VERIFY,
                    data={"g-recaptcha-response": str(secrets.token_hex(16))},
                    headers={"Origin": "https://net77.cc",
                             "Referer": "https://net77.cc/verify2",
                             "User-Agent": UA},
                    allow_redirects=False, timeout=10)
        _note_net_blocked(r.status_code)
        for c in (r.headers.get("Set-Cookie") or "").split(","):
            if c.strip().startswith("t_hash_t="):
                v = c.split(";", 1)[0].split("=", 1)[1]
                if v:
                    _THASH["v"] = v
                    _THASH["ts"] = time.time()
                    dbg("[thash] minted (%dB)" % len(v))
                    return v
    except Exception as ex:
        dbg("[thash] mint failed %r" % (ex,))
    return None


def thash(deadline=None):
    """direct mint with a failure cache (Render's datacenter IP is
    blocked: don't burn 10s retrying every request — remember 15min)."""
    with _THASH_LOCK:
        if _THASH["v"] and time.time() - _THASH["ts"] < THASH_TTL:
            return _THASH["v"]
        if time.time() - _THASH_FAIL["ts"] < 900:
            return None
        v = _mint_thash(deadline)
        if not v:
            _THASH_FAIL["ts"] = time.time()
        return v


# ------------------------------------------------- proxy exit fallback -----
# v1.0.1: from Render's IP the netmirror frontends block the t_hash_t
# mint (and a cookie used from the wrong IP makes search.php return a
# junk "Top Searches" feed with status="n" — the cookie is EXIT-BOUND:
# mint and use must go through the same proxy).  v1.0.2: ONE sticky
# exit proved too flaky on prod (a free proxy often survives a single
# request) — keep a POOL of pre-minted exits (moviebox trained-exit
# pattern): race several proxies for up to 3 (exit, cookie) pairs and
# rotate through them with 2-strike eviction.  The resolved video_link
# itself is NOT IP-bound (verified cross-IP), so the user still
# streams directly.
class ProxyPool:
    def __init__(self):
        self.lock = threading.Lock()
        self.proxies = []
        self.fetched = 0.0

    def _refresh_locked(self):
        fresh = []
        for u in PROXY_LIST_URLS:
            try:
                r = _S.get(u, timeout=10)
                if r.status_code == 200:
                    fresh += [l.strip() for l in r.text.splitlines()
                              if l.startswith("http://")]
            except Exception:
                continue
        if not fresh:
            return
        # merge-never-shrink (moviebox lesson: a bad sample must not
        # wipe a working pool)
        seen = set(self.proxies)
        merged = list(self.proxies) + [p for p in fresh if p not in seen]
        random.shuffle(merged)
        self.proxies = merged
        self.fetched = time.time()

    def candidates(self, n=16, fresh=False):
        with self.lock:
            if fresh or time.time() - self.fetched > 600 or not self.proxies:
                self._refresh_locked()
            out = list(self.proxies)
        # v1.0.8: benched (recently dead/blocked) proxies never make the
        # cut; historically-good ones come first (moviebox training)
        now = time.time()
        healthy = [p for p in out if _EXIT_BENCH.get(p, 0.0) <= now]
        pool = healthy or out
        good = [p for p in pool if (_EXIT_STATS.get(p) or {}).get("ok")]
        rest = [p for p in pool if p not in set(good)]
        random.shuffle(rest)
        random.shuffle(good)
        merged = good + rest if good else pool
        return merged[:n]

    def fetch(self, method, url, px, headers=None, cookies=None,
              data=None, deadline=None, timeout=8):
        t = timeout
        if deadline:
            t = min(timeout, deadline - time.time())
        if t <= 0.5:
            return None
        try:
            return requests.request(
                method, url, headers=headers, cookies=cookies, data=data,
                proxies={"http": px, "https": px},
                allow_redirects=False, timeout=t)
        except Exception:
            return None

    def check_206(self, url, deadline=None, referer=None):
        """True when ANY proxy pulls the first bytes of url (200/206) —
        used to verify the embed-tmdb mp4s (their CDN blocks datacenter
        IPs but serves residential/proxy ones)."""
        cands = self.candidates(18)

        def attempt(px):
            t = 12
            if deadline:
                t = min(t, deadline - time.time())
            if t <= 0.5:
                return None
            try:
                r = requests.get(url, headers={"User-Agent": UA,
                                               "Range": "bytes=0-99",
                                               "Referer": referer or NET27_REF},
                                 proxies={"http": px, "https": px}, timeout=t)
                if r.status_code in (200, 206) and len(r.content) > 0:
                    return True
            except Exception:
                pass
            return None

        ex = cf.ThreadPoolExecutor(max_workers=min(18, max(1, len(cands))))
        try:
            for res in ex.map(attempt, cands):
                if res:
                    return True
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        return False


POOL = ProxyPool()

_PROXY_EXITS = []                    # [{"px", "ck", "fails", "ts"}]
_PROXY_EXITS_LOCK = threading.Lock()


def _proxy_mint_once(px, deadline=None):
    """one verify.php mint attempt through px -> cookie or None.
    v1.0.3: minting alone is NOT proof — on prod all 3 minted exits
    then failed on search.  A freshly minted cookie is now PROBED with
    a cheap search (?s=a) and only exits whose probe comes back
    status "y" (valid feed, not the "Top Searches" junk) are kept.
    v1.0.8 (moviebox knowledge): every attempt TRAINS the pool — dead
    candidates are benched 10 min, blocked ones 30 min, successes get
    EWMA-latency records, so races stop re-rolling the same dead dice
    out of a 700-entry list."""
    if not _exit_healthy_px(px):
        return None
    _exit_busy_inc(px)
    t0 = time.time()
    try:
        r = POOL.fetch("POST", NET_VERIFY, px,
                       headers={"Origin": "https://net77.cc",
                                "Referer": "https://net77.cc/verify2",
                                "User-Agent": UA},
                       data={"g-recaptcha-response": secrets.token_hex(16)},
                       deadline=deadline, timeout=6)
    finally:
        _exit_busy_dec(px)
    ms = int((time.time() - t0) * 1000)
    if r is None:
        _note_exit_dead(px, blocked=False)
        return None
    if r.status_code == 403:
        _note_exit_dead(px, blocked=True)
        return None
    _note_exit_good(px, ms)
    ck = None
    for c in (r.headers.get("Set-Cookie") or "").split(","):
        if c.strip().startswith("t_hash_t="):
            v = c.split(";", 1)[0].split("=", 1)[1]
            if v:
                ck = v
                break
    if ck is None:
        return None
    p = POOL.fetch("GET", NET + "/mobile/search.php?s=a", px,
                   headers=MOBILE_HDRS,
                   cookies={"t_hash_t": ck, "ott": "nf", "hd": "on"},
                   deadline=deadline, timeout=5)
    if p is None or p.status_code != 200:
        return None
    try:
        if (p.json() or {}).get("status") != "y":
            return None
    except Exception:
        return None
    # v1.0.5b: an exit proven on net52 may still refuse HTTPS CONNECT
    # to the imgcdn API host (observed on prod) — prove BOTH hosts.
    q = POOL.fetch("GET", "https://tv.imgcdn.kim/newtv/player.php"
                   "?id=tt8175400", px,
                   headers={**NEWTV_HEADERS, "Ott": "nf"},
                   deadline=deadline, timeout=5)
    if q is None or q.status_code != 200:
        return None
    try:
        if "video_link" not in (q.json() or {}):
            return None
    except Exception:
        return None
    return ck


def _live_exits():
    now = time.time()
    return [e for e in _PROXY_EXITS
            if e["fails"] < 2 and now - e["ts"] < THASH_TTL]


_EXIT_RR = {"i": 0}

# ---- v1.0.8: TRAINED exit pool (knowledge ported from the moviebox
# addon v1.7/1.8, which survives the same free-proxy reality):
#   * every proxy (exit OR mint candidate) carries a training record
#     {ok, fail, lat(EWMA ms)}; picks prefer reliable-and-fast
#   * dead (conn-fail) proxies are benched 10 min, blocked (403) 30 min
#   * a good exit is sticky for 120s so a chain rides one exit
#   * in-flight busy counts spread parallel waves across exits
#   * learning dicts are bounded (stale members dropped on refresh)
_EXIT_STATS = {}                    # px -> {"ok", "fail", "lat"}
_EXIT_BENCH = {}                    # px -> benched-until ts
_STICKY_EXIT = [None, 0.0]          # last good px, sticky-until ts
_EXIT_BUSY = {}                     # px -> in-flight request count


def _exit_healthy_px(px, now=None):
    now = now or time.time()
    return _EXIT_BENCH.get(px, 0.0) <= now


def _exit_busy_inc(px):
    if px:
        _EXIT_BUSY[px] = _EXIT_BUSY.get(px, 0) + 1


def _exit_busy_dec(px):
    if px:
        n = _EXIT_BUSY.get(px, 0) - 1
        if n > 0:
            _EXIT_BUSY[px] = n
        else:
            _EXIT_BUSY.pop(px, None)


def _exit_score(px):
    st = _EXIT_STATS.get(px) or {}
    ok, fail = st.get("ok", 0), st.get("fail", 0)
    lat = st.get("lat") or 4000
    quality = (ok + 1.0) / (ok + fail + 2.0)     # Laplace-smoothed
    return quality * (4000.0 / max(lat, 250))


def _note_exit_good(px, ms=None):
    if not px:
        return
    if ms is None:
        ms = 2500
    st = _EXIT_STATS.setdefault(px, {"ok": 0, "fail": 0, "lat": None})
    st["ok"] += 1
    st["lat"] = ms if st["lat"] is None else int(0.6 * st["lat"] + 0.4 * ms)
    _EXIT_BENCH.pop(px, None)
    _STICKY_EXIT[0], _STICKY_EXIT[1] = px, time.time() + 120


def _note_exit_dead(px, blocked=False):
    """moviebox benching: dead 10 min, IP-blocked (403) 30 min."""
    if not px:
        return
    st = _EXIT_STATS.setdefault(px, {"ok": 0, "fail": 0, "lat": None})
    st["fail"] += 1
    dur = 1800 if blocked else 600
    _EXIT_BENCH[px] = max(_EXIT_BENCH.get(px, 0.0), time.time() + dur)
    if _STICKY_EXIT[0] == px:
        _STICKY_EXIT[0] = None


def _exit_train_bound():
    """keep the learning dicts bounded (moviebox v1.8.1: long-lived
    instances accumulated records for proxies that left ages ago)."""
    now = time.time()
    with _PROXY_EXITS_LOCK:
        keep = {e["px"] for e in _PROXY_EXITS}
    keep |= {u for u, t in _EXIT_BENCH.items() if t > now}
    for d in (_EXIT_STATS,):
        for k in [k for k in list(d) if k not in keep]:
            if d[k].get("fail", 0) > 0 and d[k].get("ok", 0) == 0:
                del d[k]              # pure-fail dead weight: drop now
    for k in [k for k, t in _EXIT_BENCH.items() if t <= now]:
        del _EXIT_BENCH[k]


def _next_exit():
    """TRAINED pick (moviebox _pool_pick): prefer the sticky exit, then
    the best-scored healthy live exit with <2 requests in flight (the
    parallel OTT wave spreads instead of piling on one exit), round-
    robin over the rest as a fairness fallback."""
    with _PROXY_EXITS_LOCK:
        live = _live_exits()
        if not live:
            return None
    now = time.time()
    sticky = _STICKY_EXIT[0] if _STICKY_EXIT[1] > now else None
    healthy = [e for e in live if _exit_healthy_px(e["px"], now)]
    pool = healthy or live
    if sticky:
        for e in pool:
            if e["px"] == sticky and _EXIT_BUSY.get(sticky, 0) < 2:
                return e
    ranked = sorted(pool, key=lambda e: _exit_score(e["px"]), reverse=True)
    e = next((x for x in ranked if _EXIT_BUSY.get(x["px"], 0) < 2),
             None)
    if e is None:
        e = pool[_EXIT_RR["i"] % len(pool)]
        _EXIT_RR["i"] += 1
    return e


def _via_exits(url, deadline=None, headers=None, want="json",
               detail=None):
    """fetch url through the live exits with rotation (each exit its
    own cookie spacing); returns parsed json / playlist text / None.
    Mints a fresh pool first when none is live (v1.0.4: without this
    the movie path died silently when the warm exits had just
    churned).  detail (optional dict) records the HTTP statuses seen
    so callers can tell a definitive server 404 from mere conn
    fails."""
    if detail is not None:
        detail["http"] = []
    minted = False
    for _round in range(2):
        if not _live_exits():
            if minted or not _mint_exits(deadline, want=2):
                return None
            minted = True
        for _ in range(max(1, len(_live_exits()))):
            if deadline and time.time() >= deadline - 0.5:
                return None
            ent = _next_exit()
            if not ent:
                break
            if not exit_wait(ent["px"], deadline):
                continue
            _exit_busy_inc(ent["px"])
            t0 = time.time()
            try:
                r = POOL.fetch("GET", url, ent["px"], headers=headers,
                               deadline=deadline, timeout=6)
            finally:
                _exit_busy_dec(ent["px"])
            ms = int((time.time() - t0) * 1000)
            if r is not None and detail is not None:
                detail["http"].append(r.status_code)
            if r is not None and r.status_code == 200:
                if want == "json":
                    try:
                        j = r.json()
                        _note_exit_good(ent["px"], ms)
                        return j
                    except Exception:
                        pass
                elif want == "m3u8":
                    if "#EXTM3U" in (r.text or "")[:64]:
                        _note_exit_good(ent["px"], ms)
                        return r.text
                else:
                    _note_exit_good(ent["px"], ms)
                    return r
            _note_exit_fail(ent["px"])
            _note_exit_dead(ent["px"], blocked=(r is not None
                                                and r.status_code == 403))
            dbg("[net] exit %s -> %s (%s)" % (
                ent["px"],
                "conn fail" if r is None else "HTTP %s" % r.status_code,
                url.split("?")[0][-24:]))
        if _round == 0 and _live_exits() and \
                (not deadline or time.time() < deadline - 8):
            break               # exits remain live; retrying won't help
    return None


_MINTING = {"active": False}
_MINT_COND = threading.Condition(_PROXY_EXITS_LOCK)


def _mint_exits(deadline=None, want=3):
    """single-flight: race free proxies until `want` (exit, cookie)
    pairs are minted (or candidates exhausted).  v1.0.7: the race runs
    WITHOUT holding the pool lock (the keeper's long race used to
    block request threads on the lock for its whole duration —
    45s+ stalls) — the lock is only held for the quick live-check and
    the final merge; concurrent callers wait on a condition instead of
    starting their own race."""
    with _MINT_COND:
        while True:
            live = _live_exits()
            if live:
                return live
            if not _MINTING["active"]:
                _MINTING["active"] = True
                break
            # someone else is minting — wait for them (bounded)
            if deadline and time.time() >= deadline - 1:
                return []
            _MINT_COND.wait(timeout=3)
    cands = POOL.candidates(32, fresh=True)
    hits = []

    def attempt(px):
        return px, _proxy_mint_once(px, deadline)

    # NOTE: no with-block — shutdown(wait=True) would block the
    # request until every one of the 32 candidate attempts (mint
    # up to 6s + probe up to 5s each) finishes, long past the
    # deadline (v1.0.4: this is what pushed cold requests to ~30s).
    ex = cf.ThreadPoolExecutor(min(16, max(1, len(cands))))
    try:
        futs = [ex.submit(attempt, px) for px in cands]
        for f in cf.as_completed(futs):
            if deadline and time.time() >= deadline:
                break
            try:
                px, ck = f.result()
            except Exception:
                continue
            if ck and all(h["px"] != px for h in hits):
                hits.append({"px": px, "ck": ck, "fails": 0,
                             "ts": time.time()})
                with _MINT_COND:          # publish hits as they land
                    for h in hits:
                        if all(e["px"] != h["px"] for e in _PROXY_EXITS):
                            _PROXY_EXITS.insert(0, h)
                    _MINT_COND.notify_all()
                if len(hits) >= want:
                    break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        with _MINT_COND:
            _MINTING["active"] = False
            _MINT_COND.notify_all()
    dbg("[thash] %d proxy exits minted" % len(hits))
    with _MINT_COND:
        return _live_exits() or hits


def _proxy_exit_cookie(deadline=None):
    """(cookie, px) of the first live exit (minting if none) — for
    fallbacks that only need a WORKING exit (player.php/checknewtv are
    cookie-free; the cookie is harmless there)."""
    with _PROXY_EXITS_LOCK:
        live = _live_exits()
        if live:
            return live[0]["ck"], live[0]["px"]
    hits = _mint_exits(deadline)
    if hits:
        return hits[0]["ck"], hits[0]["px"]
    return None, None


def _note_exit_fail(px):
    with _PROXY_EXITS_LOCK:
        for e in _PROXY_EXITS:
            if e["px"] == px:
                e["fails"] += 1
                if e["fails"] >= 2:
                    _PROXY_EXITS.remove(e)
                break


_EXIT_THROTTLE = {}
_EXIT_THROTTLE_LOCK = threading.Lock()


def exit_wait(px, deadline=None):
    """1.2s spacing PER EXIT — the site's anti-abuse is per-IP and each
    exit is a different IP, so parallel work through different exits is
    safe (v1.0.2: the old GLOBAL throttle serialized the whole proxy
    chain and blew the budget)."""
    while True:
        with _EXIT_THROTTLE_LOCK:
            now = time.time()
            w = _EXIT_THROTTLE.get(px, 0) + 1.2 - now
            if w <= 0:
                _EXIT_THROTTLE[px] = now
                return True
        if deadline and time.time() + w >= deadline:
            return False
        time.sleep(min(w, 0.3))


def net_get(url, deadline=None):
    """GET a net52 mobile-api url -> parsed json or None.  Direct (with
    the direct cookie) first; on blocked IPs the pre-minted proxy exits
    (cookie is exit-bound — each request reuses ITS exit), rotating on
    failure and re-minting only when the pool is exhausted."""
    ck = None if _NET_BLOCKED["v"] else thash(deadline)
    if ck:
        if not TH_MAIN.wait(deadline):
            return None
        try:
            r = _S.get(url, headers=MOBILE_HDRS,
                       cookies={"t_hash_t": ck, "ott": "nf", "hd": "on"},
                       timeout=_clamp(deadline, 12) if deadline else 12)
            _note_net_blocked(r.status_code)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    for _round in range(2):
        if not _live_exits() and not _mint_exits(deadline, want=2):
            return None
        for _ in range(max(1, len(_live_exits()))):
            if deadline and time.time() >= deadline:
                return None
            ent = _next_exit()          # trained pick (sticky first)
            if not ent:
                break
            if not exit_wait(ent["px"], deadline):
                continue
            _exit_busy_inc(ent["px"])
            t0 = time.time()
            try:
                r = POOL.fetch("GET", url, ent["px"], headers=MOBILE_HDRS,
                               cookies={"t_hash_t": ent["ck"], "ott": "nf",
                                        "hd": "on"},
                               deadline=deadline, timeout=6)
            finally:
                _exit_busy_dec(ent["px"])
            ms = int((time.time() - t0) * 1000)
            if r is not None and r.status_code == 200:
                try:
                    j = r.json()
                    _note_exit_good(ent["px"], ms)
                    return j
                except Exception as ex:
                    dbg("[net] exit %s -> bad json (%s)" % (ent["px"], ex))
                    _note_exit_fail(ent["px"])
                    continue
            why = "conn fail" if r is None else "HTTP %s %r" % (
                r.status_code, (r.text or "")[:40])
            _note_exit_fail(ent["px"])
            _note_exit_dead(ent["px"], blocked=(r is not None
                                                and r.status_code == 403))
            dbg("[net] exit %s -> %s" % (ent["px"], why))
    return None


# compat alias for the v1.0.1 name (used by apibase/player fallbacks)
proxy_thash = _proxy_exit_cookie


# ------------------------------------------------------------ api base ------
# mobiledetect.* domains -> checknewtv.php -> {token_hash} (b64) -> real base
_MOBILEDETECT = [
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLmNvbQ==",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3QuYXBw",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0LmFydA==",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3QuY2M=",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3QuY2xpY2s=",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0Lmluaw==",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0LmxpdmU=",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0LnBybw==",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0LnNob3A=",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0LnNpdGU=",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0LnNwYWNl",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0LnN0b3Jl",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0LnZpcA==",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0Lndpa2k=",
    "aHR0cHM6Ly9tb2JpZGV0ZWN0Lnh5eg==",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLmFydA==",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLmNj",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLmluZm8=",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLmluaw==",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLmxpdmU=",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLnBybw==",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLnN0b3Jl",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLnRvcA==",
    "aHR0cHM6Ly9tb2JpbGVkZXRlY3RzLnh5eg==",
]
NEWTV_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache", "Expires": "0",
    "X-Requested-With": "NetmirrorNewTV v1.0",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) "
                   "Gecko/20100101 Firefox/136.0 /OS.GatuNewTV v1.0"),
    "Accept": "application/json, text/plain, */*",
    "Usertoken": "",
}
_APIBASE = {"v": None, "ts": 0.0}
_APIBASE_LOCK = threading.Lock()


def _resolve_apibase(deadline=None):
    # v1.0.3 fast path: probe the current NewTV CDN base DIRECTLY —
    # v1.0.4: on Render EVERYTHING netmirror (net52/net77/imgcdn)
    # 403s directly, so once that's observed (flag) we skip straight
    # to the proxy resolution instead of burning seconds on doomed
    # direct attempts.
    if not _NET_BLOCKED["v"]:
        try:
            r = _S.get("https://tv.imgcdn.kim/newtv/player.php"
                       "?id=tt8175400",
                       headers={**NEWTV_HEADERS, "Ott": "nf"},
                       timeout=6, allow_redirects=False)
            if r.status_code == 200 and \
                    "status" in (r.json() or {}):
                _APIBASE["v"] = "https://tv.imgcdn.kim"
                _APIBASE["ts"] = time.time()
                dbg("[apibase] https://tv.imgcdn.kim (direct guess)")
                return "https://tv.imgcdn.kim"
            if r.status_code == 403:
                _NET_BLOCKED["v"] = True
                dbg("[apibase] direct 403 — always-rotate mode on")
        except Exception:
            pass
        for enc in _MOBILEDETECT[:4]:
            if deadline and time.time() >= deadline:
                return None
            try:
                base = base64.b64decode(enc).decode().rstrip("/")
                r = requests.get(base + "/checknewtv.php",
                                 headers=NEWTV_HEADERS, timeout=8)
                if r.status_code == 403:
                    _NET_BLOCKED["v"] = True
                    break
                th = (r.json() or {}).get("token_hash")
                if th:
                    v = base64.b64decode(th).decode().rstrip("/")
                    if v.startswith("http"):
                        _APIBASE["v"] = v
                        _APIBASE["ts"] = time.time()
                        dbg("[apibase] %s (via %s)" % (v, base))
                        return v
            except Exception:
                continue
    # blocked-IP fallback — resolve through a minted proxy exit
    for enc in _MOBILEDETECT[:3]:
        if deadline and time.time() >= deadline:
            return None
        _pck, px = proxy_thash(deadline)
        if not px:
            return None
        r = POOL.fetch("GET", base64.b64decode(enc).decode().rstrip("/")
                       + "/checknewtv.php", px, headers=NEWTV_HEADERS,
                       deadline=deadline, timeout=8)
        if r is None or r.status_code != 200:
            continue
        try:
            th = (r.json() or {}).get("token_hash")
            if th:
                v = base64.b64decode(th).decode().rstrip("/")
                if v.startswith("http"):
                    _APIBASE["v"] = v
                    _APIBASE["ts"] = time.time()
                    dbg("[apibase] %s (proxy via %s)" % (v, enc[:12]))
                    return v
        except Exception:
            continue
    return None


def apibase(deadline=None):
    with _APIBASE_LOCK:
        if _APIBASE["v"] and time.time() - _APIBASE["ts"] < APIBASE_TTL:
            return _APIBASE["v"]
        return _resolve_apibase(deadline)


# -------------------------------------------------------------- caches ------
class TTLCache:
    def __init__(self, ttl, maxsize=4096):
        self.ttl, self.maxsize = ttl, maxsize
        self.d = {}
        self.lock = threading.Lock()

    def get(self, k):
        with self.lock:
            v = self.d.get(k)
            if v and time.time() - v[1] < self.ttl:
                return v[0]
            return None

    def put(self, k, v):
        with self.lock:
            if len(self.d) > self.maxsize:
                self.d = {a: b for a, b in
                          sorted(self.d.items(), key=lambda x: -x[1][1])[:self.maxsize // 2]}
            self.d[k] = (v, time.time())

    def delete(self, k):
        with self.lock:
            self.d.pop(k, None)


TTNAMES = TTLCache(NAME_TTL)     # tt -> (name, year) or False
TTMD    = TTLCache(NAME_TTL)     # (kind, tt) -> tmdb id or False
POSTS   = TTLCache(POST_TTL)     # tt -> (cid, post dict) or False
EPIDS   = TTLCache(POST_TTL)     # (cid, s, e) -> episode id or False
STREAMS = TTLCache(STREAM_TTL)   # key -> [stream] or False
STREAMS_LOCK = threading.Lock()   # v1.2.6: bg-merge safety
TTNEG   = TTLCache(NEG_TTL)      # key -> True


# ------------------------------------------------- racing metadata ----------
_TTMETA_LOCK = threading.Lock()


def _cinemeta_name(kind, tt):
    try:
        r = requests.get(f"{CINEMETA}/meta/{kind}/{tt}.json",
                         headers=HDRS, timeout=6)
        m = (r.json().get("meta") or {}) if r.status_code == 200 else {}
        if m.get("name"):
            year = ""
            mm = re.match(r"^(\d{4})", str(m.get("releaseInfo") or ""))
            if mm:
                year = mm.group(1)
            return (m["name"], year,
                    str(m["moviedb_id"]) if m.get("moviedb_id") else None)
    except Exception:
        pass
    return None


def _tmdb_find_name(kind, tt):
    try:
        r = _S.get(f"https://api.themoviedb.org/3/find/{tt}",
                   params={"api_key": TMDB_KEY, "external_source": "imdb_id"},
                   headers=HDRS, timeout=6)
        j = r.json()
        res = ((j.get("tv_results") or [{}]) if kind == "series"
               else (j.get("movie_results") or [{}]))[0]
        name = res.get("title") or res.get("name")
        if name:
            year = str(res.get("release_date") or
                       res.get("first_air_date") or "")[:4]
            return (name, year, str(res["id"]) if res.get("id") else None)
    except Exception:
        pass
    return None


def _imdb_suggest_name(tt):
    try:
        r = _S.get(f"https://v2.sg.media-imdb.com/suggestion/"
                   f"{tt[0]}/{tt}.json", timeout=6)
        for d in (r.json().get("d") or []):
            if d.get("id") == tt and d.get("l"):
                return (d["l"], str(d.get("y") or ""))
    except Exception:
        pass
    return None


def cinemeta_name(kind, tt):
    """fastest of cinemeta / tmdb / imdb-suggest wins; tmdb id recorded
    opportunistically (single-flight per tt)."""
    hit = TTNAMES.get(tt)
    if hit is not None:
        return hit or None
    with _TTMETA_LOCK:
        hit = TTNAMES.get(tt)
        if hit is not None:
            return hit or None
        val = None
        ex = cf.ThreadPoolExecutor(max_workers=3)
        try:
            futs = [ex.submit(_cinemeta_name, kind, tt),
                    ex.submit(_tmdb_find_name, kind, tt),
                    ex.submit(_imdb_suggest_name, tt)]
            for f in cf.as_completed(futs):
                try:
                    res = f.result()
                except Exception:
                    res = None
                if not res:
                    continue
                if len(res) == 3 and res[2]:
                    TTMD.put((kind, tt), res[2])
                val = (res[0], res[1] or "")
                break
        except Exception:
            val = None
        finally:
            ex.shutdown(wait=False)
        TTNAMES.put(tt, val or False)
        return val


def tt_tmdb(kind, tt):
    """imdb id -> tmdb id (cinemeta moviedb_id first, TMDB find back)."""
    hit = TTMD.get((kind, tt))
    if hit is not None:
        return hit or None
    cinemeta_name(kind, tt)                # single-flight; fills TTMD
    hit = TTMD.get((kind, tt))
    if hit:
        return hit
    tid = None
    r = _tmdb_find_name(kind, tt)
    if r:
        tid = r[2]
    TTMD.put((kind, tt), tid or False)
    return tid


# ------------------------------------------------------------ matching ------
_VARIANT_WORDS = re.compile(
    r"\b(hindi|english|dubbed|dub|dual\s*audio|multi\s*audio|subtitled|"
    r"subtitles|subbed|web\s*dl|webdl|hdrip|amzn|complete|season|"
    r"full\s*movie)\b", re.I)


def _title_key(s):
    s = html_mod.unescape(s or "").lower().replace("&", " and ")
    s = _VARIANT_WORDS.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _year_ok(card_year, meta_year):
    if not card_year or not meta_year:
        return True
    try:
        return abs(int(card_year) - int(meta_year)) <= 1
    except ValueError:
        return True


def name_candidates(kind, tt, primary):
    """primary name first, then the other providers' name forms (they
    disagree: cinemeta 'Mirzapur' vs imdb 'Mirzapur: The Movie' — the
    site card can use either form)."""
    cands = [(primary[0], primary[1] if len(primary) > 1 else "")]
    seen = {primary[0].lower()}
    for fn, args in ((_cinemeta_name, (kind, tt)),
                     (_tmdb_find_name, (kind, tt)),
                     (_imdb_suggest_name, (tt,))):
        try:
            r = fn(*args)
        except Exception:
            r = None
        if r and r[0] and r[0].lower() not in seen:
            seen.add(r[0].lower())
            cands.append((r[0], (r[1] if len(r) > 1 else "") or ""))
    return cands


# --------------------------------------------------------- site search ------
def search_site(query, deadline=None):
    url = NET_SEARCH + "?s=" + quote(query) + "&t=%d" % int(time.time())
    j = net_get(url, deadline)
    if not j:
        return []
    # a cookie/IP mismatch serves a junk "Top Searches" feed with
    # status "n" — only a real answer has status "y"
    if j.get("status") == "y":
        return [(x.get("id"), x.get("t") or "")
                for x in (j.get("searchResult") or []) if x.get("id")]
    return []


def post_data(cid, deadline=None):
    """mobile/post.php -> post dict (throttled)."""
    url = NET_POST + "?id=" + quote(str(cid)) + "&t=%d" % int(time.time())
    j = net_get(url, deadline)
    if j and j.get("title"):
        return j
    return None


def episodes_for_season(cid, sid, deadline=None):
    """mobile/episodes.php (paged) -> [{id,s,ep,t}] (throttled)."""
    out, page = [], 1
    while True:
        url = (NET_EPS + "?s=" + quote(str(sid)) + "&series=" + quote(str(cid))
               + "&t=%d&page=%d" % (int(time.time()), page))
        j = net_get(url, deadline)
        if not j:
            break
        out.extend(j.get("episodes") or [])
        if not j.get("nextPageShow"):
            break
        page += 1
        if page > 12:
            break
    return out


def find_post(name, year, deadline=None):
    """(cid, post) for the site title matching name+year.  Strict title
    key first, then containment (name-form divergence: 'Mirzapur' vs
    'Mirzapur The Movie') — always confirmed against the post's OWN
    title+year (soft match needs the year to be right)."""
    cands = search_site(name, deadline)
    want = _title_key(name)
    if not want:
        return None, None
    strict = [c for c in cands if _title_key(c[1]) == want]
    soft = [c for c in cands
            if c not in strict and
            (want in _title_key(c[1]) or _title_key(c[1]) in want)]
    for cid, _t in strict + soft[:2]:
        if deadline and time.time() >= deadline:
            break
        p = post_data(cid, deadline)
        if not p:
            continue
        pk = _title_key(p.get("title") or "")
        if (pk == want or (pk and (want in pk or pk in want))) and \
                _year_ok(p.get("year"), year):
            return cid, p
    return None, None


def episode_id(cid, post, s, e, deadline=None):
    """episode id for (s,e) — from post.episodes (current season) or the
    season list via episodes.php."""
    for ep in (post.get("episodes") or []):
        try:
            if int(str(ep.get("s", "0")).replace("S", "")) == s and \
                    int(str(ep.get("ep", "0")).replace("E", "")) == e:
                return ep.get("id")
        except (TypeError, ValueError):
            continue
    # find the season id (post.season: [{s, id, ep}])
    for sn in (post.get("season") or []):
        try:
            if int(sn.get("s") or 0) == s:
                for ep in episodes_for_season(cid, sn.get("id"), deadline):
                    try:
                        if int(str(ep.get("s", "0")).replace("S", "")) == s \
                                and int(str(ep.get("ep", "0")).replace("E", "")) == e:
                            return ep.get("id")
                    except (TypeError, ValueError):
                        continue
        except (TypeError, ValueError):
            continue
    return None


# ------------------------------------------------------------ newtv ---------
def newtv_player(eid, ott, deadline=None):
    """player.php -> (video_link, referer) or (None, None) (throttled;
    direct first, sticky-proxy exit as blocked-IP fallback)."""
    base = apibase(deadline)
    if not base:
        return None, None
    url = base + "/newtv/player.php?id=" + quote(str(eid))
    hdrs = {**NEWTV_HEADERS, "Ott": ott}
    if _NET_BLOCKED["v"]:
        j = _via_exits(url, deadline, headers=hdrs, want="json")
        if j:
            vl = (j.get("video_link") or "").strip()
            if vl.startswith("http"):
                return vl, (j.get("referer") or NET).strip()
        return None, None
    if not TH_MAIN.wait(deadline):
        return None, None
    try:
        r = _S.get(url, headers=hdrs,
                   timeout=_clamp(deadline, 12) if deadline else 12)
        _note_net_blocked(r.status_code)
        if r.status_code == 200:
            j = r.json()
            vl = (j.get("video_link") or "").strip()
            if vl.startswith("http"):
                return vl, (j.get("referer") or NET).strip()
    except Exception:
        pass
    # blocked-IP fallback (v1.0.4: exit rotation, not single-exit)
    j = _via_exits(url, deadline, headers=hdrs, want="json")
    if j:
        vl = (j.get("video_link") or "").strip()
        if vl.startswith("http"):
            return vl, (j.get("referer") or NET).strip()
    return None, None


def _master_info(url, deadline=None):
    """(qualities, audio_langs, has_subs, playlist_text) from a master
    playlist (direct, exit-rotation fallback for blocked IPs).
    v1.0.7: on a blocked IP go straight through the rotating pool."""
    if _NET_BLOCKED["v"]:
        txt = _via_exits(url, deadline, headers={"User-Agent": UA},
                         want="m3u8")
        if txt is None:
            return None
    else:
        txt = None
        try:
            r = _S.get(url, headers={"User-Agent": UA},
                       timeout=_clamp(deadline, 10) if deadline else 10)
            _note_net_blocked(r.status_code)
            if r.status_code == 200 and "#EXTM3U" in r.text[:64]:
                txt = r.text
        except Exception:
            pass
        if txt is None:
            txt = _via_exits(url, deadline, headers={"User-Agent": UA},
                             want="m3u8")
        if txt is None:
            return None
    q = sorted({int(h) for _, h in
                re.findall(r"RESOLUTION=(\d+)x(\d+)", txt)},
               reverse=True)
    q = ["%dp" % h for h in q if h in (2160, 1440, 1080, 720, 480, 360)]
    langs = re.findall(r'TYPE=AUDIO[^\n]*?LANGUAGE="([a-z]{3})"',
                       txt)
    langs = list(dict.fromkeys(langs))
    subs = "TYPE=SUBTITLES" in txt
    return q, langs, subs, txt


def _ql_label(qtxt):
    """v1.2.0: HIGHEST resolution only (user spec v2)."""
    hs = [int(x) for x in
          re.findall(r"(2160|1440|1080|960|720|576|480|360)", qtxt or "")]
    if not hs:
        q = (qtxt or "").strip().upper()
        return q if q in ("UHD", "FHD", "HD", "SD", "4K") else "HLS"
    h = max(hs)
    if h >= 2160:
        return "UHD 2160p"
    if h >= 1080:
        return "FHD 1080p"
    return ("HD %dp" if h >= 720 else "SD %dp") % h

def _fmt_card(ql, title, ep, year, audio, prov, subs=0, extra=""):
    """v1.2.0 card spec v2:
    ♧ QUALITY ✹ title / ◫ ep / ◈ WEB-DL / ◈ audio langs /
    ⌗ NetMirror / ⌬ server ◴ year ⟡ subs."""
    t4 = ["⌬ %s" % prov]
    y = str(year or "")[:4]
    if y.isdigit():
        t4.append("◴ %s" % y)
    if subs:
        t4.append("⟡ %d SUB" % subs)
    lines = [ep, "◈ WEB-DL"]
    if audio:
        # v1.2.0: 'Hindi/English' -> 'Hindi · English' on the glass line
        lines.append("◈ %s" % " · ".join(x for x in audio.split("/") if x))
    lines += ["⌗ %s" % ADDON_NAME, "  ".join(t4)]
    return ("♧ %s  ✹ %s" % (ql, title),
            "\n".join(x for x in lines if x.strip()))

_LANG = {"hin": "Hindi", "eng": "English", "tam": "Tamil", "tel": "Telugu",
         "ben": "Bengali", "mar": "Marathi", "pun": "Punjabi", "guj": "Gujarati",
         "kan": "Kannada", "mal": "Malayalam", "spa": "Spanish", "fra": "French",
         "deu": "German", "por": "Portuguese", "ita": "Italian", "rus": "Russian",
         "ara": "Arabic", "jpn": "Japanese", "kor": "Korean", "tha": "Thai",
         "vie": "Vietnamese", "ind": "Indonesian", "fil": "Filipino",
         "tur": "Turkish", "pol": "Polish", "nld": "Dutch", "ukr": "Ukrainian",
         "ces": "Czech", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
         "fin": "Finnish", "heb": "Hebrew", "ell": "Greek", "hun": "Hungarian",
         "ron": "Romanian", "zho": "Chinese"}


def _variant_alive(master_url, master_txt, deadline=None):
    """v1.0.5: netmirror's own CDN sometimes serves masters whose
    variant playlists 404 for EVERYONE (verified cross-IP during their
    outage) — an honest HLS card must have at least one live variant.
    Returns True/False (a dead master's variants all share one URL).
    v1.0.6: also memoizes the outage so Engine A skips its whole
    (expensive, exit-churning) chain for the next few minutes."""
    lines = [l for l in (master_txt or "").splitlines()
             if l and not l.startswith("#")]
    if not lines:
        _SITE_CDN_DOWN["ts"] = time.time()
        return False
    v = lines[0]
    if not v.startswith("http"):
        v = master_url.rsplit("/", 1)[0] + "/" + v
    try:
        r = _S.get(v, headers={"User-Agent": UA},
                   timeout=_clamp(deadline, 4) if deadline else 4)
        if r.status_code == 200 and "#EXTM3U" in (r.text or "")[:32]:
            return True
    except Exception:
        pass
    d = {}
    ok = _via_exits(v, deadline, headers={"User-Agent": UA},
                    want="m3u8", detail=d) is not None
    if not ok:
        # memoize the outage ONLY when a server actually answered with
        # a definitive non-200 (all-conn-fails mean OUR exits were
        # dead, not the site — no memo then)
        if d.get("http"):
            _SITE_CDN_DOWN["ts"] = time.time()
    return ok


def newtv_cards(eid, title, s, e, deadline=None):
    """one DIRECT card per OTT that has the title (adaptive master with
    multi-audio + subs; player sends Referer via proxyHeaders).
    v1.0.2: the three OTT lookups run IN PARALLEL (each is an
    independent player.php call; per-exit throttle keeps per-IP
    spacing — the sequential loop cost ~8s on the proxy path)."""
    def _ott_task(ott):
        vl, ref = newtv_player(eid, ott, deadline)
        if not vl:
            return None
        info = _master_info(vl, deadline)
        if info is None:                      # honest card: must be m3u8
            dbg("[newtv] %s %s: master not m3u8" % (ott, eid))
            return None
        if not _variant_alive(vl, info[3], deadline):
            dbg("[newtv] %s %s: variants dead (site CDN down?) — "
                "skipping card" % (ott, eid))
            return None
        return (ott, vl, info)

    by_ott = {}
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_ott_task, ott): ott for ott, _l in OTTS}
        for f in cf.as_completed(futs):
            try:
                res = f.result()
            except Exception:
                res = None
            if res:
                by_ott[res[0]] = res
    cards = []
    for ott, label in OTTS:
        res = by_ott.get(ott)
        if not res:
            continue
        _ott, vl, info = res
        q, langs, subs = info[:3]
        ln = "/".join(_LANG.get(x, x) for x in langs[:3]) or "audio"
        extra = " • ".join(x for x in [
            "/".join(q) or "HLS",
            ln + ("+%d" % (len(langs) - 3) if len(langs) > 3 else ""),
            "subs" if subs else "",
            "direct"] if x)
        qstr = "/".join(q) or "HLS"
        cname, cdesc = _fmt_card(
            _ql_label(qstr), title or label,
            ("◫ S%02d E%02d" % (s, e)) if s and e else "◫ MOVIE",
            "", ln, label, subs=1 if subs else 0)
        cards.append({
            "name": cname,
            "description": cdesc,
            "url": vl,
            "behaviorHints": {
                "notWebReady": True,
                "proxyHeaders": {"request": {"Referer": NET}},
                "bingeGroup": "%s-%s" % (ADDON_ID, ott),
            },
        })
        dbg("[newtv] %s card %s %s" % (ott, eid, "/".join(q) or "?"))
    return cards


# ------------------------------------------------------- embed-tmdb ---------
def _directize(url):
    """v1.0.9: net27's embed API returns some urls (captions) as RELATIVE
    proxy wrappers — '/api/proxy/video?url=<encoded direct url>'.  A
    Stremio client resolves relatives against the ADDON's origin, i.e.
    netmirror-stremio.onrender.com/api/proxy/video?url=… — this addon has
    no such route (404) and routing bytes through Render violates the
    strict zero-bandwidth directive.  Unwrap to the direct CDN url;
    absolute urls pass through unchanged."""
    try:
        p = urlparse(url or "")
        if p.scheme in ("http", "https"):
            return url
        if p.path.startswith("/"):
            inner = (parse_qs(p.query).get("url") or [""])[0]
            if inner.startswith("http"):
                return inner
    except Exception:
        pass
    return url


def embed_tmdb_cards(tmdb, kind, s, e, deadline=None, title=None):
    """net27 embed-tmdb -> signed DIRECT mp4 cards (+ captions as
    stream.subtitles).  The mp4 CDN 429s datacenter IPs, so cards are
    only listed after a proxy-verified 200/206 (honest cards)."""
    if not tmdb or not TH_NET27.wait(deadline):
        return []
    url = EMBED_TMDB + str(tmdb)
    if kind == "series":
        # v1.2.5 FIX: the net27 API's params are 'se' + 'ep'. The old
        # 's'/'e' params were silently IGNORED — every episode answered
        # with S1E1's video (user: "sob EP e ektai same EP play kore").
        url += "?type=tv&se=%d&ep=%d" % (s or 1, e or 1)
    try:
        r = _S.get(url, headers={"Accept": "application/json",
                                 "Referer": NET27_REF, "User-Agent": UA},
                   timeout=_clamp(deadline, 15) if deadline else 15)
        j = r.json() if r.status_code == 200 else {}
    except Exception:
        return []
    streams = [x for x in (j.get("streams") or []) if x.get("url")]
    if not streams:
        return []
    # verify ONCE (top resolution) via the proxy pool
    top = max(streams, key=lambda x: int(x.get("resolution") or 0))
    sub_deadline = time.time() + 10
    if deadline:
        sub_deadline = min(sub_deadline, deadline)
    if not POOL.check_206(top["url"], sub_deadline):
        dbg("[embed-tmdb] mp4 verify failed (datacenter+proxy blocked)")
        return []
    # v1.2.2: Stremio expects ISO-639-1 lang codes ('en'), not names
    _NAME2L1 = {"english": "en", "hindi": "hi", "bengali": "bn", "tamil": "ta",
                "telugu": "te", "marathi": "mr", "punjabi": "pa",
                "arabic": "ar", "spanish": "es", "french": "fr",
                "portuguese": "pt", "russian": "ru", "indonesian": "id",
                "malay": "ms", "chinese": "zh", "japanese": "ja",
                "korean": "ko", "turkish": "tr", "german": "de",
                "italian": "it", "thai": "th", "vietnamese": "vi"}
    subs = [{"url": _directize(c.get("url")),
             "lang": _NAME2L1.get((c.get("name") or "").strip().lower(),
                                  (c.get("name") or "")[:2].lower()),
             "id": "nm-%d" % i}
            for i, c in enumerate(j.get("captions") or [])
            if c.get("url")][:8]
    out = []
    for st in sorted(streams,
                     key=lambda x: -int(x.get("resolution") or 0)):
        res = int(st.get("resolution") or 0)
        cname, cdesc = _fmt_card(
            _ql_label("%dp" % res), title or "MP4", "◫ MP4", "",
            "", "net27", subs=len(subs))   # v1.2.2: no fake multi-audio
        out.append({
            "name": cname,
            "description": cdesc,
            "url": _directize(st["url"]),
            "subtitles": subs,
            "behaviorHints": {
                "notWebReady": True,
                "proxyHeaders": {"request": {"Referer": NET27_REF}},
                "bingeGroup": "%s-mp4" % ADDON_ID,
            },
        })
    dbg("[embed-tmdb] %d mp4 cards (%s)" % (len(out), tmdb))
    return out


# ------------------------------------------------------------- engine -------
# v1.0.6: when netmirror's own CDN is down (masters fine, variants
# 404 for everyone) every Engine-A chain burns the whole budget just
# to conclude "no cards" — remember that state for a few minutes so
# requests complete at Engine-B speed instead.  The first request
# after expiry re-probes (that's the recovery detector).
_SITE_CDN_DOWN = {"ts": 0.0}
SITE_CDN_DOWN_TTL = 480.0        # 8 min


def _site_cdn_down():
    return time.time() - _SITE_CDN_DOWN["ts"] < SITE_CDN_DOWN_TTL


def engine_newtv(kind, tt, name, year, s, e, deadline):
    """search -> post -> episode id -> per-OTT adaptive cards.  Every
    provider's name form is tried (cinemata/tmdb/imdb disagree).
    v1.0.3: MOVIES are first tried DIRECTLY by their IMDb id —
    player.php accepts tt ids with no cookie at all (verified:
    tt8175400/tt34339725/tt1649418 all return masters), so the flaky
    net52 search/cookie/proxy chain is skipped entirely for them.
    Series ids (tt:S:E) are NOT accepted by the player ("Video ID
    Missing") — series keeps the full chain."""
    if _site_cdn_down():
        return []                # outage memo: cards can't exist now
    if kind == "movie":
        cards = newtv_cards(tt, name, 0, 0, deadline)
        if cards:
            return cards
        dbg("[newtv] movie %s: no direct tt cards, falling back to "
            "search" % tt)
    cid_post = POSTS.get(tt)
    if cid_post is None:
        cid, post = None, None
        for nm, yr in name_candidates(kind, tt, (name, year or "")):
            if deadline and time.time() >= deadline - 2:
                break
            cid, post = find_post(nm, yr or year or "", deadline)
            if cid:
                break
        POSTS.put(tt, (cid, post) if cid else False)
    else:
        cid, post = cid_post if cid_post else (None, None)
    if not cid or not post:
        return []
    if kind == "series":
        ek = (cid, s, e)
        eid = EPIDS.get(ek)
        if eid is None:
            eid = episode_id(cid, post, s, e, deadline)
            EPIDS.put(ek, eid or False)
        if not eid:
            dbg("[newtv] no episode id %s %sx%s" % (cid, s, e))
            return []
    else:
        eid = cid                      # movies play by their post id
    return newtv_cards(eid, post.get("title") or name, s, e, deadline)


def _merge_subs(cards):
    """v1.2.3: deduped subtitle list from stream cards (for the
    subtitles-resource route Nuvio-style players call)."""
    subs, seen = [], set()
    for c in (cards or []):
        for s in (c.get("subtitles") or []):
            u = s.get("url")
            if u and u not in seen:
                seen.add(u)
                subs.append({"url": u, "lang": s.get("lang", "en"),
                             "id": s.get("id", "nm-0")})
    return subs

def streams_for_tt(kind, tt, s=0, e=0):
    key = f"tt:{kind}:{tt}:{s}:{e}"
    hit = STREAMS.get(key)
    if hit is not None:
        return hit or []
    if TTNEG.get(key) is not None:
        return []
    deadline = time.time() + BUDGET

    # metadata (fastest wins) + tmdb id for the embed engine
    info = cinemeta_name(kind, tt)
    name, year = (info or (None, None))
    if not name:
        TTNEG.put(key, True)
        return []

    def run_newtv():
        try:
            return engine_newtv(kind, tt, name, year or "", s, e, deadline)
        except Exception:
            return []

    def run_embed():
        try:
            tmdb = tt_tmdb(kind, tt)
            return embed_tmdb_cards(tmdb, kind, s, e, deadline, title=name)
        except Exception:
            return []

    # v1.2.6 (user: "stream show korte time onek nei"): answer the
    # player the MOMENT one engine produces cards instead of waiting
    # for the other (a slow newtv proxy chain used to hold an already
    # ready embed answer hostage). The losing engine keeps running and
    # its cards merge into the cache when they land.
    ex = cf.ThreadPoolExecutor(max_workers=2)
    f1 = ex.submit(run_newtv)
    f2 = ex.submit(run_embed)
    newtv_res = []

    def _bg_merge(fut, is_newtv):
        try:
            res = fut.result()
        except Exception:
            res = []
        if not res:
            return
        with STREAMS_LOCK:
            cur = STREAMS.get(key) or []
            seen = {c["url"] for c in cur}
            merged = cur + [c for c in res if c["url"] not in seen]
            STREAMS.put(key, merged)

    def _mk(fut, is_newtv):
        return lambda _f: _bg_merge(fut, is_newtv)

    f1.add_done_callback(_mk(f1, True))
    f2.add_done_callback(_mk(f2, False))
    try:
        pending = {f1, f2}
        out = []
        while pending and time.time() < deadline:
            done, pending = cf.wait(
                pending, timeout=max(0.2, deadline - time.time()),
                return_when=cf.FIRST_COMPLETED)
            for f in done:
                try:
                    res = f.result()
                except Exception:
                    res = []
                if f is f1:
                    newtv_res = res
                if res and not out:            # first engine to land cards:
                    out.extend(res)            # the response goes out NOW
            if out:
                break
        try:
            if pending:
                for f in pending:
                    f.cancel()
        except Exception:
            pass
    finally:
        ex.shutdown(wait=False)
    # dedupe by url
    seen, uniq = set(), []
    for st in out:
        if st["url"] not in seen:
            seen.add(st["url"])
            uniq.append(st)
    if uniq:
        STREAMS.put(key, uniq)
    else:
        TTNEG.put(key, True)

    # v1.0.3 background-continue: when Engine A (adaptive HLS) didn't
    # finish inside the request budget (flaky free proxies rotate mid-
    # chain), it retries in the BACKGROUND with a longer budget and
    # merges its cards into STREAMS — the user's next refresh sees the
    # HLS cards instantly.  Negative caches it may have written are
    # cleared first so the retry actually re-searches.
    if len(newtv_res) < len(OTTS):
        # nothing at all -> full retry; partial (an OTT lost to a dead
        # exit) -> top-up retry; both merge by url into STREAMS
        def _bg_engine():
            try:
                if POSTS.get(tt) is False:
                    POSTS.delete(tt)
                cards = engine_newtv(kind, tt, name, year or "",
                                     s, e, time.time() + BG_BUDGET)
            except Exception:
                cards = []
            if cards:
                prev = STREAMS.get(key) or []
                have = {p["url"] for p in prev}
                merged = prev + [c for c in cards
                                 if c["url"] not in have]
                STREAMS.put(key, merged)
                TTNEG.delete(key)
                dbg("[bg] engine A finished late for %s: %d cards "
                    "cached" % (tt, len(merged)))
        threading.Thread(target=_bg_engine, daemon=True).start()
    return uniq


# ------------------------------------------------------------ stremio -------
MANIFEST = {
    "id": ADDON_ID,
    "version": VERSION,
    "name": ADDON_NAME,
    "description": ("Netflix / Hotstar / Prime Video mirrors (netmirror) — "
                    "works with ANY catalog (IMDb, Trakt, ...): open a "
                    "title and NETMIRROR streams appear. Multi-audio "
                    "adaptive HLS + mp4, direct CDN links, zero addon "
                    "bandwidth."),
    "logo": ("https://image.tmdb.org/t/p/w500/"
             "9O1Iy9odqMlHfBCXg7xw3fPnXnz.jpg"),
    "types": ["movie", "series"],
    # v1.2.3: subtitles declared as a resource (Nuvio-style players
    # fetch /subtitles/... instead of reading stream-embedded subs).
    "resources": ["stream", "subtitles"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "behaviorHints": {"configurable": False},
}


class Handler(BaseHTTPRequestHandler):
    server_version = "netmirror/" + VERSION

    # v1.0.9: real accounting for /debug/bandwidth — every byte the
    # addon sends to a client is counted.  Media bytes are 0 by
    # construction (no route ever relays a segment or an mp4); this
    # counter proves it instead of asserting it.
    _egress_lock = threading.Lock()
    EGRESS = {"bytes": 0, "responses": 0}

    def log_message(self, fmt, *args):
        try:
            if "/health" in (fmt % args):
                return
        except Exception:
            pass
        dbg("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        with Handler._egress_lock:
            Handler.EGRESS["bytes"] += len(body)
            Handler.EGRESS["responses"] += 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _404(self):
        self._send(404, {"error": "not found"})

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path)
        # Stremio URL-encodes the ':' in series ids (tt4574334%3A1%3A1) —
        # v1.0.1: decode BEFORE route matching (the 404s ate every series)
        path = unquote(p.path)
        try:
            if path == "/manifest.json":
                return self._send(200, MANIFEST)
            if path == "/health":
                return self._send(200, {
                    "status": "ok", "version": VERSION,
                    "thash": bool(_THASH["v"]),
                    "apibase": _APIBASE["v"] or ""})
            # v1.2.3: subtitles-resource route — reuses the cached build
            m = re.match(r"^/subtitles/movie/(tt\d+)\.json$", path)
            if m:
                cards = streams_for_tt("movie", m.group(1))
                return self._send(200, {"subtitles": _merge_subs(cards),
                                        "cacheMaxAge": 300})
            m = re.match(r"^/subtitles/series/(tt\d+):(\d+):(\d+)\.json$",
                         path)
            if m:
                cards = streams_for_tt("series", m.group(1), int(m.group(2)),
                                       int(m.group(3)))
                return self._send(200, {"subtitles": _merge_subs(cards),
                                        "cacheMaxAge": 300})
            m = re.match(r"^/stream/movie/(tt\d+)\.json$", path)
            if m:
                return self._send(200, {"streams":
                                        streams_for_tt("movie", m.group(1))})
            m = re.match(r"^/stream/series/(tt\d+):(\d+):(\d+)\.json$", path)
            if m:
                return self._send(200, {"streams": streams_for_tt(
                    "series", m.group(1), int(m.group(2)), int(m.group(3)))})
            if path == "/debug/lastlog":
                with LOG_LOCK:
                    body = "".join(LOG_RING[-120:])
                return self._send(200, {"log": body})
            if path == "/debug/exits":
                with _PROXY_EXITS_LOCK:
                    exs = [{"px": e["px"], "ck": e["ck"][:6] + "...",
                            "fails": e["fails"],
                            "age": int(time.time() - e["ts"])}
                           for e in _PROXY_EXITS]
                return self._send(200, {"exits": exs,
                                        "pool": len(POOL.proxies)})
            if path.startswith("/debug/net"):
                from urllib.parse import parse_qs
                qs = parse_qs(p.query)
                u = (qs.get("u", [""])[0] or "").strip()
                host = urlparse(u).netloc
                ok = u.startswith("http") and any(
                    host.endswith(w) for w in (
                        "net52.cc", "net77.cc", "netmirror.nl",
                        "netmirror.to", "imgcdn.kim", "net27.cc",
                        "netmirror.app", "mobiledetects.com",
                        "mobiledetect.app"))
                if not ok:
                    return self._send(200, {"error": "host not allowed"})
                try:
                    rr = _S.get(u, headers=MOBILE_HDRS, timeout=10,
                                allow_redirects=False)
                    return self._send(200, {"status": rr.status_code,
                                            "head": rr.text[:100]})
                except Exception as ex:
                    return self._send(200, {"exc": str(ex)[:120]})
            if path.startswith("/debug/site"):
                # netmirror CDN health through the REAL code path
                # (direct + exit fallback): player -> master -> variant
                from urllib.parse import parse_qs
                qs = parse_qs(p.query)
                sid = (qs.get("id", ["tt8175400"])[0] or "tt8175400")
                dl = time.time() + 25
                out = {"id": sid, "otts": {}}
                for ott, _l in OTTS:
                    ent = {}
                    try:
                        vl, _ref = newtv_player(sid, ott, dl)
                        ent["player"] = "ok" if vl else "dead"
                        if vl:
                            info = _master_info(vl, dl)
                            ent["master"] = "ok" if info else "dead"
                            if info:
                                alive = _variant_alive(vl, info[3], dl)
                                ent["variant"] = "ok" if alive else "DEAD"
                    except Exception as ex:
                        ent["exc"] = str(ex)[:80]
                    out["otts"][ott] = ent
                return self._send(200, out)
            if path == "/debug/bandwidth":
                with Handler._egress_lock:
                    b, n = Handler.EGRESS["bytes"], \
                        Handler.EGRESS["responses"]
                return self._send(200, {
                    "mode": "zero-bandwidth (all streams direct)",
                    "relayed_media_bytes": 0,
                    "client_egress_bytes": b,
                    "responses": n,
                    "note": "media bytes = 0 by construction: cards AND "
                            "subtitles point directly at the netmirror "
                            "CDNs (v1.0.9 unwraps net27's relative "
                            "/api/proxy/video caption wrappers — that "
                            "route never existed here); client_egress is "
                            "KB-scale stream JSON only. Referer sent by "
                            "the player via proxyHeaders"})
            if path == "/":
                return self._send(200, LANDING, "text/html; charset=utf-8")
            return self._404()
        except Exception as ex:
            dbg(f"[err] {path}: {ex!r}\n" + traceback.format_exc())
            return self._send(200, {"streams": []})


LANDING = """<!doctype html><html><head><meta charset='utf-8'>
<title>NetMirror — Stremio addon</title>
<style>
body{background:#0b0f17;color:#e8ecf4;font-family:system-ui,sans-serif;
max-width:760px;margin:40px auto;padding:0 20px;line-height:1.6}
a{color:#4da3ff} .btn{display:inline-block;background:#e50914;color:#fff;
padding:12px 26px;border-radius:10px;text-decoration:none;font-weight:700}
code{background:#151c2b;padding:2px 6px;border-radius:6px}
</style></head><body>
<h1>🎬 NetMirror <small>v""" + VERSION + """</small></h1>
<p>Netflix / Hotstar (Disney+) / Prime Video mirror streams from the
netmirror family — <b>works with any catalog</b>: browse IMDb, Trakt or
anything else, open a title, and NETMIRROR streams appear.</p>
<p><b>Zero addon bandwidth</b> — adaptive multi-audio HLS (Hindi/English/
Tamil/… + subtitles) and direct mp4s, straight from the CDNs to your
player.</p>
<p><a class="btn" href="stremio:///detail/series/tt4574334">Install in
Stremio</a></p>
<h3>Endpoints</h3>
<p><code>/manifest.json</code> • <code>/stream/{tt}/…</code> •
<code>/health</code> • <code>/debug/bandwidth</code></p>
</body></html>"""


# ------------------------------------------------- keepalive / liveness -----
SELF_PING_URL = os.environ.get(
    "SELF_PING_URL", "https://netmirror-stremio.onrender.com")
KEEPALIVE = {"last": 0, "ok": 0, "fail": 0, "code": 0}
LIVENESS = {"fails": 0, "last": 0}


def _keepalive_loop():
    time.sleep(90)
    while True:
        code = None
        try:
            code = requests.get(SELF_PING_URL + "/health",
                                timeout=30).status_code
        except Exception:
            pass
        if code == 200:
            KEEPALIVE["ok"] += 1
            KEEPALIVE["fail"] = 0
        else:
            KEEPALIVE["fail"] += 1
        KEEPALIVE["code"] = code or 0
        KEEPALIVE["last"] = time.time()
        time.sleep(600)


def _exit_keeper_loop():
    """v1.0.8 (moviebox trainer knowledge): keep the exit pool WARM and
    TRAINED — every ~2.5 min: (a) re-probe current live exits through
    the cookie-free imgcdn player call to refresh their EWMA records
    and bench the ones that just died, (b) mint up to 3 when short,
    (c) bound the learning dicts.  Requests then find warm, proven,
    best-scored exits instead of paying the mint race."""
    time.sleep(5)          # warm the pool right at boot (Render restarts)
    while True:
        try:
            with _PROXY_EXITS_LOCK:
                live = list(_live_exits())
            # train pass: cheap imgcdn probe per exit (its own cookie
            # is irrelevant for player.php — cookie-free endpoint)
            for ent in live[:6]:
                _exit_busy_inc(ent["px"])
                t0 = time.time()
                try:
                    r = POOL.fetch(
                        "GET", "https://tv.imgcdn.kim/newtv/player.php"
                        "?id=tt8175400", ent["px"],
                        headers={**NEWTV_HEADERS, "Ott": "nf"},
                        timeout=5)
                except Exception:
                    r = None
                finally:
                    _exit_busy_dec(ent["px"])
                ms = int((time.time() - t0) * 1000)
                if r is not None and r.status_code == 200:
                    _note_exit_good(ent["px"], ms)
                else:
                    _note_exit_dead(ent["px"],
                                    blocked=(r is not None
                                             and r.status_code == 403))
                    _note_exit_fail(ent["px"])   # 2-strike cookie evict
            with _PROXY_EXITS_LOCK:
                still = len(_live_exits())
            if still < 3:
                _mint_exits(time.time() + 45)
            _exit_train_bound()
        except Exception:
            pass
        time.sleep(150)


def _liveness_watchdog(port):
    time.sleep(120)
    while True:
        try:
            r = requests.get("http://127.0.0.1:%d/health" % port,
                             timeout=25)
            ok = r.status_code == 200
        except Exception:
            ok = False
        LIVENESS["last"] = time.time()
        if ok:
            LIVENESS["fails"] = 0
        else:
            LIVENESS["fails"] += 1
            if LIVENESS["fails"] >= 3:
                os._exit(1)
        time.sleep(300)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else \
        int(os.environ.get("PORT", "7860"))
    threading.Thread(target=_keepalive_loop, daemon=True).start()
    threading.Thread(target=_exit_keeper_loop, daemon=True).start()
    threading.Thread(target=_liveness_watchdog, args=(port,),
                     daemon=True).start()

    class _MemErr:
        def write(self, txt):
            if txt and txt.strip():
                dbg(txt.rstrip()[:2000])

        def flush(self):
            pass

    sys.stderr = _MemErr()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[netmirror] v{VERSION} listening on :{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
