# iOS setup — step by step (the fiddly part)

Honest warning up front: **Apple gives you no clean way to import any of this.**
There's no shareable `.shortcut` file that "just works", and the **Home** app's
automation editor is more limited than the **Shortcuts** app (in particular it won't
let you insert variables). So you build it **by hand**, once. It's tedious, but it's a
one-time setup — after that the iPhone plays **no role**; everything runs on the
HomePod hub.

The trick that makes it bearable: **build and test the whole thing in the Shortcuts
app first** (where the variable picker works), then **copy the finished pieces into
the Home automation**. This doc gives you the exact JSON block to paste.

Order: do **A** (Shortcuts app, testable on its own), then **B** (the Home
automation).

---

## A. Build & test it in the Shortcuts app

Goal: read temp + humidity of both HomePods and POST them in one request. You'll
build this in the **Shortcuts** app because it's the only place the **variable
picker** works — you'll reuse these pieces in part B.

### A.1 — New shortcut
- **Shortcuts** app → *Shortcuts* tab → **+** (top right). Name it **`HomePod
  Logger`**.

### A.2 — Read the sensors (into named variables)
Variables are **not optional here** — without them the values and the URL blur
together and nothing works. For **each** value add a **Get State of Home Accessory**
action (**Home** category), then a **Set Variable** action:

| Get State of Home Accessory | Characteristic | Set Variable |
|---|---|---|
| HomePod (living room) | Temperature | `t1` |
| HomePod (living room) | Humidity | `h1` |
| HomePod (bedroom) | Temperature | `t2` |
| HomePod (bedroom) | Humidity | `h2` |

> If a HomePod doesn't list Humidity, that's normal on some models — see "Gotchas".

### A.3 — Build the JSON in a **Text** action
Add a **Text** action and type exactly this. Where you see `⟨t1⟩` etc., **insert the
variable** (type the name, then tap it in the bar above the keyboard) — don't leave it
as literal text. Replace `living-room` / `bedroom` with your own room labels:

```json
{"readings":[
  {"homepod":"living-room","temp":"⟨t1⟩","humidity":"⟨h1⟩"},
  {"homepod":"bedroom","temp":"⟨t2⟩","humidity":"⟨h2⟩"}
]}
```

**Note the quotes around the values.** HomePods emit localized strings like
`21,4°C` / `52,0 %` (decimal **comma**, plus a unit). The server now accepts those as
long as they're **quoted strings** — it strips the unit and the comma for you. A
**bare** comma (`"temp": 21,4`) is invalid JSON and will be rejected, so keep the
quotes and you don't need to reformat anything in the Shortcut.

> Keep this Text action tidy and self-contained — in part B you'll **copy this exact
> action** into the Home automation (that's how you get variables into Home, which
> can't insert them itself).

### A.4 — Send it (variables again)
Add **Get Contents of URL**. Tip: put the URL in its **own Text action / variable**
first (e.g. a Text action `http://SERVER_IP:8088/api/readings` → Set Variable `url`) —
trying to type the URL *and* wire the body in one go tends to get the fields tangled.

- **URL**: the `url` variable (or `http://SERVER_IP:8088/api/readings`, replacing
  `SERVER_IP`, e.g. `192.168.1.75`).
- **Show more**:
  - **Method**: `POST`
  - **Headers**: `Content-Type` = `application/json`
  - **Request Body**: **Text** (or **File**) → insert the **Text** variable from A.3.

### A.5 — Test the Shortcut alone
Run it (▶). Open `http://SERVER_IP:8088/` — a fresh reading for both rooms should
appear. On failure the server replies `400` with a detail message that says what's
wrong (bad JSON, value out of range, unreachable, etc.). **Don't move on until this
works.**

---

## B. Make it autonomous (Home app) — the tedious bit

This is where Apple fights you. Two hard truths, both worked around below:

1. You often **can't select your finished `HomePod Logger` shortcut** from a Home
   automation (the picker may not offer it) → you rebuild the actions **inside** the
   automation's editor.
2. The Home automation editor **won't let you insert variables** into a Text action →
   you **copy the Text (JSON) action from the Shortcuts app** and paste it in.

### B.0 — Prerequisites (from the Homebridge step)
The `HomePod Logger Trigger` switch must already be:
- **paired into the Home app** (you scanned the Homebridge/child-bridge QR), and
- **exposed as a Lamp**, not a plain Switch — otherwise it won't appear as a trigger.

Both are covered in [`../homebridge-bridge/README.md`](../homebridge-bridge/README.md)
(steps 1b and 1c). Do them first.

### B.1 — New Home automation
- **Home** app → **Automation** tab → **+** → **An Accessory is Controlled**.
- Pick **`HomePod Logger Trigger`** (it shows up because it's now a Lamp) → **Turns
  On** → **Next**.
- Leave the time window **all day, every day** (the hourly cadence comes from the
  cron, not here).

### B.2 — Build the action (the copy-paste trick)
- At the Actions step, scroll to the bottom → **Convert to Shortcut**. A mini
  Shortcuts editor opens. Delete any default actions.
- Recreate the flow here **by hand**: the four **Get State of Home Accessory** →
  **Set Variable** (`t1`,`h1`,`t2`,`h2`) actions, then the **Get Contents of URL**
  action (URL + POST + header, as in A.4).
- For the **JSON Text** action — which needs the `t1…h2` variables and which this
  editor **can't** build — do this: open the **Shortcuts** app in parallel, open your
  `HomePod Logger` shortcut from part A, **long-press the Text (JSON) action → Copy**,
  come back to the Home automation editor and **paste**. The pasted action keeps its
  variable references. Wire it as the **Request Body** of Get Contents of URL.
- **Done** → **Next** → **OK**.

> Yes, this is clumsy. It's the reliable path given the tooling. Test right after
> (part C) and, if a variable reference broke on paste, re-copy just the Text action.

### ❌ Do NOT
- Do **not** build a *"Time of Day"* automation in the **Shortcuts** app — it runs on
  the **iPhone**, may prompt for confirmation, and breaks autonomy. The trigger must
  be the switch, on the **Home** side, so the **HomePod hub** runs it.

---

## C. End-to-end checks

1. **Full chain:** run the cron once (Homebridge bridge → *Run Script*). The
   `HomePod Logger Trigger` switch flips → the Home automation runs → a new reading
   appears on `http://SERVER_IP:8088/`.
2. **Without the iPhone (the real test):** put the iPhone in Airplane Mode (or leave
   the house) and run the cron again. The reading should **still** land — proof the
   HomePod hub is doing the work, not your phone. 🎉
3. **Network:** hub and server on the same LAN. No internet exposure needed.

---

## Gotchas

- **You can't import a shortcut.** There's no working shareable file; this hand-build
  is the intended path. Budget ~30 min and test after every piece.
- **Comma / unit in temperature:** send the values **quoted** (`"21,4°C"`). The server
  handles the comma and the `°C`/`%`. Don't try to strip them in the Shortcut — a bare
  comma just breaks the JSON.
- **Home editor won't insert variables:** compose the Text (JSON) action in the
  **Shortcuts** app and **copy-paste** it into the Home automation (section B.2).
- **Switch not offered as a trigger:** expose the dummy as a **Lamp**, not a Switch
  (Homebridge step 1c).
- **A stray "light" clutters the Home app:** you can **hide** it — in the Home app,
  long-press the accessory → **⚙︎ / settings** → turn off **Show in Home / Status and
  Notifications** (wording varies by iOS). It stays available to automations while
  disappearing from the main view.
- **"Allow this shortcut" confirmation:** the first run may ask permission. Approve
  once; afterwards the hub proceeds on its own.
- **No humidity sensor:** some HomePods only expose temperature. Drop that room's
  humidity line from the JSON rather than sending a fake value.
- **Server IP changes:** set a **DHCP reservation** so the POST URL stays stable.
- **HomePod not a hub:** Home app → Home Settings → confirm a HomePod shows as a
  connected hub. Without one, automations don't run autonomously.
