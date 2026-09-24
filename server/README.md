# Server — ingestion API + dashboard (Docker)

A small Python (FastAPI) + SQLite app. Receives the temperature/humidity readings
POSTed by the iOS Shortcut, stores them, and serves the dashboard. **Local only** —
no outbound internet access.

## Contents

| File | Role |
|---|---|
| `app.py` | API + dashboard (single file) |
| `static/chart.umd.min.js` | **vendored** Chart.js (no CDN, fully local page) |
| `static/icon-*.png`, `apple-touch-icon.png` | app icons (PWA) |
| `tools/gen_icons.py` | regenerate the icons (thermometer) — pure stdlib, for recoloring |
| `requirements.txt` | FastAPI + uvicorn |
| `Dockerfile` | `python:3.12-slim` image |
| `docker-compose.yml` | port + `/data` volume |

## API

### `POST /api/readings` — ingestion

Single reading:
```json
{ "homepod": "living-room", "temp": 24.3, "humidity": 52.0, "timestamp": "2026-07-03T14:00:00Z" }
```
`timestamp` is **optional** (ISO-8601; the `Z` UTC suffix is accepted). When absent
the server uses receive time.

Batch (recommended, to send both HomePods in one POST):
```json
{ "readings": [
  { "homepod": "living-room", "temp": 24.3, "humidity": 52.0 },
  { "homepod": "bedroom",     "temp": 21.1, "humidity": 58.0 }
] }
```

- **Idempotent**: an identical `(homepod, timestamp)` pair is never duplicated
  (upsert). Replaying a POST is safe.
- **Localized values**: `temp`/`humidity` may be plain numbers **or** strings as
  HomePods emit them — e.g. `"temp": "21,4°C"`, `"humidity": "52,0 %"` (decimal comma
  and trailing unit are stripped). Send such values **quoted**, since a bare comma is
  invalid JSON.
- **Validation**: `400` if `temp` is outside `[-40, 80]`, `humidity` outside
  `[0, 100]`, the value isn't a parseable number, or `timestamp` isn't parseable.
- **200** response: `{ "status": "ok", "received": N, "stored": N }`.

### `GET /api/series?range=24h|3d|7d|30d|all`
Chart-ready series grouped by HomePod.

### `GET /api/latest`
Latest known reading per HomePod (feeds the dashboard's live tiles).

### `GET /` — dashboard
- **Live tiles** per HomePod: large temperature + humidity, "x min ago", and a
  comfort chip (✓ Comfort / • OK / ▲ Out of range — icon + label, never color alone).
- **Two charts** (temperature, humidity), both HomePods overlaid, end-of-line value
  labels, hover crosshair + tooltip.
- **Pods / Average switch** — plot each HomePod, or one apartment-average curve
  (hourly buckets; persisted client-side).
- **Per-series stats** (min – max · average) over the selected range.
- **Ranges** 24h / 3d / 7d / 30d / all, auto-refresh (60s). The 24h and 3d axes are
  pinned to round local hours (ticks every 3h / 12h), so the time scale doesn't
  drift with the newest reading; 7d ticks sit on midnights.
- **Today vs yesterday** — both days' apartment-average temperature overlaid on a
  time-of-day axis, with the Δ compared over the same hours.
- **Daily averages** — one figure per day, color-binned from cold (blue) to hot
  (red), as a Monday-first calendar with one page per month: swipe left/right
  (or use ‹ ›) to browse the whole history.
- **Responsive** phone/desktop, **automatic light/dark**, iOS-style translucent
  "liquid glass" surfaces.
- **Installable PWA** (see below) — Chart.js is vendored, so the page needs no CDN.
- **Optional outdoor-weather backdrop** (Open-Meteo) behind the indoor curves, with
  an on/off toggle — only when `WEATHER_LAT`/`WEATHER_LON` are set on the server.

### `GET /manifest.webmanifest` · `GET /sw.js`
PWA manifest and service worker (caches the shell + data). Served at root so the
service-worker scope covers the whole app.

### `GET /api/health`
`{ "status": "ok", "readings": <count>, "retention_days": <n> }`.

## Environment variables

| Variable | Default | Role |
|---|---|---|
| `PORT` | `8088` | listen port |
| `DB_PATH` | `/data/homepod.db` | SQLite file (inside the volume) |
| `RETENTION_DAYS` | `0` | `0` = unlimited history; `>0` prunes readings older than N days at startup |
| `WEATHER_LAT` / `WEATHER_LON` | *(unset)* | **opt-in** outdoor-weather overlay: set both to your coordinates to draw [Open-Meteo](https://open-meteo.com)'s hourly outdoor temperature/humidity as a gray backdrop behind the indoor curves (free, no API key). When unset the server makes **no outbound request** — the app stays 100% local |
| `WEATHER_REFRESH_S` | `3600` | how often to re-poll Open-Meteo (min 300s). Each poll backfills ~92 past days, so history survives restarts |

## Run locally (quick test)

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./homepod.db PORT=8088 uvicorn app:app --host 0.0.0.0 --port 8088
```

POST a fake reading, then check it:
```bash
curl -X POST http://127.0.0.1:8088/api/readings \
  -H 'Content-Type: application/json' \
  -d '{"homepod":"living-room","temp":24.3,"humidity":52}'

curl "http://127.0.0.1:8088/api/series?range=all"
```
Open `http://127.0.0.1:8088/` — the reading shows up on the charts.

## Docker

### Prebuilt image (recommended)
```bash
docker run -d --name homepod-temp-logger \
  -p 8088:8088 \
  -v /path/to/appdata/homepod-temp-logger:/data \
  ghcr.io/gregbny/homepod-temp-logger:latest
```

### Build locally
```bash
cd server
docker compose up -d --build
```
The `.db` is persisted in `./data` (mounted at `/data`).

## Deploy on Unraid

See [`../TUTORIAL.md`](../TUTORIAL.md#step-1--deploy-the-server-unraid) for the exact
**Add Container** fields, or import the one-click template
[`../unraid/homepod-temp-logger.xml`](../unraid/homepod-temp-logger.xml).

The URL to use in the Shortcut's POST is:
```
http://SERVER_IP:8088/api/readings
```

## Install as a PWA (home screen)

The page is a **PWA**: Chart.js is vendored, a manifest and service worker are
served, and the shell + data are cached. No internet required.

- **iPhone/iPad (Safari):** open `http://SERVER_IP:8088/` → **Share** → **Add to Home
  Screen**. An install tip appears automatically in iOS Safari.
- **Mac (Chrome/Edge):** install icon in the address bar, or menu → *Install*.
- **Offline:** once opened, the app re-shows the last cached charts even if the
  server is briefly unreachable; data refreshes as soon as it responds again.

> After updating the app, bump `CACHE` in the service worker
> (`homepod-logger-v1` → `-v2`) to force a cache refresh.

## Regenerate / recolor the icon

Icons are PNGs generated without dependencies:
```bash
python3 server/tools/gen_icons.py
```
Edit the color constants at the top of the script (`TOP`, `BOT`, `RED`), then re-run.

## Localization

The dashboard ships in English. All UI strings live in `INDEX_HTML` inside `app.py`
(labels, range buttons, comfort chips, "x ago"). Swapping them for another language
is a find-and-replace in that one block.
