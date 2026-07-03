#!/usr/bin/env python3
"""Turn on the "HomePod Logger Trigger" virtual switch via the Homebridge UI API.

Called by the server cron (once per hour). Each activation generates the
"accessory controlled" event that the Home app automation listens for to run the
logging Shortcut.

Standard library only (urllib) — nothing to install.

Configuration via environment variables:
    HB_URL          Homebridge UI API base   (e.g. http://192.168.1.75:8581)
    HB_USER         Homebridge UI username
    HB_PASS         Homebridge UI password
    HB_SWITCH_NAME  accessory name           (default: "HomePod Logger Trigger")

Exit 0 = OK, non-zero = failure (visible in cron / User Scripts logs).
"""

import json
import os
import sys
import urllib.error
import urllib.request

HB_URL = os.environ.get("HB_URL", "http://192.168.1.75:8581").rstrip("/")
HB_USER = os.environ.get("HB_USER", "")
HB_PASS = os.environ.get("HB_PASS", "")
SWITCH_NAME = os.environ.get("HB_SWITCH_NAME", "HomePod Logger Trigger")
TIMEOUT = 15


def _request(method, path, token=None, body=None):
    url = f"{HB_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"[trigger] {method} {path} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"[trigger] {method} {path} -> unreachable: {e.reason}")


def login():
    resp = _request("POST", "/api/auth/login",
                    body={"username": HB_USER, "password": HB_PASS})
    token = resp.get("access_token") if resp else None
    if not token:
        raise SystemExit("[trigger] login: no access_token in response")
    return token


def find_unique_id(token):
    accessories = _request("GET", "/api/accessories", token=token) or []
    for acc in accessories:
        info = acc.get("serviceCharacteristics") or []
        name = acc.get("serviceName") or acc.get("name")
        # Some versions expose the name only in the characteristics.
        if not name:
            for c in info:
                if c.get("type") == "Name":
                    name = c.get("value")
                    break
        if name == SWITCH_NAME:
            return acc.get("uniqueId")
    raise SystemExit(f"[trigger] accessory not found: {SWITCH_NAME!r}")


def turn_on(token, unique_id):
    _request("PUT", f"/api/accessories/{unique_id}", token=token,
             body={"characteristicType": "On", "value": True})


def main():
    if not HB_USER or not HB_PASS:
        raise SystemExit("[trigger] HB_USER / HB_PASS not set")
    token = login()
    unique_id = find_unique_id(token)
    turn_on(token, unique_id)
    print(f"[trigger] OK — {SWITCH_NAME} turned on (uniqueId={unique_id})")


if __name__ == "__main__":
    main()
