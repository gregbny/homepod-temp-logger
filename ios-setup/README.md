# iOS setup — step by step

This part can't be automated: you do it once, by hand, on the iPhone. After that the
iPhone plays **no role** — everything runs on the HomePod hub.

> ⚠️ **The single most important step** is section **B**: the automation must be
> created in the **Home** app, NOT in the **Shortcuts** app. That's what makes
> logging autonomous (independent of the iPhone).

Order: do **A** (the Shortcut) first and test it, then **B** (the automation).

---

## A. Create the Shortcut (Shortcuts app)

Goal: read temp + humidity of both HomePods and send them in a single POST.

### A.1 — New shortcut
- **Shortcuts** app → *Shortcuts* tab → **+** (top right).
- Rename it **`HomePod Logger`** (a stable name — the Home automation will reference
  it).

### A.2 — Read the sensors
For **each** value, add a **Get State of Home Accessory** action (under the **Home**
category):

1. *Get State of Home Accessory* → pick **HomePod (living room)** → characteristic
   **Temperature**. → then *Set Variable* → name it `t1`.
2. *Get State of Home Accessory* → **HomePod (living room)** → **Humidity**.
   → *Set Variable* `h1`.
3. *Get State of Home Accessory* → **HomePod (bedroom)** → **Temperature**.
   → *Set Variable* `t2`.
4. *Get State of Home Accessory* → **HomePod (bedroom)** → **Humidity**.
   → *Set Variable* `h2`.

> Tip: if a HomePod doesn't expose humidity in the list, that's normal on some
> models/versions — see "Gotchas" below.

### A.3 — Build the JSON (batch format)
- Add a **Text** action and paste exactly this, inserting the variables `t1`, `h1`,
  `t2`, `h2` at the marked spots (type the name, then pick the variable from the bar
  above the keyboard). Replace `living-room` / `bedroom` with your own room labels:

```json
{"readings":[
  {"homepod":"living-room","temp":⟨t1⟩,"humidity":⟨h1⟩},
  {"homepod":"bedroom","temp":⟨t2⟩,"humidity":⟨h2⟩}
]}
```

- The `⟨…⟩` mean **inserted variables** (not literal text).
- We deliberately omit `timestamp`: the server uses receive time. (The hourly cadence
  comes from the cron, so receive time is accurate enough.)

### A.4 — Send to the server
- Add **Get Contents of URL**:
  - **URL**: `http://SERVER_IP:8088/api/readings`
    (replace `SERVER_IP` with the server's LAN IP, e.g. `192.168.1.75`).
  - Expand **Show more**:
    - **Method**: `POST`
    - **Headers**: add `Content-Type` = `application/json`
    - **Request Body**: **File** → select the **Text** variable built in A.3 (the
      JSON).

### A.5 — Test the Shortcut alone
- Run it manually (▶).
- Open `http://SERVER_IP:8088/` in a browser: a new reading should appear for both
  rooms.
- On failure, the server's response body (`400` with details) helps diagnose a
  malformed JSON.

---

## B. Create the automation (HOME app — critical)

This automation, hosted by the **HomePod hub**, runs the Shortcut without the iPhone
whenever the server turns the switch on.

### B.1 — New Home automation
- **Home** app → **Automation** tab (or **+** → *Add Automation*).
- Trigger type: **An Accessory is Controlled**.
- Pick the accessory **`HomePod Logger Trigger`** (the switch from the Homebridge
  bridge).
- Condition: **Turns On**.
- Time settings: leave it **all day**, every day (the cadence comes from the cron,
  not here).

### B.2 — Action: run the Shortcut
- At the "Actions" step, scroll to the very bottom → **Convert to Shortcut**.
- In the editor that opens, **delete** the default actions and add **Run Shortcut** →
  select **`HomePod Logger`** (from part A).
- **Done** → **Next** → **OK**.

> Depending on the iOS version the action may be called "Run Shortcut" or offered
> directly. The essential part: the **Home** automation ends up calling the
> `HomePod Logger` Shortcut.

### ❌ Do NOT
- Do **not** create a *"Time of Day"* automation in the **Shortcuts** app. It runs on
  the **iPhone**, often asks for confirmation, and breaks autonomy. The trigger must
  stay the switch, on the **Home** side.

---

## C. End-to-end checks

1. **Full chain:** run the cron once (bridge, *Run Script*) → the
   `HomePod Logger Trigger` switch flips → the Home automation runs `HomePod Logger`
   → a new reading appears on `http://SERVER_IP:8088/`.
2. **Without the iPhone:** put the iPhone in Airplane Mode (or leave the house), run
   the cron again. The reading should **still** land — the HomePod hub is doing the
   work. (This is the test that proves the whole point of the project.)
3. **Network:** the HomePod hub and the server must be on the same LAN. No internet
   exposure needed.

---

## Gotchas

- **"Allow this shortcut" confirmation:** the first automation run may ask for
  permission. Shortcuts → Settings → disable confirmations for this shortcut if the
  option exists; or approve once, and the hub proceeds on its own afterward.
- **No humidity sensor:** some HomePods/versions only expose temperature. If humidity
  is missing for a HomePod, drop its line from the JSON, or send `"humidity":0` — but
  prefer removing the corresponding action so the charts stay clean.
- **Server IP changes:** set a **DHCP reservation** for the server so the POST URL
  stays stable.
- **HomePod not set as a hub:** Settings → Home → *Home* → check that a HomePod shows
  "Home Hub: Connected". Without a hub, no autonomous automation.
