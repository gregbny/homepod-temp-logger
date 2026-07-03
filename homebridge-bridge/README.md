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

1. Homebridge UI → **Plugins** → search for the **Dummy** plugin → install.
2. On the plugin card, click the **wrench / Settings** to open its **config UI** (do
   **not** hand-edit JSON — let the UI generate the block). Add an accessory:
   - **Name**: `HomePod Logger Trigger`
   - **Type**: `Switch`
   - **Auto Reset**: enabled, `1 second` (TIMEOUT) → this makes it a **momentary**
     switch that returns to *off* by itself after 1s, so each toggle fires a clean
     "turned on" event for the Home automation.
   - Leave history/webhook off.

   The UI writes a `platforms` block like [`config.snippet.json`](config.snippet.json)
   (`"platform": "HomebridgeDummy"`). **Heads-up:** the older accessory form
   (`{"accessory":"DummySwitch", …}`) does **not** produce a usable momentary switch
   here — use the platform form the config UI generates.
3. *(Optional but recommended)* enable **Child Bridge** for this plugin in the UI.
   Homebridge adds a `_bridge` section (its own `username`/`port`) automatically —
   leave whatever it generates.
4. **Restart Homebridge.**

### 1b. Pair it into the Home app (don't skip this)

Homebridge running the accessory is **not** enough — HomeKit won't see the switch
until the bridge is paired:

1. Homebridge UI → **Status** page → the QR code (or the setup code / PIN). If you
   enabled a child bridge in step 3, use **that** bridge's QR (each child bridge has
   its own).
2. **Home** app → **+** → **Add Accessory** → scan the QR (or *More options…* → pick
   the bridge → enter the PIN). Assign it to any room.
3. `HomePod Logger Trigger` now appears in the Home app.

### 1c. Make it selectable for automations (expose it as a *Lamp*)

A plain **Switch** is often **not offered** as a trigger under Home → Automation →
*An Accessory is Controlled*. Exposing the dummy as a **Lightbulb / Lamp** fixes this.
In the Dummy plugin's config UI, set the accessory's service **type to `Lightbulb`
(Lamp)** instead of `Switch`, then restart Homebridge and re-check the Home app — the
accessory now shows as a light and becomes selectable in the automation trigger list.
(Cosmetic-only: it behaves the same; you can hide it later — see step 4c.)

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

> **Getting `trigger.py` onto the server.** The container image does **not** drop
> this file into `appdata` for you — it lives in this repo. The script below
> **auto-downloads it on first run** into `/mnt/user/appdata/homepod-temp-logger/`,
> so you don't have to copy anything by hand. (Prefer to place it manually? On the
> Unraid terminal: `mkdir -p /mnt/user/appdata/homepod-temp-logger && curl -fsSL
> https://raw.githubusercontent.com/gregbny/homepod-temp-logger/main/homebridge-bridge/trigger.py
> -o /mnt/user/appdata/homepod-temp-logger/trigger.py`.)

### Option A — User Scripts plugin (recommended)
1. Apps → install **User Scripts** (Community Apps) if missing.
2. Settings → **User Scripts** → *Add New Script* → name it `homepod-trigger`.
3. Edit the script (self-contained — fetches `trigger.py` if it's not there yet):
   ```bash
   #!/bin/bash
   export HB_URL="http://192.168.1.75:8581"
   export HB_USER="admin"
   export HB_PASS="your-password"
   export HB_SWITCH_NAME="HomePod Logger Trigger"

   SCRIPT=/mnt/user/appdata/homepod-temp-logger/trigger.py
   if [ ! -f "$SCRIPT" ]; then
     mkdir -p "$(dirname "$SCRIPT")"
     curl -fsSL https://raw.githubusercontent.com/gregbny/homepod-temp-logger/main/homebridge-bridge/trigger.py -o "$SCRIPT"
   fi
   python3 "$SCRIPT"
   ```
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
