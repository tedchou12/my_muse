#!/usr/bin/env bash
# Configure Muse to use an OpenCode Go subscription.
#
# The API key is read with a hidden prompt (or from a 0600 file), written only to
# .env, and never printed. Muse is then recreated and checked for health.
set -euo pipefail

cd "$(dirname "$0")/.."

MODEL="${MUSE_OPENCODE_MODEL:-deepseek-v4.1-flash}"
KEY_FILE=""

usage() {
  cat <<'EOF'
Usage: scripts/set-opencode-go.sh [--model MODEL] [--key-file PATH]

  --model MODEL      Go model id (default: deepseek-v4.1-flash)
  --key-file PATH    Read the API key from a private file instead of prompting

Get your key from the OpenCode Console after subscribing to Go:
  https://opencode.ai/console
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --model)
      MODEL="${2:-}"
      shift 2
      ;;
    --key-file)
      KEY_FILE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [ ! -f .env ]; then
  echo "No .env found. Run: cp .env.example .env, set APP_PASSWORD, then retry." >&2
  exit 1
fi

if [ -n "$KEY_FILE" ]; then
  if [ ! -f "$KEY_FILE" ]; then
    echo "Key file not found: $KEY_FILE" >&2
    exit 1
  fi
  KEY="$(cat "$KEY_FILE")"
else
  printf 'OpenCode Go API key (input hidden): '
  read -r -s KEY
  printf '\n'
fi

KEY="$(printf '%s' "$KEY" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
unset KEY_FILE
if [ -z "$KEY" ]; then
  echo "No key entered; nothing changed." >&2
  exit 1
fi
case "$KEY" in
  *[!A-Za-z0-9._~+/=-]*)
    echo "Key contains unexpected characters; aborting without writing." >&2
    exit 1
    ;;
esac

TMP="$(mktemp)"
chmod 600 "$TMP"
printf '%s' "$KEY" > "$TMP"
unset KEY
trap 'rm -f "$TMP"' EXIT

python3 - "$TMP" "$MODEL" <<'PY'
import json
import pathlib
import re
import sys

key = pathlib.Path(sys.argv[1]).read_text().strip()
model = sys.argv[2].strip()
path = pathlib.Path(".env")
text = path.read_text() if path.exists() else ""


def set_value(text: str, name: str, value: str) -> str:
    line = "%s=%s" % (name, value)
    pattern = re.compile(r"^%s=.*$" % re.escape(name), re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _match: line, text)
    return text.rstrip("\n") + "\n" + line + "\n"


values = {
    "OPENAI_BASE_URL": "https://opencode.ai/inference/go/openai/v1",
    "OPENAI_API_KEY": json.dumps(key),
    "OPENAI_TEXT_MODEL": model,
    "OPENAI_USER_AGENT": "Muse/0.1.0",
    "OPENAI_SESSION_HEADER": "x-opencode-session",
}
for name, value in values.items():
    text = set_value(text, name, value)
path.write_text(text)
path.chmod(0o600)
print("Configured .env for OpenCode Go (key not displayed).")
print("Model:", model)
print("Image generation is not part of Go; set OPENAI_IMAGE_* separately if needed.")
PY

rm -f "$TMP"
trap - EXIT

docker compose up -d --build --force-recreate

echo "Waiting for Muse to become healthy..."
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo -n "Health: "
curl -fsS http://127.0.0.1:8000/healthz || true
echo

if [ -x .venv/bin/python ]; then
  APP_PW="$(
    .venv/bin/python - <<'PY' 2>/dev/null || true
from app.config import Settings
print(Settings().app_password)
PY
  )"
else
  APP_PW="$(grep '^APP_PASSWORD=' .env | cut -d= -f2- || true)"
fi

if [ -n "$APP_PW" ]; then
  PAYLOAD="$(python3 -c 'import json,sys; print(json.dumps({"password": sys.argv[1]}))' "$APP_PW")"
  curl -s -c /tmp/muse-opencode-cookies -X POST http://127.0.0.1:8000/api/login \
    -H 'Content-Type: application/json' -d "$PAYLOAD" >/dev/null || true
  echo -n "Session: "
  curl -s -b /tmp/muse-opencode-cookies http://127.0.0.1:8000/api/session || true
  echo
  rm -f /tmp/muse-opencode-cookies
fi

echo
echo "Done. Open http://127.0.0.1:8000 and send a short message to verify Go."
echo "Note: if you saved provider settings in the Provider tab, the UI config"
echo "overrides .env. Rotate/update the key there after changing it in the Console."
