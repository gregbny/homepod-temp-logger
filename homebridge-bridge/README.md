# Homebridge bridge — virtual switch + cron

Gives the Home app a "trigger" accessory that the server turns on every hour. This
is the bridge between the cadence imposed by the cron and the autonomous automation
running on the HomePod hub.

```
[Unraid cron, hourly] --PUT On=true--> [Homebridge UI API]
        --> ["HomePod Logger Trigger" switch] --> event the Home app listens for
```

## Contents

| File | Role |
|---|---|
| `config.snippet.json` | dummy accessory to add to `config.json` |
| `trigger.py` | turns the switch on via the Homebridge API (stdlib, no dependencies) |

## 1. Install the virtual switch

1. Homebridge UI → **Plugins** → search **`homebridge-dummy`** → install.
2. Homebridge UI → **Config** (JSON mode) → add the block from `config.snippet.json`
   to `accessories`:
   ```json
   {
     "accessory": "DummySwitch",
     "name": "HomePod Logger Trigger",
     "reverse": false,
     "time": 1000,
     "resettable": true
   }
   ```
   - `resettable: true` + `time: 1000` make it a **momentary** switch: it returns to
     *off* by itself after 1s. Each toggle therefore fires a clean "turned on" event
     for the Home automation.
3. **Restart Homebridge.** The `HomePod Logger Trigger` accessory appears in
   Homebridge UI and becomes exposable to the Home app (via the bridge's HomeKit
   code).

## 2. Find your Homebridge API details

`trigger.py` talks to the **homebridge-config-ui-x** API (the Homebridge web UI,
already present on most installs).

- **URL**: `http://SERVER_IP:8581` (default Homebridge UI port — adjust if you
  changed it). If Homebridge runs in **host** network mode, this is just your
  server's IP on port `8581`.
- **Auth**: a Homebridge UI username/password. If the UI runs with authentication
  disabled, still create a user (Settings → Users) so the API accepts a login;
  otherwise adapt the script.

The script finds the accessory's `uniqueId` by its **name** (`HB_SWITCH_NAME`), so
nothing is hardcoded.

## 3. Test manually

```bash
export HB_URL="http://192.168.1.75:8581"
export HB_USER="admin"
export HB_PASS="your-password"
export HB_SWITCH_NAME="HomePod Logger Trigger"

python3 trigger.py
# -> [trigger] OK — HomePod Logger Trigger turned on (uniqueId=...)
```
Confirm in Homebridge UI (or the Home app) that the switch briefly flips on and back
off.

## 4. Install the hourly cron (Unraid)

### Option A — User Scripts plugin (recommended)
1. Apps → install **User Scripts** (Community Apps) if missing.
2. Settings → **User Scripts** → *Add New Script* → name it `homepod-trigger`.
3. Edit the script:
   ```bash
   #!/bin/bash
   export HB_URL="http://192.168.1.75:8581"
   export HB_USER="admin"
   export HB_PASS="your-password"
   export HB_SWITCH_NAME="HomePod Logger Trigger"
   python3 /mnt/user/appdata/homepod-temp-logger/trigger.py
   ```
   (Copy `trigger.py` to a stable path on the array, e.g.
   `/mnt/user/appdata/homepod-temp-logger/trigger.py`.)
4. *Schedule* → **Custom** → hourly cron expression:
   ```
   0 * * * *
   ```
   (minute 0 of every hour).

### Option B — system cron
Add the line (via User Scripts "At Startup" or your `go` file, since Unraid's cron
isn't persistent across reboots):
```
0 * * * * HB_URL=http://192.168.1.75:8581 HB_USER=admin HB_PASS=xxx python3 /mnt/user/appdata/homepod-temp-logger/trigger.py >> /var/log/homepod-trigger.log 2>&1
```

### Shell alternative (curl + jq)
If you prefer pure shell:
```bash
TOKEN=$(curl -s -X POST "$HB_URL/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$HB_USER\",\"password\":\"$HB_PASS\"}" | jq -r .access_token)

UID=$(curl -s "$HB_URL/api/accessories" -H "Authorization: Bearer $TOKEN" \
  | jq -r ".[] | select(.serviceName==\"$HB_SWITCH_NAME\") | .uniqueId")

curl -s -X PUT "$HB_URL/api/accessories/$UID" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"characteristicType":"On","value":true}'
```

## Environment variables

| Variable | Default | Role |
|---|---|---|
| `HB_URL` | `http://192.168.1.75:8581` | Homebridge UI API base |
| `HB_USER` | — | Homebridge UI username (required) |
| `HB_PASS` | — | password (required) |
| `HB_SWITCH_NAME` | `HomePod Logger Trigger` | name of the accessory to turn on |

## Final check

Run the cron once (User Scripts *Run Script*) → the switch flips in the Home app.
The next link (running the Shortcut) is wired up in [`../ios-setup/README.md`](../ios-setup/README.md).
