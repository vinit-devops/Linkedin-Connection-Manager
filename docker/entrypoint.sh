#!/bin/bash
set -e
mkdir -p "$DATA_DIR/logs"
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
# A container stopped mid-run leaves Chromium's profile lock behind (it names the old
# container's hostname), which blocks every later launch. Nothing runs yet, so clear it.
rm -f "$DATA_DIR"/browser_profile/Singleton{Lock,Cookie,Socket}

# Virtual screen the bot's (non-headless) Chromium draws on.
Xvfb :99 -screen 0 1320x1000x24 -nolisten tcp >/dev/null 2>&1 &
for i in $(seq 1 20); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.25; done
fluxbox >/dev/null 2>&1 &

# VNC is only reachable inside the container; noVNC (port 6080) is the way in.
# -skip_lockkeys: macOS sends Caps Lock oddly; ignore it and trust the (already upper-case) letters
x11vnc -display :99 -forever -shared -nopw -quiet -localhost -rfbport 5900 -skip_lockkeys >/dev/null 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

echo "Dashboard: http://localhost:8000   Browser view: http://localhost:6080/vnc.html"
exec uvicorn ui.server:app --host 0.0.0.0 --port 8000
