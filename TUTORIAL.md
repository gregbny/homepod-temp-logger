# Full tutorial — from zero to autonomous logging

This walks you through the whole thing in order. Budget ~30–45 min. You need:

- A HomePod (mini) — at least one set as a **Home hub** (Home app → Home Settings →
  the HomePod shows *Connected* as a hub).
- A home server running Docker (this guide uses **Unraid**), with **Homebridge**
  already running.
- An iPhone on the **same LAN** as the server.

Throughout, replace `SERVER_IP` with your server's LAN IP (e.g. `192.168.1.75`).

> **Recommended order:** get the server working first and test it with `curl`, then
> the Shortcut, then the switch + cron, then the Home automation. Each step is
> verifiable on its own, so you never debug four things at once.

---

## Step 1 — Deploy the server (Unraid)

The dashboard/API ships as a prebuilt image on GHCR:
`ghcr.io/gregbny/homepod-temp-logger:latest`.

### 1a. Add the container

**Docker** tab → **Add Container**. Leave *Template* empty and fill in:

| Field | Value |
|---|---|
| **Name** | `homepod-temp-logger` |
| **Repository** | `ghcr.io/gregbny/homepod-temp-logger:latest` |
| **Network Type** | `Bridge` |

Then **Add another Path, Port, Variable…** for each of these:

| Type | Name | Container | Host / Value |
|---|---|---|---|
| **Port** | WebUI | `8088` | `8088` |
| **Path** | Data | `/data` | `/mnt/user/appdata/homepod-temp-logger` |
| **Variable** | `PORT` | — | `8088` |
| **Variable** | `DB_PATH` | — | `/data/homepod.db` |
| **Variable** | `RETENTION_DAYS` | — | `0` (0 = keep everything) |

> Prefer a one-click import? Drop
> [`unraid/homepod-temp-logger.xml`](unraid/homepod-temp-logger.xml) into
> `/boot/config/plugins/dockerMan/templates-user/` on the server, then pick it from
> the **Template** dropdown — all fields are pre-filled.

Click **Apply**. Unraid pulls the image and starts it.

> **Port already used?** `8088` is a common free port, but if it clashes, change
> both the host port **and** the `PORT` variable to the same new value.

### 1b. Verify

Open `http://SERVER_IP:8088/` — you should see the dashboard (empty for now). Then
push a fake reading and watch it appear:

```bash
curl -X POST http://SERVER_IP:8088/api/readings \
  -H 'Content-Type: application/json' \
  -d '{"homepod":"living-room","temp":24.3,"humidity":52}'
```

Refresh the page: a `living-room` tile and a point on the charts. ✅ The server works.

*(Alternative without a prebuilt image: the [`server/`](server/README.md) folder has
a `docker-compose.yml` you can build locally with Unraid's Compose Manager.)*

---

## Step 2 — Create the iOS Shortcut

> Heads-up: this is the fiddly, all-manual part — Apple lets you import nothing, and
> you'll hand-build it. Full illustrated steps + workarounds are in
> [`ios-setup/README.md`](ios-setup/README.md). Overview:

Build a Shortcut named **`HomePod Logger`** (in the **Shortcuts** app) that:

1. Reads, via **Get State of Home Accessory** actions, temp + humidity of HomePod 1
   then HomePod 2, each into a **named variable** (`t1`,`h1`,`t2`,`h2` — variables are
   required, not optional).
2. Assembles the batch JSON in a **Text** action, with the values **quoted**:
   `{"readings":[{"homepod":"living-room","temp":"⟨t1⟩","humidity":"⟨h1⟩"}, …]}`.
   Quoting matters — the server accepts HomePod's localized `"21,4°C"` / `"52,0 %"`
   (comma + unit) as long as they're strings, so you don't reformat anything.
3. **Gets Contents of URL** → `http://SERVER_IP:8088/api/readings`, method `POST`,
   header `Content-Type: application/json`, body = the JSON text variable.

**Test it:** run the Shortcut once (▶). A new reading for both rooms should appear on
the dashboard. ✅ The read→POST path works. (You'll reuse these pieces in Step 4.)

---

## Step 3 — Homebridge virtual switch + hourly trigger

Full steps in [`homebridge-bridge/README.md`](homebridge-bridge/README.md).

### 3a. The switch

Homebridge UI → **Plugins** → install the **Dummy** plugin → open its **config UI**
(the wrench) and add a **momentary switch** named `HomePod Logger Trigger` with a
**1-second auto-reset**. The UI writes a `platforms` block (`"platform":
"HomebridgeDummy"`) — see [`homebridge-bridge/config.snippet.json`](homebridge-bridge/config.snippet.json).
Restart Homebridge. The auto-reset makes it **momentary** (returns to off by itself),
so each toggle fires a clean "turned on" event.

> Two easy-to-miss steps: after this you must **pair the switch into the Home app**
> (scan the Homebridge/child-bridge QR) **and expose it as a Lamp** (a plain Switch
> often isn't selectable as an automation trigger). Both are in
> [`homebridge-bridge/README.md`](homebridge-bridge/README.md) steps 1b–1c.

### 3b. The hourly cron

Copy [`homebridge-bridge/trigger.py`](homebridge-bridge/trigger.py) to the server
(e.g. `/mnt/user/appdata/homepod-temp-logger/trigger.py`). Install **User Scripts**
(Community Apps), add a script:

```bash
#!/bin/bash
export HB_URL="http://SERVER_IP:8581"          # Homebridge UI (host mode)
export HB_USER="your-homebridge-user"
export HB_PASS="your-homebridge-password"
export HB_SWITCH_NAME="HomePod Logger Trigger"
python3 /mnt/user/appdata/homepod-temp-logger/trigger.py
```

Set the schedule to **hourly** (custom cron `0 * * * *`).

**Test it:** click **Run Script** once. The `HomePod Logger Trigger` switch should
flip on (briefly) in Homebridge UI / the Home app. ✅ The cron→switch path works.

---

## Step 4 — Wire the Home app automation (the autonomous bit)

This is the key step and it must be in the **Home** app, not Shortcuts — and it's the
most awkward one. Full steps + workarounds in
[`ios-setup/README.md`](ios-setup/README.md#b-make-it-autonomous-home-app--the-tedious-bit).

- **Home** app → **Automation** → **+** → **An Accessory is Controlled** →
  `HomePod Logger Trigger` (visible because it's exposed as a Lamp) → **Turns On**.
- Action: scroll down → **Convert to Shortcut**. You usually **can't** just pick your
  existing `HomePod Logger` shortcut, so rebuild the actions here. The catch: this
  editor **won't insert variables**, so **copy the JSON Text action from the Shortcuts
  app** (long-press → Copy) and **paste** it into this automation. Wire it as the POST
  body. Done.

Because this automation lives on the **HomePod hub**, it runs with no iPhone
involved.

---

## Step 5 — End-to-end test

1. Run the cron once (**Run Script**). Within a moment a fresh reading appears on the
   dashboard — that's cron → switch → Home automation → Shortcut → server, fully
   chained. ✅
2. **The real test:** put your iPhone in Airplane Mode (or leave the house), run the
   cron again. The reading **still** lands — proving the HomePod hub is doing the
   work, not your phone. 🎉

From now on you get one reading per HomePod every hour, forever, with no interaction.

---

## Step 6 — Install the dashboard as an app (optional)

- **iPhone/iPad (Safari):** open `http://SERVER_IP:8088/` → **Share** → **Add to Home
  Screen**. It launches full-screen with its own icon and keeps showing the last
  cached charts even if the server is briefly unreachable.
- **Mac (Chrome/Edge):** install icon in the address bar, or menu → *Install HomePod
  Logger*.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Dashboard loads but stays empty | No readings yet. Run the `curl` test (Step 1b) or the Shortcut (Step 2). |
| Shortcut fails at the POST | Wrong `SERVER_IP`/port, or malformed JSON. The server returns `400` with a detail message — read it. Check the phone is on the same LAN. |
| `400` on temperature like `21,4` | Send the value **quoted** (`"21,4°C"`). A bare comma is invalid JSON; a quoted string is accepted and the comma/unit are stripped server-side. |
| Switch not offered as an automation trigger | Expose the dummy as a **Lamp**, not a Switch (Homebridge README step 1c), and make sure it's **paired into Home** (step 1b). |
| Can't insert variables in the Home automation | Expected — the Home editor can't. Build the JSON **Text** action in the **Shortcuts** app and **copy-paste** it into the automation. |
| Home automation never runs the Shortcut | You created it in **Shortcuts**, not **Home**. Recreate it under Home → Automation → *An Accessory is Controlled*. |
| First automation run asks to confirm the Shortcut | iOS confirmation prompt. Approve once; in Shortcuts settings disable the confirmation for this Shortcut if the option exists. |
| Tiles turn red / "x h ago" grows | No recent reading (older than 2h) — the cron or a link in the chain stopped. Re-test Step 3b, then Step 5. |
| Humidity missing for a HomePod | Some HomePod models/versions expose only temperature. Drop that room's humidity line from the Shortcut JSON. |
| Server IP changed | Set a **DHCP reservation** for the server so the POST URL stays stable. |
| No HomePod hub | Home app → Home Settings → confirm a HomePod shows as a connected hub. Without one, Home automations don't run autonomously. |
