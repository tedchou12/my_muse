#!/usr/bin/env bash
set -Eeuo pipefail

export DISPLAY="${DISPLAY:-:99}"
PROFILE_DIR="${PROFILE_DIR:-/profile}"
BROWSER_SCREEN="${BROWSER_SCREEN:-1440x900x24}"

mkdir -p "$PROFILE_DIR"
# A killed container can leave Chromium's singleton lock behind.
rm -f \
  "$PROFILE_DIR/SingletonLock" \
  "$PROFILE_DIR/SingletonSocket" \
  "$PROFILE_DIR/SingletonCookie"

Xvfb "$DISPLAY" -screen 0 "$BROWSER_SCREEN" -nolisten tcp &
XVFB_PID=$!

for _ in $(seq 1 50); do
  if [ -S /tmp/.X11-unix/X99 ]; then
    break
  fi
  sleep 0.1
done

start_vnc() {
  while true; do
    # Xvfb can create its socket before it is ready to accept clients.
    # Keep restarting x11vnc until the display is usable.
    x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 -noxdamage -quiet || true
    sleep 1
  done
}
start_vnc &
VNC_PID=$!

# Let the VNC listener come up before starting the web socket proxy.
for _ in $(seq 1 100); do
  if (echo > /dev/tcp/127.0.0.1/5900) 2>/dev/null; then
    break
  fi
  sleep 0.1
done

websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 &
WS_PID=$!

# Chrome deliberately binds DevTools to loopback. Expose the small CDP proxy on
# the compose network so Muse can connect without weakening Chrome itself.
python3 /usr/local/bin/cdp-proxy.py &
PROXY_PID=$!

cleanup() {
  kill "$PROXY_PID" "$WS_PID" "$VNC_PID" "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

CHROME_BIN="$(find /ms-playwright -path '*/chrome-linux/chrome' -type f 2>/dev/null | sort | tail -1)"
if [ -z "$CHROME_BIN" ]; then
  CHROME_BIN="$(command -v chromium || command -v chromium-browser || true)"
fi
if [ -z "$CHROME_BIN" ]; then
  echo "Could not find a Chromium binary." >&2
  exit 1
fi

exec "$CHROME_BIN" \
  --no-sandbox \
  --test-type \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-extensions \
  --no-first-run \
  --no-default-browser-check \
  --disable-background-networking \
  --disable-sync \
  --disable-features=ServiceWorker \
  --password-store=basic \
  --user-data-dir="$PROFILE_DIR" \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port=9222 \
  --remote-allow-origins=* \
  --window-size=1440,900 \
  --window-position=0,0 \
  about:blank
