# VA × NetMirror — configurable stream-only Stremio addon

Two native-HLS sources behind one **configuration page**:

| Source | What it gives |
|---|---|
| ▶️ **VA Player** | `streamdata.vaplayer.ru` — 3 native-HLS masters per title (ported from CinemaVIP) |
| 🎬 **NetMirror** | the full netmirror machinery (thash bootstrap, newtv + embed engines, exit pool) — Netflix/Hotstar/Prime mirrors, multi-audio HLS + mp4 |

## Configure
Open `/configure`, tick the sources you want, hit **Install** — the choice
is encoded into the install URL (`/cfg-<token>/manifest.json`). No token =
both sources ON. Reconfigure any time.

## Endpoints
- `/configure` — the configuration page
- `/cfg-<token>/manifest.json` — configured install
- `/manifest.json` — default install (both sources)
- `/[cfg-<token>/]stream/movie/<tt>.json` · `/[cfg-<token>/]stream/series/<tt>:s:e.json`
- `/health`

Cards are direct provider links — the addon relays no media.

## Local
```
pip install requests
python addon.py          # PORT env respected
pytest test_vanm.py
```

## Deploy (beamup)
Procfile build (`web: python addon.py`), no Dockerfile:
```
git push beamup HEAD:refs/heads/master --force   # detached commit, Dockerfile removed
```

## v1.0.1
- Security: TMDB key no longer hardcoded — read from `TMDB_KEY` env/config var (public-repo key policy). Falls back to IMDb-suggest title resolution if unset.
