#!/bin/sh
set -e
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
sleep 1
fluxbox >/dev/null 2>&1 &
if [ -n "$VNC_PASSWORD" ]; then
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
  x11vnc -display :99 -forever -shared -rfbauth /tmp/vncpass -rfbport 5900 -quiet &
else
  echo "WARN: VNC_PASSWORD 未设置，noVNC 无密码" >&2
  x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -quiet &
fi
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
exec python /app/main.py
