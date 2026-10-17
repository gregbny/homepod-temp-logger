#!/bin/sh
# Receive a HomePod reading over SSH and forward it to the logger's API.
#
# Why this exists: on HomePod / tvOS 27 hubs, the Shortcuts network actions
# ("Get Contents of URL", …) fail silently when a Home automation runs them
# (Apple FB23893604). "Run Script over SSH" is a different action, so the iOS
# side sends the JSON over SSH and this script POSTs it on the server itself.
#
# Called by sshd as the forced command of a dedicated key (see README.md):
# the Shortcut's Script field carries the JSON, which sshd hands over in
# $SSH_ORIGINAL_COMMAND without running it. When that holds no JSON, the
# body is read from stdin instead (the action's Input field).
#
# Standard tools only (sh + curl), both present on Unraid.
#
# Env: HPL_URL  ingestion endpoint (default http://127.0.0.1:8088/api/readings)
#
# Exit 0 = stored, non-zero = failure (also logged to syslog).

URL="${HPL_URL:-http://127.0.0.1:8088/api/readings}"

log() {
    echo "[ssh-ingest] $*" >&2
    command -v logger >/dev/null 2>&1 && logger -t homepod-ssh-ingest -- "$*"
}

case "$SSH_ORIGINAL_COMMAND" in
    *"{"*) body="$SSH_ORIGINAL_COMMAND" ;;
    *) body="$(cat)" ;;
esac

if [ -z "$body" ]; then
    log "empty body: put the JSON in the Script (or Input) field"
    exit 2
fi

resp="$(printf '%s' "$body" | curl -sS -m 15 -X POST "$URL" \
    -H 'Content-Type: application/json' --data-binary @- \
    -w '\n%{http_code}')" || {
    log "POST $URL failed: server unreachable"
    exit 1
}

code="$(printf '%s\n' "$resp" | tail -n 1)"
msg="$(printf '%s\n' "$resp" | sed '$d')"

case "$code" in
    2??) log "OK ($code) $msg"; echo "$msg" ;;
    *) log "rejected ($code) $msg"; exit 1 ;;
esac
