# VA × NetMirror × MovieBox — configurable stream-only Stremio addon

Three stream sources behind one **configuration page**:

| Source | What it gives |
|---|---|
| ▶️ **VA Player** | `streamdata.vaplayer.ru` — 3 native-HLS masters per title (ported from CinemaVIP) |
| 🎬 **NetMirror** | the full netmirror machinery (`nm_core`) — Netflix/Hotstar/Prime mirrors, multi-audio HLS + mp4 |
| 📦 **MovieBox** | the full moviebox machinery (`mb_core`) — multi-audio HLS with a quality menu; master/variant playlist TEXT is served by this addon's `/hls` routes, segments are absolute signed CDN URLs |

## Configure
Open `/configure`, tick the sources you want, hit **Install** — the choice
is encoded into the install URL (`/cfg-<token>/manifest.json`). No token =
all sources ON (older 2-key tokens are honoured, MovieBox defaults ON).
Reconfigure any time.

## Endpoints
- `/configure` — the configuration page
- `/cfg-<token>/manifest.json` — configured install
- `/manifest.json` — default install (all sources)
- `/[cfg-<token>/]stream/movie/<tt>.json` · `/[cfg-<token>/]stream/series/<tt>:s:e.json`
- `/hls/<sid>/<se>/<ep>/(master|vN|aN).m3u8` — MovieBox playlist text (tiny, gzip; zero media bytes)
- `/health`

Cards are direct provider links — the addon relays no media.

## Local
```
pip install requests[socks]
export TMDB_KEY=...        # nm_core/mb_core title resolution
export MB_SECRET_KEY=...   # moviebox platform x-tr-sign key
python addon.py            # PORT env respected
python -m unittest test_vanm
```

## Deploy (beamup)
Procfile build (`web: python addon.py`), no Dockerfile:
```
git push beamup HEAD:refs/heads/master --force
```
Config vars: `TMDB_KEY`, `MB_SECRET_KEY` (env/config only — no keys in the repo).

## Changelog
### v1.1.0 (r2: public-URL host_base fix)
- **New: 📦 MovieBox source** — third toggle on the configure page; cards
  rebranded `♧ → 📦` so sources are tellable apart; `/hls` playlist routes
  added; mb pool/token machinery boots with the addon.
- Legacy 2-key config tokens keep working (MovieBox defaults ON).
- Security: mb_core keys moved to env (`TMDB_KEY`, `MB_SECRET_KEY`).
- Env: `MOVIEBOX_WEB_MP4=1` in prod — header-free signed web-MP4 cards so Stremio Web has a playable option (edge-HLS segments are Cookie-scoped by the platform).

### v1.0.1
- Security: TMDB key read from `TMDB_KEY` env/config (public-repo key policy).

### v1.1.2
- mb_core refresh: MovieBox v1.9.14 — H5-gateway header-free file cards (signed MP4s) for movies; series stays on the cookie-scoped HLS ladder.
