#!/bin/bash
# macOS counterpart of run.ps1. Started at login by the launchd agent "local.kolmail" (written by install.sh), which
# also restarts it whenever the app exits (KeepAlive, 15 s throttle): that is how a self-update takes effect.
# The log lives next to the install (~/Library/Application Support/kol-mail/server.log) because it can contain mail
# subjects.
#   run.sh <venv python>

PY="${1:-$HOME/Library/Application Support/kol-mail/venv/bin/python}"
APP="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$(dirname "$APP")/server.log"
export PYTHONIOENCODING=utf-8
cd "$APP" || exit 1

stamp() { date "+%Y-%m-%dT%H:%M:%S"; }
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 5242880 ]; then
    mv -f "$LOG" "$LOG.1"
fi
PORT="$("$PY" -I -c 'import json, sys
try:
    print(int(json.load(open(sys.argv[1], encoding="utf-8-sig")).get("port") or 8765))
except Exception:
    print(8765)' "$APP/data/settings.json")"
# Another copy already serves the port. A second one would also log in to the Telegram session twice.
if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[$(stamp)] port $PORT is in use by another process, checking again in 60 s" >> "$LOG"
    sleep 60
    exit 0
fi
echo "[$(stamp)] starting kol-mail on 127.0.0.1:$PORT" >> "$LOG"
exec "$PY" -I -X utf8 app.py >> "$LOG" 2>&1
