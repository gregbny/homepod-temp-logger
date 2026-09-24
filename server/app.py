"""HomePod Logger — ingestion server + dashboard.

Minimal stack: FastAPI + SQLite (stdlib). No heavy dependencies, no outbound
internet access. Everything stays local (LAN).

Payload contract:
    POST /api/readings
    {
      "homepod": "living-room", "temp": 24.3, "humidity": 52.0,
      "timestamp": "2026-07-03T14:00:00Z"   # optional
    }
  or batch:
    { "readings": [ {…}, {…} ] }

``temp`` and ``humidity`` may be plain numbers OR strings as HomePods emit
them, e.g. "21,4°C" / "52,0 %" (decimal comma and unit are tolerated). Send
such values quoted, since a bare comma is invalid JSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Iterator, Optional

log = logging.getLogger("homepod")

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

# --------------------------------------------------------------------------- #
# Configuration (via environment variables)
# --------------------------------------------------------------------------- #
DB_PATH = os.environ.get("DB_PATH", "/data/homepod.db")
# 0 = no pruning (unlimited history, the default).
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "0"))

# Outdoor weather background (opt-in). When WEATHER_LAT/WEATHER_LON are unset the
# feature is fully disabled and the server makes NO outbound request — the app
# stays 100% local, exactly as before. Set both to overlay Open-Meteo's hourly
# outdoor temperature/humidity behind the indoor curves.
def _opt_float(name: str) -> Optional[float]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


WEATHER_LAT = _opt_float("WEATHER_LAT")
WEATHER_LON = _opt_float("WEATHER_LON")
WEATHER_ENABLED = WEATHER_LAT is not None and WEATHER_LON is not None
# How often to re-poll Open-Meteo (seconds). One call backfills ~92 past days.
WEATHER_REFRESH_S = int(os.environ.get("WEATHER_REFRESH_S", "3600"))

# "Plausible" validation bounds.
TEMP_MIN, TEMP_MAX = -40.0, 80.0
HUM_MIN, HUM_MAX = 0.0, 100.0

app = FastAPI(title="HomePod Logger", version="1")

# Static files (vendored Chart.js + PWA icons) served locally.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                homepod   TEXT    NOT NULL,
                temp      REAL    NOT NULL,
                humidity  REAL    NOT NULL,
                ts        TEXT    NOT NULL
            )
            """
        )
        # Idempotency: a (homepod, ts) pair is unique.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_homepod_ts "
            "ON readings (homepod, ts)"
        )
        # Outdoor weather (Open-Meteo). One row per hour; ts is the primary key so
        # re-polling the same hours upserts instead of duplicating. History
        # accumulates over time just like readings.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weather (
                ts        TEXT    PRIMARY KEY,
                temp      REAL,
                humidity  REAL
            )
            """
        )


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    prune_old()
    if WEATHER_ENABLED:
        asyncio.create_task(_weather_loop())


def prune_old() -> None:
    """Optional pruning. Disabled by default (RETENTION_DAYS=0)."""
    if RETENTION_DAYS <= 0:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    with db() as conn:
        conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))


# --------------------------------------------------------------------------- #
# Outdoor weather (Open-Meteo) — opt-in, server-side, cached in SQLite
# --------------------------------------------------------------------------- #
# Free, no API key, no signup. A single call returns hourly outdoor temperature
# and relative humidity for the past ~92 days plus the current day.
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_WEATHER_PAST_DAYS = 92  # Open-Meteo's max look-back on the forecast endpoint.


def fetch_weather() -> int:
    """Poll Open-Meteo and upsert hourly outdoor readings. Returns rows stored.

    Blocking (urllib). Call it off the event loop via ``asyncio.to_thread`` so a
    slow network never stalls request handling. Any failure is swallowed and
    logged: the weather overlay is best-effort and must never break the app.
    """
    if not WEATHER_ENABLED:
        return 0
    query = urllib.parse.urlencode({
        "latitude": WEATHER_LAT,
        "longitude": WEATHER_LON,
        "hourly": "temperature_2m,relative_humidity_2m",
        "past_days": _WEATHER_PAST_DAYS,
        "forecast_days": 1,
        "timezone": "UTC",
    })
    req = urllib.request.Request(
        f"{OPEN_METEO_URL}?{query}",
        headers={"User-Agent": "homepod-temp-logger"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.load(resp)

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    hums = hourly.get("relative_humidity_2m") or []

    rows = []
    for t, temp, hum in zip(times, temps, hums):
        if temp is None and hum is None:
            continue
        # Open-Meteo returns naive local times; timezone=UTC makes them UTC.
        ts = datetime.fromisoformat(t).replace(tzinfo=timezone.utc).isoformat()
        rows.append((ts, temp, hum))

    if not rows:
        return 0
    with db() as conn:
        conn.executemany(
            """
            INSERT INTO weather (ts, temp, humidity) VALUES (?, ?, ?)
            ON CONFLICT(ts) DO UPDATE SET
                temp = excluded.temp,
                humidity = excluded.humidity
            """,
            rows,
        )
    return len(rows)


async def _weather_loop() -> None:
    """Refresh the weather cache on startup, then every WEATHER_REFRESH_S."""
    while True:
        try:
            n = await asyncio.to_thread(fetch_weather)
            log.info("weather: refreshed %d hourly points", n)
        except Exception as exc:  # network, JSON, DB — never crash the app.
            log.warning("weather: refresh failed: %s", exc)
        await asyncio.sleep(max(WEATHER_REFRESH_S, 300))


# --------------------------------------------------------------------------- #
# Input schema
# --------------------------------------------------------------------------- #
# First number in a string, tolerating a decimal comma (e.g. "21,4°C" -> 21.4).
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _coerce_number(v: object) -> float:
    """Parse a temperature/humidity value the way HomePods actually emit it.

    HomePods report localized strings such as ``"21,4°C"`` or ``"52,0 %"``.
    Accept plain numbers, but also strings with a decimal comma and/or a
    trailing unit (``°C``, ``%``, …) so the iOS Shortcut can forward the raw
    value without pre-formatting it.
    """
    if isinstance(v, bool):  # bool is an int subclass; reject it explicitly.
        raise ValueError("expected a number, got a boolean")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        m = _NUMBER_RE.search(s)
        if m:
            return float(m.group())
    raise ValueError(f"could not parse a number from {v!r}")


class Reading(BaseModel):
    homepod: str = Field(min_length=1, max_length=64)
    temp: float
    humidity: float
    timestamp: Optional[str] = None

    @field_validator("temp", "humidity", mode="before")
    @classmethod
    def _parse_number(cls, v: object) -> float:
        return _coerce_number(v)

    @field_validator("temp")
    @classmethod
    def _check_temp(cls, v: float) -> float:
        if not (TEMP_MIN <= v <= TEMP_MAX):
            raise ValueError(f"temp {v} out of range [{TEMP_MIN}, {TEMP_MAX}]")
        return v

    @field_validator("humidity")
    @classmethod
    def _check_hum(cls, v: float) -> float:
        if not (HUM_MIN <= v <= HUM_MAX):
            raise ValueError(f"humidity {v} out of range [{HUM_MIN}, {HUM_MAX}]")
        return v


def normalize_ts(raw: Optional[str]) -> str:
    """Return an ISO-8601 UTC timestamp. Use receive time when absent."""
    if not raw:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    s = raw.strip()
    # Accept the "Z" suffix (Python < 3.11 does not parse it natively).
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid timestamp: {raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
@app.post("/api/readings")
async def ingest(payload: dict) -> JSONResponse:
    # Accept either a single reading or { "readings": [...] }.
    if "readings" in payload:
        raw_items = payload["readings"]
        if not isinstance(raw_items, list):
            raise HTTPException(status_code=400, detail="'readings' must be a list")
    else:
        raw_items = [payload]

    if not raw_items:
        raise HTTPException(status_code=400, detail="no reading provided")

    try:
        readings = [Reading(**item) for item in raw_items]
    except ValidationError as exc:
        # A trimmed error list stays JSON-serializable.
        detail = [
            {"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        raise HTTPException(status_code=400, detail=detail)
    except TypeError:
        raise HTTPException(status_code=400, detail="invalid reading format")

    stored = 0
    with db() as conn:
        for r in readings:
            ts = normalize_ts(r.timestamp)
            # Upsert: replaying the same (homepod, ts) updates, never duplicates.
            cur = conn.execute(
                """
                INSERT INTO readings (homepod, temp, humidity, ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(homepod, ts) DO UPDATE SET
                    temp = excluded.temp,
                    humidity = excluded.humidity
                """,
                (r.homepod, r.temp, r.humidity, ts),
            )
            stored += 1 if cur.rowcount else 0

    return JSONResponse({"status": "ok", "received": len(readings), "stored": stored})


# --------------------------------------------------------------------------- #
# Series for the dashboard
# --------------------------------------------------------------------------- #
RANGES = {
    "24h": timedelta(hours=24),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


@app.get("/api/series")
async def series(range: str = "24h") -> dict:
    if range not in RANGES:
        raise HTTPException(status_code=400, detail=f"unknown range: {range}")

    params: tuple = ()
    where = ""
    delta = RANGES[range]
    if delta is not None:
        cutoff = (datetime.now(timezone.utc) - delta).isoformat()
        where = "WHERE ts >= ?"
        params = (cutoff,)

    with db() as conn:
        rows = conn.execute(
            f"SELECT homepod, temp, humidity, ts FROM readings {where} "
            "ORDER BY homepod, ts",
            params,
        ).fetchall()

    data: dict[str, list] = {}
    for row in rows:
        # x in epoch milliseconds for a simple time axis on the Chart.js side.
        s = row["ts"]
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        x_ms = int(datetime.fromisoformat(s).timestamp() * 1000)
        data.setdefault(row["homepod"], []).append(
            {"x": x_ms, "temp": row["temp"], "humidity": row["humidity"]}
        )

    weather: list[dict] = []
    if WEATHER_ENABLED:
        with db() as conn:
            wrows = conn.execute(
                f"SELECT temp, humidity, ts FROM weather {where} ORDER BY ts",
                params,
            ).fetchall()
        for row in wrows:
            s = row["ts"]
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            x_ms = int(datetime.fromisoformat(s).timestamp() * 1000)
            weather.append(
                {"x": x_ms, "temp": row["temp"], "humidity": row["humidity"]}
            )

    return {
        "range": range,
        "homepods": sorted(data.keys()),
        "data": data,
        "weather_enabled": WEATHER_ENABLED,
        "weather": weather,
    }


@app.get("/api/latest")
async def latest() -> dict:
    """Latest known reading per HomePod (feeds the live tiles)."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT r.homepod, r.temp, r.humidity, r.ts
            FROM readings r
            JOIN (SELECT homepod, MAX(ts) AS mts FROM readings GROUP BY homepod) m
              ON r.homepod = m.homepod AND r.ts = m.mts
            ORDER BY r.homepod
            """
        ).fetchall()
    return {
        "homepods": [
            {"homepod": r["homepod"], "temp": r["temp"],
             "humidity": r["humidity"], "ts": r["ts"]}
            for r in rows
        ]
    }


@app.get("/api/health")
async def health() -> dict:
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        w = conn.execute("SELECT COUNT(*) AS n FROM weather").fetchone()["n"]
    return {
        "status": "ok",
        "readings": n,
        "retention_days": RETENTION_DAYS,
        "weather_enabled": WEATHER_ENABLED,
        "weather_points": w,
    }


# --------------------------------------------------------------------------- #
# PWA: manifest + service worker (served at root for the right scope)
# --------------------------------------------------------------------------- #
@app.get("/manifest.webmanifest")
async def manifest() -> Response:
    return Response(content=MANIFEST_JSON, media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> Response:
    # Served from "/" so the SW scope covers the whole app.
    return Response(content=SERVICE_WORKER_JS, media_type="application/javascript")


MANIFEST_JSON = """{
  "name": "HomePod Logger",
  "short_name": "HomePod Log",
  "description": "HomePod temperature & humidity logger",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#0d0d0d",
  "theme_color": "#1c5cab",
  "icons": [
    { "src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable" },
    { "src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable" }
  ]
}"""


SERVICE_WORKER_JS = r"""
const CACHE = "homepod-logger-v5";
const SHELL = [
  "/",
  "/static/chart.umd.min.js",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png",
  "/manifest.webmanifest"
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;

  // API data: network first, cache fallback (fresh data when online).
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Shell / static: cache first, network fallback.
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
      return res;
    }).catch(() => caches.match("/")))
  );
});
"""


# --------------------------------------------------------------------------- #
# Dashboard page
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>HomePod Logger</title>

<!-- PWA -->
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#eef0f4" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#060709" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="HomePod Log">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">

<!-- Vendored Chart.js (no internet access required) -->
<script src="/static/chart.umd.min.js"></script>
<style>
  /* "Liquid glass" look: translucent, blurred surfaces floating over a soft
     tinted backdrop. --surface stays opaque — the canvas halos and tooltips
     need a solid color; the cards use the --glass tokens. */
  :root {
    color-scheme: light dark;
    --page:#eef0f4; --surface:#fcfcfb;
    --text-1:#0b0b0b; --text-2:#52514e; --muted:#7d7b76;
    --grid:rgba(11,11,11,.08); --baseline:#c3c2b7; --border:rgba(11,11,11,.10);
    --s1:#2a78d6; --s2:#1baf7a; --s3:#eda100; --s4:#008300;
    --s5:#4a3aa7; --s6:#e34948; --s7:#e87ba4; --s8:#eb6834;
    --good:#0ca30c; --warning:#c98500; --serious:#d03b3b;
    --glass:rgba(255,255,255,.55); --glass-strong:rgba(255,255,255,.85);
    --glass-border:rgba(255,255,255,.75); --glass-chip:rgba(255,255,255,.45);
    --glass-hi:inset 0 1px 0 rgba(255,255,255,.9), inset 0 -1px 0 rgba(11,11,11,.03);
    --shadow:0 1px 2px rgba(20,30,60,.05), 0 10px 34px rgba(20,30,60,.09);
    --blob1:rgba(42,120,214,.30); --blob2:rgba(27,175,122,.22); --blob3:rgba(235,104,52,.18);
    --spring:cubic-bezier(.34,1.4,.64,1);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --page:#060709; --surface:#1a1a19;
      --text-1:#fff; --text-2:#c3c2b7; --muted:#8e8c86;
      --grid:rgba(255,255,255,.08); --baseline:#383835; --border:rgba(255,255,255,.10);
      --s1:#3987e5; --s2:#199e70; --s3:#c98500; --s4:#008300;
      --s5:#9085e9; --s6:#e66767; --s7:#d55181; --s8:#d95926;
      --good:#0ca30c; --warning:#fab219; --serious:#e0554f;
      --glass:rgba(38,40,46,.50); --glass-strong:rgba(90,94,104,.55);
      --glass-border:rgba(255,255,255,.12); --glass-chip:rgba(255,255,255,.05);
      --glass-hi:inset 0 1px 0 rgba(255,255,255,.10);
      --shadow:0 10px 34px rgba(0,0,0,.45);
      --blob1:rgba(57,135,229,.28); --blob2:rgba(25,158,112,.18); --blob3:rgba(144,133,233,.18);
    }
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html { -webkit-text-size-adjust:100%; overflow-x:hidden; background:var(--page); }
  body {
    font-family: -apple-system, system-ui, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
    margin:0; background:var(--page); color:var(--text-1);
    padding: 0 16px calc(env(safe-area-inset-bottom) + 24px);
    max-width:1040px; margin-inline:auto; line-height:1.4; overflow-x:hidden;
    -webkit-font-smoothing:antialiased;
  }
  /* Soft colored light behind the glass (fixed, so it doesn't scroll). */
  body::before {
    content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
    background:
      radial-gradient(60vmax 45vmax at 5% 0%, var(--blob1), transparent 70%),
      radial-gradient(50vmax 40vmax at 100% 35%, var(--blob2), transparent 70%),
      radial-gradient(55vmax 45vmax at 20% 100%, var(--blob3), transparent 70%),
      var(--page);
  }
  canvas { max-width:100%; }

  /* Shared glass material. */
  .glass, .tile, .card, .ranges, .ios-hint {
    background:var(--glass);
    -webkit-backdrop-filter:blur(26px) saturate(180%);
    backdrop-filter:blur(26px) saturate(180%);
    border:1px solid var(--glass-border);
    box-shadow:var(--glass-hi), var(--shadow);
  }

  header {
    position:sticky; top:0; z-index:20;
    display:flex; align-items:center; justify-content:space-between;
    gap:12px; margin:0 -16px 16px;
    padding: calc(max(env(safe-area-inset-top), 12px) + 4px) 16px 12px;
    background:var(--glass);
    -webkit-backdrop-filter:blur(26px) saturate(180%);
    backdrop-filter:blur(26px) saturate(180%);
    border-bottom:1px solid var(--glass-border);
  }
  .brand { display:flex; align-items:center; gap:10px; }
  .brand img { width:34px; height:34px; border-radius:9px; box-shadow:0 2px 8px rgba(0,0,0,.12); }
  .brand h1 { font-size:1.15rem; margin:0; font-weight:700; letter-spacing:-.02em; }
  .brand .sub { font-size:.72rem; color:var(--muted); }
  .refresh {
    display:flex; align-items:center; gap:6px; font-size:.78rem; color:var(--text-2);
    cursor:pointer; user-select:none;
  }
  .refresh input { accent-color:var(--s1); width:16px; height:16px; }

  /* Live tiles ----------------------------------------------------------- */
  .tiles { display:grid; gap:12px; grid-template-columns:1fr; margin-bottom:20px; }
  @media (min-width:560px){ .tiles { grid-template-columns:1fr 1fr; } }
  .tile { border-radius:26px; padding:18px; min-width:0; }
  .tile .top { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:12px; }
  .tile .name { display:flex; align-items:center; gap:8px; font-weight:600; font-size:.95rem; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .dot { width:11px; height:11px; border-radius:50%; flex:none; }
  .chip {
    display:inline-flex; align-items:center; gap:5px; font-size:.72rem; font-weight:600;
    padding:3px 10px; border-radius:999px; border:1px solid var(--glass-border);
    background:var(--glass-chip); white-space:nowrap;
  }
  .chip .ico { font-size:.8em; }
  .metrics { display:flex; gap:22px; }
  .metric .k { font-size:.72rem; color:var(--muted); margin-bottom:2px; }
  .metric .v { font-size:2.2rem; font-weight:650; letter-spacing:-.03em; line-height:1; }
  .metric .v small { font-size:.9rem; font-weight:500; color:var(--text-2); }
  .tile .when { margin-top:12px; font-size:.72rem; color:var(--muted); }
  .tile .when.stale { color:var(--serious); font-weight:600; }

  /* Segmented controls: a glass capsule with a sliding "lens" thumb -------- */
  .controls {
    display:flex; align-items:center; justify-content:space-between;
    gap:12px 16px; flex-wrap:wrap; margin-bottom:16px;
  }
  .ranges {
    position:relative; display:inline-flex; border-radius:999px; padding:4px; gap:2px;
  }
  .ranges .thumb {
    position:absolute; top:4px; bottom:4px; left:0; width:0; border-radius:999px;
    background:linear-gradient(180deg, rgba(255,255,255,.28), rgba(255,255,255,0) 60%), var(--s1);
    box-shadow:0 4px 14px rgba(42,120,214,.35), inset 0 1px 0 rgba(255,255,255,.4);
    transition:transform .45s var(--spring), width .45s var(--spring);
    pointer-events:none;
  }
  .ranges button {
    position:relative; z-index:1;
    font:inherit; font-size:.84rem; font-weight:600; border:0; background:transparent;
    color:var(--text-2); padding:8px 15px; border-radius:999px; cursor:pointer;
    transition:color .25s, transform .2s var(--spring);
  }
  .ranges button:active { transform:scale(.94); }
  .ranges button.active { color:#fff; }

  /* Outdoor-weather toggle (only shown when the server has it enabled) ---- */
  .wx-toggle {
    display:none; align-items:center; gap:7px; font-size:.78rem;
    color:var(--text-2); cursor:pointer; user-select:none;
  }
  .wx-toggle input { accent-color:var(--s1); width:16px; height:16px; }
  .wx-swatch {
    width:15px; height:11px; border-radius:3px; flex:none;
    background:var(--grid); border:1px solid var(--baseline);
  }

  /* Chart cards ---------------------------------------------------------- */
  .card { border-radius:26px; padding:18px; margin-bottom:16px; min-width:0; }
  .card h2 { font-size:1rem; margin:0 0 2px; font-weight:650; letter-spacing:-.01em; }
  .card .sub { font-size:.74rem; color:var(--muted); }
  .legend { display:flex; flex-wrap:wrap; gap:14px 18px; margin:10px 0 14px; }
  .legend .item { display:flex; align-items:center; gap:8px; font-size:.78rem; }
  .legend .item .lbl { font-weight:600; color:var(--text-1); }
  .legend .item .stat { color:var(--muted); font-variant-numeric:tabular-nums; }
  .chart-wrap { position:relative; height:280px; }
  @media (max-width:500px){
    .chart-wrap { height:240px; }
    /* Reclaim horizontal space so the charts are wider on phones. */
    body { padding-left:10px; padding-right:10px; }
    header { margin-inline:-10px; padding-inline:14px; }
    .card { padding:16px 12px; }
  }

  /* Daily-average history: one month per page, swipe left/right ----------- */
  .month-nav {
    display:flex; align-items:center; justify-content:space-between; gap:8px;
    margin:14px 0 10px;
  }
  .month-nav .lbl { font-weight:650; font-size:.95rem; text-transform:capitalize; }
  .month-nav .lbl small { display:block; font-size:.7rem; font-weight:500; color:var(--muted); text-transform:none; }
  .navbtn {
    width:36px; height:36px; border-radius:50%; flex:none; cursor:pointer;
    font:inherit; font-size:1.2rem; line-height:1; color:var(--text-1);
    background:var(--glass-chip); border:1px solid var(--glass-border);
    box-shadow:var(--glass-hi);
    transition:transform .2s var(--spring), opacity .2s;
  }
  .navbtn:active { transform:scale(.9); }
  .navbtn:disabled { opacity:.3; cursor:default; }
  .months {
    display:flex; overflow-x:auto; overflow-y:hidden;
    scroll-snap-type:x mandatory; scroll-behavior:smooth;
    overscroll-behavior-x:contain; scrollbar-width:none;
    -webkit-overflow-scrolling:touch;
  }
  .months::-webkit-scrollbar { display:none; }
  .month { flex:0 0 100%; min-width:0; scroll-snap-align:start; scroll-snap-stop:always; }
  .wk, .cal { display:grid; grid-template-columns:repeat(7, minmax(0, 1fr)); gap:6px; }
  .wk { margin-bottom:6px; }
  .wk span { text-align:center; font-size:.66rem; font-weight:600; color:var(--muted); text-transform:uppercase; }
  .day {
    display:flex; flex-direction:column; align-items:center; gap:3px;
    padding:6px 2px 7px; border-radius:14px; min-width:0;
    background:var(--glass-chip); border:1px solid var(--glass-border);
  }
  .day.empty { background:transparent; border-color:transparent; }
  .day.today { border-color:var(--s1); box-shadow:0 0 0 1px var(--s1) inset; }
  .day .d { font-size:.64rem; color:var(--muted); font-variant-numeric:tabular-nums; }
  .day .t { font-size:.82rem; font-weight:650; font-variant-numeric:tabular-nums; letter-spacing:-.02em; white-space:nowrap; }
  .day.empty .t { color:var(--muted); font-weight:400; }
  .day .bar { width:60%; max-width:26px; height:4px; border-radius:2px; }
  .mdots { display:flex; justify-content:center; gap:6px; margin-top:12px; }
  .mdots span {
    width:6px; height:6px; border-radius:3px; background:var(--muted); opacity:.35;
    transition:width .35s var(--spring), opacity .25s;
  }
  .mdots span.on { width:18px; opacity:.9; }

  .status-line { font-size:.74rem; color:var(--muted); text-align:center; margin-top:4px; }
  .ios-hint {
    display:none; font-size:.78rem; color:var(--text-2);
    border-radius:18px; padding:10px 14px; margin-bottom:16px;
  }
  .ios-hint b { color:var(--text-1); }
  @media (prefers-reduced-motion: reduce) {
    .ranges .thumb, .mdots span, .navbtn, .ranges button { transition:none; }
    .months { scroll-behavior:auto; }
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <img src="/static/icon-192.png" alt="">
    <div>
      <h1>HomePod Logger</h1>
      <div class="sub">Temperature &amp; humidity</div>
    </div>
  </div>
  <label class="refresh"><input type="checkbox" id="auto" checked> auto</label>
</header>

<div class="ios-hint" id="iosHint">
  📲 Tip: <b>Share</b> → <b>Add to Home Screen</b> to install the app.
</div>

<section class="tiles" id="tiles"></section>

<div class="controls">
  <div class="ranges" id="ranges">
    <button data-range="24h" class="active">24h</button>
    <button data-range="3d">3d</button>
    <button data-range="7d">7d</button>
    <button data-range="30d">30d</button>
    <button data-range="all">All</button>
  </div>
  <div class="ranges" id="modes">
    <button data-mode="pods">Pods</button>
    <button data-mode="avg">Average</button>
  </div>
  <label class="wx-toggle" id="wxToggle">
    <input type="checkbox" id="wx" checked>
    <span class="wx-swatch"></span> Outdoor weather
  </label>
</div>

<div class="card">
  <h2>Temperature</h2>
  <div class="legend" id="legendTemp"></div>
  <div class="chart-wrap"><canvas id="tempChart"></canvas></div>
</div>
<div class="card">
  <h2>Humidity</h2>
  <div class="legend" id="legendHum"></div>
  <div class="chart-wrap"><canvas id="humChart"></canvas></div>
</div>
<div class="card">
  <h2>Today vs yesterday</h2>
  <div class="sub">Apartment average temperature, by time of day</div>
  <div class="legend" id="legendCmp"></div>
  <div class="chart-wrap"><canvas id="cmpChart"></canvas></div>
</div>
<div class="card" id="dailyCard" style="display:none">
  <h2>Daily average</h2>
  <div class="sub">Apartment mean temperature, one figure per day · swipe for other months</div>
  <div class="month-nav">
    <button class="navbtn" id="mPrev" aria-label="Previous month">‹</button>
    <span class="lbl" id="mLabel"></span>
    <button class="navbtn" id="mNext" aria-label="Next month">›</button>
  </div>
  <div class="months" id="months"></div>
  <div class="mdots" id="mdots"></div>
</div>

<p class="status-line" id="status">Loading…</p>

<script>
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const SERIES_VARS = ["--s1","--s2","--s3","--s4","--s5","--s6","--s7","--s8"];
const HOUR = 3600e3;
let currentRange = "24h";
let colorMap = {};           // homepod -> color (follows the entity, never rank)
let tempChart, humChart, cmpChart, timer = null;
// Outdoor-weather overlay is on unless the user unticked it (persisted).
let showWeather = localStorage.getItem("showWeather") !== "0";
// What the main charts plot: each pod, or the apartment average (persisted).
let displayMode = localStorage.getItem("displayMode") === "avg" ? "avg" : "pods";

const fmtT = (v) => v.toFixed(1) + "°";
const fmtH = (v) => Math.round(v) + "%";
// Axis ticks: Chart.js steps in floats (23.8 + 0.1 → 23.900000000000006), so
// round to 2 decimals and let Number() drop the trailing zeros.
const tickFmt = (unit) => (v) => Number(v.toFixed(2)) + unit;

// Translucent rgba from a #rrggbb / #rgb token (for the subtle area fill).
function rgba(hex, a) {
  hex = hex.replace("#", "");
  if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
  const n = parseInt(hex, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
function isStale(iso) { return (Date.now() - new Date(iso).getTime()) / 1000 > 7200; }

// Comfort: returns {v, ico, lbl} — status carried by icon + label, never color alone.
function comfort(t, h) {
  const st = (t >= 19 && t <= 25) ? 0 : (t >= 16 && t <= 28) ? 1 : 2;
  const sh = (h >= 40 && h <= 60) ? 0 : (h >= 30 && h <= 70) ? 1 : 2;
  const worst = Math.max(st, sh);
  return [
    { v: "var(--good)",    ico: "✓", lbl: "Comfort" },
    { v: "var(--warning)", ico: "•", lbl: "OK" },
    { v: "var(--serious)", ico: "▲", lbl: "Out of range" },
  ][worst];
}

function assignColors(homepods) {
  homepods.forEach((h, i) => {
    if (!(h in colorMap)) colorMap[h] = css(SERIES_VARS[i % SERIES_VARS.length]);
  });
}

function renderTiles(items) {
  const el = document.getElementById("tiles");
  if (!items.length) { el.innerHTML = '<div class="tile">No data yet.</div>'; return; }
  el.innerHTML = items.map((r) => {
    const c = comfort(r.temp, r.humidity);
    const stale = isStale(r.ts);
    return `
      <div class="tile">
        <div class="top">
          <span class="name"><span class="dot" style="background:${colorMap[r.homepod]}"></span>${r.homepod}</span>
          <span class="chip" style="color:${c.v}"><span class="ico">${c.ico}</span>${c.lbl}</span>
        </div>
        <div class="metrics">
          <div class="metric"><div class="k">Temperature</div><div class="v">${fmtT(r.temp)}<small>C</small></div></div>
          <div class="metric"><div class="k">Humidity</div><div class="v">${Math.round(r.humidity)}<small>%</small></div></div>
        </div>
        <div class="when ${stale ? "stale" : ""}">${ago(r.ts)}</div>
      </div>`;
  }).join("");
}

// Plugin: label the last value at the line end (mark spec: value at the end).
const endLabel = {
  id: "endLabel",
  afterDatasetsDraw(chart) {
    const { ctx } = chart;
    chart.data.datasets.forEach((ds, i) => {
      if (!ds._fmt) return;                  // reference series: no end label
      const meta = chart.getDatasetMeta(i);
      if (meta.hidden || !meta.data.length) return;
      const p = meta.data[meta.data.length - 1];
      const txt = ds._fmt(ds.data[ds.data.length - 1].y);
      ctx.save();
      ctx.font = "600 12px system-ui, -apple-system, sans-serif";
      ctx.textBaseline = "middle";
      ctx.lineWidth = 3; ctx.strokeStyle = css("--surface");
      ctx.strokeText(txt, p.x + 8, p.y);      // surface halo for legibility
      ctx.fillStyle = css("--text-1");
      ctx.fillText(txt, p.x + 8, p.y);
      ctx.restore();
    });
  }
};

// Plugin: vertical crosshair line under the tooltip.
const crosshair = {
  id: "crosshair",
  afterDraw(chart) {
    const t = chart.tooltip;
    if (!t || !t.getActiveElements || !t.getActiveElements().length) return;
    const x = t.getActiveElements()[0].element.x;
    const { top, bottom } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom);
    ctx.lineWidth = 1; ctx.strokeStyle = css("--baseline"); ctx.stroke();
    ctx.restore();
  }
};

// Fixed, hour-aligned x windows on the short ranges so the axis reads the same
// whenever you open the app, instead of drifting with the newest reading.
function xBounds() {
  const spanH = { "24h": 24, "3d": 72 }[currentRange];
  if (!spanH) return { min: undefined, max: undefined };
  const max = Math.ceil(Date.now() / HOUR) * HOUR;
  return { min: max - spanH * HOUR, max };
}

// Fixed local-time ticks: 24h → every 3h, 3d → every 12h, 7d → every midnight.
// 30d/all keep Chart.js's data-driven ticks. Date arithmetic is DST-safe.
function fixedTicks(min, max) {
  const stepH = { "24h": 3, "3d": 12, "7d": 24 }[currentRange];
  if (!stepH || min == null || max == null) return null;
  const d = new Date(min);
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() - (d.getHours() % stepH));
  const out = [];
  while (d.getTime() <= max) {
    if (d.getTime() >= min) out.push({ value: d.getTime() });
    d.setHours(d.getHours() + stepH);
  }
  return out;
}

function baseConfig(fmt, unit) {
  const adaptTick = (v) => {
    const d = new Date(v);
    if (currentRange === "24h")
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (currentRange === "3d")
      return d.getHours() === 0
        ? d.toLocaleDateString([], { weekday: "short", day: "2-digit" })
        : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString([], { day: "2-digit", month: "2-digit" });
  };
  return {
    type: "line",
    data: { datasets: [] },
    options: {
      responsive: true, maintainAspectRatio: false,
      layout: { padding: { right: 44 } },     // room for the end label
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          type: "linear",
          afterBuildTicks(axis) {
            const t = fixedTicks(axis.min, axis.max);
            if (t) axis.ticks = t;
          },
          // Colors are scriptable (re-read each render) so a light/dark theme
          // switch is always reflected — otherwise the grid keeps the value it
          // had when the chart was built (a light grid looks white on dark).
          grid: { color: () => css("--grid"), drawTicks: false },
          border: { color: () => css("--baseline") },
          ticks: { color: () => css("--muted"), maxRotation: 0, autoSkip: true,
                   maxTicksLimit: 9, font: { size: 11 }, callback: adaptTick }
        },
        y: {
          grid: { color: () => css("--grid"), drawTicks: false },
          border: { display: false },
          ticks: { color: () => css("--muted"), font: { size: 11 },
                   maxTicksLimit: 7, callback: tickFmt(unit) }
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: () => css("--surface"), titleColor: () => css("--text-2"),
          bodyColor: () => css("--text-1"), borderColor: () => css("--border"), borderWidth: 1,
          padding: 10, cornerRadius: 10, displayColors: true, usePointStyle: true,
          callbacks: {
            title: (it) => new Date(it[0].parsed.x).toLocaleString([],
              { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }),
            label: (it) => "  " + it.dataset.label + ": " + fmt(it.parsed.y)
          }
        }
      }
    },
    plugins: [endLabel, crosshair]
  };
}

function lineDataset(label, color, points, fmt) {
  return {
    label, data: points, _fmt: fmt,
    borderColor: color, backgroundColor: color,
    tension: .25, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5,
    pointHoverBorderColor: css("--surface"), pointHoverBorderWidth: 2, spanGaps: true
  };
}

// Apartment average: bucket to the hour (the ingest cadence), average each pod
// within its bucket first, then average across pods — so a pod that reported
// twice in an hour doesn't weigh more than the other.
function averageSeries(data, homepods, field) {
  const buckets = new Map();               // hour -> { pod: [values] }
  homepods.forEach((name) => (data[name] || []).forEach((p) => {
    const k = Math.round(p.x / HOUR) * HOUR;
    const m = buckets.get(k) || {};
    (m[name] = m[name] || []).push(p[field]);
    buckets.set(k, m);
  }));
  return [...buckets.keys()].sort((a, b) => a - b).map((k) => {
    const pods = Object.values(buckets.get(k))
      .map((v) => v.reduce((a, c) => a + c, 0) / v.length);
    return { x: k, y: pods.reduce((a, c) => a + c, 0) / pods.length };
  });
}

function datasets(data, homepods, field, fmt) {
  if (displayMode === "avg")
    return [lineDataset("Average", css("--s1"),
                        averageSeries(data, homepods, field), fmt)];
  return homepods.map((name) => lineDataset(
    name, colorMap[name], data[name].map((p) => ({ x: p.x, y: p[field] })), fmt));
}

// Outdoor weather as a recessive gray area *behind* the indoor lines. It is
// context, not an entity, so it never takes a series color: muted fill, thin
// dashed border, higher draw order (drawn first → underneath), no end label.
function weatherDataset(weather, field) {
  return {
    label: "Outdoor",
    data: weather.map((p) => ({ x: p.x, y: p[field] })).filter((p) => p.y != null),
    _weather: true,
    borderColor: css("--baseline"),
    backgroundColor: rgba(css("--muted"), 0.12),
    fill: "start",                 // area from the value down to the axis
    borderWidth: 1, borderDash: [4, 3],
    tension: .3, pointRadius: 0, pointHoverRadius: 0,
    spanGaps: true, order: 10, hidden: !showWeather
  };
}

function renderLegend(elId, data, homepods, field, fmt) {
  const items = displayMode === "avg"
    ? [{ name: "Average", color: css("--s1"),
         vals: averageSeries(data, homepods, field).map((p) => p.y) }]
    : homepods.map((name) => ({ name, color: colorMap[name],
                                vals: data[name].map((p) => p[field]) }));
  document.getElementById(elId).innerHTML = items.filter((i) => i.vals.length)
    .map(({ name, color, vals }) => {
      const mn = Math.min(...vals), mx = Math.max(...vals);
      const av = vals.reduce((a, b) => a + b, 0) / vals.length;
      return `<span class="item">
        <span class="dot" style="background:${color}"></span>
        <span class="lbl">${name}</span>
        <span class="stat">${fmt(mn)} – ${fmt(mx)} · avg ${fmt(av)}</span>
      </span>`;
    }).join("");
}

// --- "Today vs yesterday" — both days on a shared time-of-day axis ---------- //
const fmtHour = (v) => String(Math.round(v / HOUR)).padStart(2, "0") + ":00";

function cmpConfig() {
  return {
    type: "line",
    data: { datasets: [] },
    options: {
      responsive: true, maintainAspectRatio: false,
      layout: { padding: { right: 44 } },
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          type: "linear", min: 0, max: 24 * HOUR,
          grid: { color: () => css("--grid"), drawTicks: false },
          border: { color: () => css("--baseline") },
          ticks: { color: () => css("--muted"), maxRotation: 0, autoSkip: true,
                   maxTicksLimit: 9, stepSize: 3 * HOUR, font: { size: 11 },
                   callback: fmtHour }
        },
        y: {
          grid: { color: () => css("--grid"), drawTicks: false },
          border: { display: false },
          ticks: { color: () => css("--muted"), font: { size: 11 },
                   maxTicksLimit: 7, callback: tickFmt("°") }
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: () => css("--surface"), titleColor: () => css("--text-2"),
          bodyColor: () => css("--text-1"), borderColor: () => css("--border"), borderWidth: 1,
          padding: 10, cornerRadius: 10, displayColors: true, usePointStyle: true,
          callbacks: {
            title: (it) => fmtHour(it[0].parsed.x),
            label: (it) => "  " + it.dataset.label + ": " + fmtT(it.parsed.y)
          }
        }
      }
    },
    plugins: [endLabel, crosshair]
  };
}

// Split the last 48h of apartment-average temperature into today / yesterday,
// re-based to milliseconds since each day's local midnight.
function compareSeries(ser3) {
  const avg = averageSeries(ser3.data, ser3.homepods, "temp");
  const mid = new Date(); mid.setHours(0, 0, 0, 0);
  const t0 = mid.getTime();
  const ym = new Date(mid); ym.setDate(ym.getDate() - 1);
  const y0 = ym.getTime();
  const today = [], yest = [];
  avg.forEach((p) => {
    if (p.x >= t0) today.push({ x: p.x - t0, y: p.y });
    else if (p.x >= y0) yest.push({ x: p.x - y0, y: p.y });
  });
  return { today, yest };
}

function renderCompare(ser3) {
  const { today, yest } = compareSeries(ser3);
  cmpChart.data.datasets = [
    lineDataset("Today", css("--s1"), today, fmtT),
    { label: "Yesterday", data: yest,
      borderColor: css("--muted"), backgroundColor: css("--muted"),
      borderWidth: 1.5, borderDash: [5, 4], tension: .25,
      pointRadius: 0, pointHoverRadius: 4, spanGaps: true, order: 5 }
  ];
  cmpChart.update();

  const el = document.getElementById("legendCmp");
  if (!today.length || !yest.length) {
    el.innerHTML = '<span class="item"><span class="stat">Needs data for both today and yesterday.</span></span>';
    return;
  }
  const mean = (a) => a.reduce((s, p) => s + p.y, 0) / a.length;
  // Fair Δ: compare today with yesterday *up to the same time of day*.
  const nowX = today[today.length - 1].x;
  const yestSoFar = yest.filter((p) => p.x <= nowX);
  const ref = yestSoFar.length ? yestSoFar : yest;
  const d = mean(today) - mean(ref);
  el.innerHTML = `
    <span class="item"><span class="dot" style="background:${css("--s1")}"></span>
      <span class="lbl">Today</span><span class="stat">avg ${fmtT(mean(today))}</span></span>
    <span class="item"><span class="dot" style="background:${css("--muted")}"></span>
      <span class="lbl">Yesterday</span><span class="stat">avg ${fmtT(mean(ref))} to this hour</span></span>
    <span class="item"><span class="lbl">Δ</span>
      <span class="stat">${d >= 0 ? "+" : "−"}${Math.abs(d).toFixed(1)}° vs same hours yesterday</span></span>`;
}

// --- Daily-average history — one figure per day ------------------------------ //
// Magnitude → color bin. The number itself stays in ink; the bar carries the
// scale (blue cold → teal cool → green comfort → amber warm → red hot).
function tempBin(t) {
  if (t < 16) return css("--s1");
  if (t < 19) return css("--s2");
  if (t <= 25) return css("--good");
  if (t <= 28) return css("--warning");
  return css("--serious");
}

const dayKey = (d) => d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
let dailyCache = null;      // { at, ser } — full history, refetched every 10 min
let dailyMonths = [];       // month keys (yyyymm) of the rendered pages
let dailyMonth = null;      // month the user is looking at (null → latest)

// Per-day apartment mean: average each pod within the day, then across pods.
function dailyAverages(data, homepods) {
  const days = new Map();                  // local day -> { pod: [temps] }
  homepods.forEach((name) => (data[name] || []).forEach((p) => {
    const k = dayKey(new Date(p.x));
    const m = days.get(k) || {};
    (m[name] = m[name] || []).push(p.temp);
    days.set(k, m);
  }));
  const out = new Map();
  days.forEach((m, k) => {
    const pods = Object.values(m).map((v) => v.reduce((a, c) => a + c, 0) / v.length);
    out.set(k, pods.reduce((a, c) => a + c, 0) / pods.length);
  });
  return out;
}

// One page per month, laid out as a Monday-first calendar; pages sit side by
// side in a scroll-snap strip so they swipe left/right natively.
function renderDaily(data, homepods) {
  const card = document.getElementById("dailyCard");
  const avgs = dailyAverages(data, homepods);
  if (!avgs.size) { card.style.display = "none"; return; }
  card.style.display = "";

  const keys = [...avgs.keys()].sort((a, b) => a - b);
  const todayK = dayKey(new Date());
  const lastK = Math.max(keys[keys.length - 1], todayK);
  dailyMonths = [];
  for (let ym = Math.floor(keys[0] / 100); ym <= Math.floor(lastK / 100);
       ym = ym % 100 === 12 ? ym + 89 : ym + 1)
    dailyMonths.push(ym);

  // Narrow weekday initials, Monday first (2024-01-01 was a Monday).
  const wk = Array.from({ length: 7 }, (_, i) =>
    `<span>${new Date(2024, 0, 1 + i).toLocaleDateString([], { weekday: "narrow" })}</span>`).join("");

  const el = document.getElementById("months");
  el.innerHTML = dailyMonths.map((ym) => {
    const y = Math.floor(ym / 100), m = ym % 100;
    const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7;
    const n = new Date(y, m, 0).getDate();
    let cells = "<span></span>".repeat(lead);
    for (let d = 1; d <= n; d++) {
      const k = ym * 100 + d, v = avgs.get(k);
      const today = k === todayK ? " today" : "";
      cells += v == null
        ? `<span class="day empty${today}"><span class="d">${d}</span><span class="t">–</span></span>`
        : `<span class="day${today}"><span class="d">${d}</span>
             <span class="t">${v.toFixed(1)}°</span>
             <span class="bar" style="background:${tempBin(v)}"></span></span>`;
    }
    return `<section class="month"><div class="wk">${wk}</div><div class="cal">${cells}</div></section>`;
  }).join("");

  document.getElementById("mdots").innerHTML =
    dailyMonths.length > 1 && dailyMonths.length <= 12 ? "<span></span>".repeat(dailyMonths.length) : "";

  paintMonth(snapMonth(), avgs);
}

// Jump (no animation) to the month the user was on — kept across refreshes —
// or to the latest one, which also follows a new month when it starts.
function snapMonth() {
  const el = document.getElementById("months");
  let idx = dailyMonths.indexOf(dailyMonth);
  if (idx < 0) idx = dailyMonths.length - 1;
  el.style.scrollBehavior = "auto";
  el.scrollLeft = idx * el.clientWidth;
  el.style.scrollBehavior = "";
  return idx;
}

let lastAvgs = new Map();
function paintMonth(idx, avgs) {
  if (avgs) lastAvgs = avgs;
  const ym = dailyMonths[idx];
  if (ym == null) return;
  const y = Math.floor(ym / 100), m = ym % 100;
  const vals = [...lastAvgs.entries()].filter(([k]) => Math.floor(k / 100) === ym).map(([, v]) => v);
  const mean = vals.length ? vals.reduce((a, c) => a + c, 0) / vals.length : null;
  const name = new Date(y, m - 1, 1).toLocaleDateString([], { month: "long", year: "numeric" });
  document.getElementById("mLabel").innerHTML = `${name}<small>${
    mean == null ? "no data" : `avg ${fmtT(mean)} · ${vals.length} day${vals.length > 1 ? "s" : ""}`}</small>`;
  document.getElementById("mPrev").disabled = idx <= 0;
  document.getElementById("mNext").disabled = idx >= dailyMonths.length - 1;
  document.querySelectorAll("#mdots span").forEach((d, i) => d.classList.toggle("on", i === idx));
}

function monthIndex() {
  const el = document.getElementById("months");
  return el.clientWidth ? Math.round(el.scrollLeft / el.clientWidth) : 0;
}

function goMonth(delta) {
  const el = document.getElementById("months");
  const idx = Math.min(Math.max(monthIndex() + delta, 0), dailyMonths.length - 1);
  el.scrollTo({ left: idx * el.clientWidth });
}

// Segmented controls: slide the glass "lens" under the active button.
function paintThumb(group) {
  let th = group.querySelector(".thumb");
  if (!th) {
    th = document.createElement("span");
    th.className = "thumb";
    th.style.transition = "none";          // first placement doesn't animate
    group.prepend(th);
    requestAnimationFrame(() => requestAnimationFrame(() => { th.style.transition = ""; }));
  }
  const a = group.querySelector("button.active");
  th.style.width = a ? a.offsetWidth + "px" : "0";
  th.style.transform = `translateX(${a ? a.offsetLeft : 0}px)`;
}
const paintThumbs = () => document.querySelectorAll(".ranges").forEach(paintThumb);

async function refresh() {
  const status = document.getElementById("status");
  try {
    // The compare card always needs the last 48h; reuse the main fetch when the
    // selected range already covers it.
    // The daily history needs the whole record; it changes slowly, so it is
    // refetched at most every 10 min (or taken from the "All" range).
    const needDaily = currentRange !== "all" &&
      (!dailyCache || Date.now() - dailyCache.at > 10 * 60e3);
    const [latRes, serRes, ser3Res, allRes] = await Promise.all([
      fetch("/api/latest"),
      fetch(`/api/series?range=${currentRange}`),
      currentRange === "3d" ? null : fetch("/api/series?range=3d"),
      needDaily ? fetch("/api/series?range=all") : null
    ]);
    const latest = (await latRes.json()).homepods || [];
    const ser = await serRes.json();
    const ser3 = ser3Res ? await ser3Res.json() : ser;
    if (currentRange === "all") dailyCache = { at: Date.now(), ser };
    else if (allRes) dailyCache = { at: Date.now(), ser: await allRes.json() };
    const homepods = ser.homepods;

    assignColors([...new Set([...latest.map((r) => r.homepod), ...homepods])].sort());
    renderTiles(latest);

    // Outdoor overlay: only when the server has it configured and has data.
    const wx = ser.weather || [];
    const wxOn = !!ser.weather_enabled && wx.length > 0;
    document.getElementById("wxToggle").style.display =
      ser.weather_enabled ? "inline-flex" : "none";

    const b = xBounds();
    [tempChart, humChart].forEach((ch) => {
      ch.options.scales.x.min = b.min;
      ch.options.scales.x.max = b.max;
    });
    tempChart.data.datasets = datasets(ser.data, homepods, "temp", fmtT)
      .concat(wxOn ? [weatherDataset(wx, "temp")] : []);
    humChart.data.datasets = datasets(ser.data, homepods, "humidity", fmtH)
      .concat(wxOn ? [weatherDataset(wx, "humidity")] : []);
    tempChart.update(); humChart.update();
    renderLegend("legendTemp", ser.data, homepods, "temp", fmtT);
    renderLegend("legendHum", ser.data, homepods, "humidity", fmtH);
    renderCompare(ser3);
    renderDaily(dailyCache.ser.data, dailyCache.ser.homepods);

    const total = homepods.reduce((n, h) => n + ser.data[h].length, 0);
    status.textContent = homepods.length
      ? `${total} points · updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
      : "No data for this range.";
  } catch (e) {
    status.textContent = "Load error: " + e.message;
  }
}

function setRange(r, btn) {
  currentRange = r;
  document.querySelectorAll("#ranges button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  paintThumbs();
  refresh();
}

window.addEventListener("DOMContentLoaded", () => {
  // Deep link: /#range=7d&mode=avg preselects a view (bookmarkable).
  const hash = new URLSearchParams(location.hash.slice(1));
  const hr = hash.get("range"), hm = hash.get("mode");
  if (hr && document.querySelector(`#ranges [data-range="${hr}"]`)) {
    currentRange = hr;
    document.querySelectorAll("#ranges button").forEach((b) =>
      b.classList.toggle("active", b.dataset.range === hr));
  }
  if (hm === "avg" || hm === "pods") displayMode = hm;

  tempChart = new Chart(document.getElementById("tempChart"), baseConfig(fmtT, "°"));
  humChart = new Chart(document.getElementById("humChart"), baseConfig(fmtH, "%"));
  cmpChart = new Chart(document.getElementById("cmpChart"), cmpConfig());

  // Touch: show the tooltip only while a finger is down, then dismiss it —
  // otherwise the panel sticks around after a tap and eats the screen.
  const dismiss = (ch) => {
    ch.setActiveElements([]);
    if (ch.tooltip) ch.tooltip.setActiveElements([], { x: 0, y: 0 });
    ch.update();
  };
  [tempChart, humChart, cmpChart].forEach((ch) => {
    ch.canvas.addEventListener("touchend", () => dismiss(ch), { passive: true });
    ch.canvas.addEventListener("touchcancel", () => dismiss(ch), { passive: true });
  });

  document.querySelectorAll("#ranges button").forEach((btn) =>
    btn.addEventListener("click", () => setRange(btn.dataset.range, btn)));

  // Pods / Average display mode (persisted).
  const modeBtns = document.querySelectorAll("#modes button");
  const paintMode = () => {
    modeBtns.forEach((b) => b.classList.toggle("active", b.dataset.mode === displayMode));
    paintThumbs();
  };
  paintMode();
  modeBtns.forEach((btn) => btn.addEventListener("click", () => {
    displayMode = btn.dataset.mode;
    localStorage.setItem("displayMode", displayMode);
    paintMode();
    refresh();
  }));

  // Daily history: swipe (native scroll-snap) or the ‹ › buttons.
  const monthsEl = document.getElementById("months");
  let curMonth = -1;
  monthsEl.addEventListener("scroll", () => {
    const i = monthIndex();
    if (i === curMonth) return;
    curMonth = i;
    dailyMonth = i < dailyMonths.length - 1 ? dailyMonths[i] : null;
    paintMonth(i);
  }, { passive: true });
  document.getElementById("mPrev").addEventListener("click", () => goMonth(-1));
  document.getElementById("mNext").addEventListener("click", () => goMonth(1));
  window.addEventListener("resize", () => {
    paintThumbs();
    if (dailyMonths.length) snapMonth();
  });
  if (document.fonts) document.fonts.ready.then(paintThumbs);

  const auto = document.getElementById("auto");
  const startTimer = () => { timer = setInterval(refresh, 60000); };
  auto.addEventListener("change", (e) => {
    if (e.target.checked) startTimer(); else { clearInterval(timer); timer = null; }
  });
  if (auto.checked) startTimer();

  // Outdoor-weather toggle: flip visibility in place, no refetch, and persist.
  const wxBox = document.getElementById("wx");
  wxBox.checked = showWeather;
  wxBox.addEventListener("change", (e) => {
    showWeather = e.target.checked;
    localStorage.setItem("showWeather", showWeather ? "1" : "0");
    [tempChart, humChart].forEach((ch) => {
      ch.data.datasets.forEach((ds) => { if (ds._weather) ds.hidden = !showWeather; });
      ch.update();
    });
  });

  // iOS install tip (Safari, when not already standalone).
  const isiOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const standalone = window.navigator.standalone || matchMedia("(display-mode: standalone)").matches;
  if (isiOS && !standalone) document.getElementById("iosHint").style.display = "block";

  refresh();
});

// Service worker (PWA / offline).
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
</script>
</body>
</html>
"""
