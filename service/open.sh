#!/bin/bash
# What the "KOL Mail" app in Applications runs (macOS counterpart of start-kol-mail.cmd): open the page in the
# default browser, starting the launchd agent first when nothing serves the port yet.
#   open.sh [--no-browser]     (install.sh uses --no-browser for unattended test installs)

APP="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$(dirname "$APP")/server.log"
LABEL="local.kolmail"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="$(/usr/bin/plutil -extract port raw -o - "$APP/data/settings.json" 2>/dev/null || true)"
case "$PORT" in ''|*[!0-9]*) PORT=8765 ;; esac
URL="http://127.0.0.1:$PORT/"

listening() { /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; }

if ! listening; then
    if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null  # switched off under Login Items: back on
        launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null
    fi
    launchctl kickstart "gui/$(id -u)/$LABEL" >/dev/null 2>&1
    for _ in $(seq 1 60); do
        listening && break
        sleep 1
    done
    if ! listening; then
        echo "KOL Mail 60 秒内没有启动起来。日志在 $LOG ，最后几行：" >&2
        tail -n 5 "$LOG" 2>/dev/null >&2
        exit 1
    fi
fi
[ "$1" = "--no-browser" ] || open "$URL"
exit 0
