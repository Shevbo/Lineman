#!/usr/bin/env bash
# Lineman boot script.
# Cutover 2026-08-11 (инцидент утечки fedbackup msg 24302): все секреты берём
# через Keymaster HTTP API. Файл ~/keymaster/.lineman-proxy.env удалён —
# никаких plaintext .env на диске. Keymaster сам держит values в
# ~/.keymaster/credentials/ (600 shectory:shectory).
#
# --dry-run — fetch secrets, print counts (без exec) — для проверки без рестарта.
set -euo pipefail
cd "$(dirname "$0")"

KMASTER_URL="${KMASTER_URL:-http://127.0.0.1:9093}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# Fetch one secret from Keymaster. Prints value to stdout, exits 1 on any fail.
# Не логируем значения — только имена + размер.
km_get() {
  local name="$1"
  local resp
  resp=$(curl -sS -m 8 -X POST \
    "${KMASTER_URL}/keymaster/request-value?name=${name}&requester=lineman&purpose=lineman-boot" \
    2>/dev/null) || {
      echo "[km_get] $name: keymaster unreachable at $KMASTER_URL" >&2
      return 1
    }
  # Parse status + delivery в ОДИН вызов python (не два — разные stdin risk race)
  local parsed
  parsed=$(printf '%s' "$resp" | .venv/bin/python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get("status",""), d.get("delivery",""), sep="\t")
except Exception:
    print("", "", sep="\t")
') || parsed=$'\t'
  local status delivery
  status="${parsed%%$'\t'*}"
  delivery="${parsed#*$'\t'}"
  if [ "$status" != "approved" ] || [ -z "$delivery" ]; then
    echo "[km_get] $name: status=$status (нужно pre_approved=lineman в manifest)" >&2
    return 1
  fi
  # Expand leading ~ (bash не делает подстановку в переменных)
  delivery="${delivery/#\~/$HOME}"
  if [ ! -f "$delivery" ]; then
    echo "[km_get] $name: delivery-file '$delivery' не существует" >&2
    return 1
  fi
  cat "$delivery"
}

# Список секретов, которые нужны Lineman. Order/spelling ровно как в
# os.environ.get() в main.py + connect/dispatch/mcp модулях.
LINEMAN_SECRETS=(
  DEEPSEEK_API_KEY
  GEMINI_API_KEY
  TELEGRAM_BOT_TOKEN
  KLOD_BOT_TOKEN
  LINEMAN_PROXY1_URL
  LINEMAN_IPROYAL_URL
  LINEMAN_PROXY6_URL
  SHECTORY_AUTH_BRIDGE_SECRET
  SHECTORY_PORTAL_URL
)

echo "[boot] fetching ${#LINEMAN_SECRETS[@]} secrets from Keymaster ($KMASTER_URL)..."
for name in "${LINEMAN_SECRETS[@]}"; do
  val=$(km_get "$name") || {
    echo "[boot] FATAL: cannot fetch $name — aborting" >&2
    exit 1
  }
  export "$name=$val"
  echo "  ok  $name (${#val} bytes)"
done

if [ "$DRY_RUN" = "1" ]; then
  echo "[boot] --dry-run: not exec'ing main.py"
  exit 0
fi

exec .venv/bin/python3 main.py
