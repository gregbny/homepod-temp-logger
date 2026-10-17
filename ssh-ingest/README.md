# SSH ingestion — workaround for OS 27 hubs

On **HomePod / tvOS 27** hubs, the Shortcuts network actions (**Get Contents of
URL**, Get Contents of Web Page, Get Weather, …) **fail silently** when a Home
automation runs them (Apple FB23893604, tracked publicly in
[homebridge-dummy#483](https://github.com/mpatfield/homebridge-dummy/issues/483)).
The symptom: the Shortcut works when you test it on the iPhone, the trigger
fires, yet no reading ever reaches the server.

**Run Script over SSH** is a different action and isn't on the list of broken
ones. So the Shortcut sends the same JSON **over SSH to the Unraid host**, and
[`ingest.sh`](ingest.sh) POSTs it to the logger locally:

```
[Home automation on the hub] --Run Script over SSH (JSON)--> [Unraid sshd]
        --forced command--> ingest.sh --POST 127.0.0.1:8088--> [logger container]
```

The SSH key is **locked to that one script** (`restrict,command=…`): it can't open
a shell, run anything else, or forward ports. Whatever the Shortcut sends as
"script" is never executed — `ingest.sh` only treats it as the JSON body.

The server itself doesn't change: same endpoint, same payload contract.

## 1. Enable SSH on Unraid

**Settings → Management Access → Use SSH: Yes** (port `22`) → *Apply*.

## 2. Put `ingest.sh` on the server

On the Unraid terminal:

```bash
mkdir -p /mnt/user/appdata/homepod-temp-logger
curl -fsSL https://raw.githubusercontent.com/gregbny/homepod-temp-logger/main/ssh-ingest/ingest.sh \
  -o /mnt/user/appdata/homepod-temp-logger/ssh-ingest.sh
```

It's called through `sh`, so it doesn't need to be executable. It only uses `sh`
and `curl`, both built into Unraid.

Quick check, still on the terminal (should print `"stored":1`):

```bash
echo '{"homepod":"ssh-test","temp":"21,4°C","humidity":"50 %"}' \
  | sh /mnt/user/appdata/homepod-temp-logger/ssh-ingest.sh
```

(This leaves an `ssh-test` reading on the dashboard; it ages out of the short
ranges by itself.)

## 3. Authorize the Shortcut's key — locked to `ingest.sh`

1. On the iPhone, get the Shortcut's **public key** (see
   [`../ios-setup/README.md`](../ios-setup/README.md#ssh-variant-os-27-hubs), step 1).
   It looks like `ssh-ed25519 AAAAC3Nz… `.
2. Unraid → **Users** → **root** → **SSH authorized keys** → add **one line**:

   ```
   restrict,command="sh /mnt/user/appdata/homepod-temp-logger/ssh-ingest.sh" ssh-ed25519 AAAAC3Nz…
   ```

   (the `restrict,command="…"` prefix, a space, then the public key exactly as
   iOS gave it) → *Apply*. Unraid stores this on the flash drive, so it survives
   reboots — unlike editing `/root/.ssh` by hand.

> **Changed the logger's host port?** `ingest.sh` posts to
> `http://127.0.0.1:8088/api/readings`. If you mapped another port, set it in the
> same line: `command="HPL_URL=http://127.0.0.1:9000/api/readings sh /mnt/user/appdata/homepod-temp-logger/ssh-ingest.sh"`.

## 4. Check from another machine (optional)

From a Mac/Linux box, with a key you authorized the same way:

```bash
ssh root@SERVER_IP '{"homepod":"ssh-test","temp":"21,4°C","humidity":"50 %"}'
# -> {"status":"ok","received":1,"stored":1}
```

## Troubleshooting

`ingest.sh` logs every call to the Unraid syslog (**Tools → System Log**, tag
`homepod-ssh-ingest`):

| Log / Shortcut output | Meaning |
|---|---|
| `OK (200) …"stored":2` | it works |
| `empty body` | the Script field doesn't hold the JSON (the variable didn't survive the paste — re-copy the action) |
| `rejected (400) …` | the server refused the JSON; the detail says why (bad JSON, value out of range) |
| `server unreachable` | the logger container is stopped, or not on port 8088 (see the port note above) |
| nothing at all | SSH never connected: SSH disabled, wrong IP/user, or key not authorized |

Once Apple fixes the hub bug (expected in 27.2), you can go back to **Get
Contents of URL** — or keep SSH; both feed the same endpoint.
