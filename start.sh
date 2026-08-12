#!/usr/bin/env bash
# Lineman boot script (PM2 entry point).
#
# Cutover 2026-08-11 (инцидент утечки fed-backup msg 24302 — plain-text
# .lineman-proxy.env читался любым shectory-агентом): все секреты берём
# через Keymaster HTTP API. Файл ~/keymaster/.lineman-proxy.env удалён
# насовсем — никаких plaintext .env в проекте. Keymaster хранит values
# в ~/.keymaster/credentials/ (600 shectory:shectory) и отдаёт по API
# только запрашивающим из pre_approved (см. manifest).
#
# --dry-run — fetch без exec (проверка без рестарта прод-сервиса).
set -euo pipefail
cd "$(dirname "$0")"

KMASTER_URL="${KMASTER_URL:-http://127.0.0.1:9093}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# km_get NAME — печатает значение секрета из Keymaster; exit 1 при неуспехе.
# Значения не логируем — только имя + размер.
km_get() {
  local name="$1"
  local resp
  resp=$(curl -sS -m 8 -X POST \
    "${KMASTER_URL}/keymaster/request-value?name=${name}&requester=lineman&purpose=lineman-boot" \
    2>/dev/null) || {
      echo "[km_get] $name: keymaster unreachable at $KMASTER_URL" >&2
      return 1
    }
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
    echo "[km_get] $name: status=$status (нужно 'lineman' в manifest.secrets.$name.pre_approved)" >&2
    return 1
  fi
  delivery="${delivery/#\~/$HOME}"
  if [ ! -f "$delivery" ]; then
    echo "[km_get] $name: delivery-file '$delivery' отсутствует" >&2
    return 1
  fi
  cat "$delivery"
}

# Полный список ENV, которые Lineman ожидает. Порядок = порядок появления в
# коде (main.py + модули). Все должны быть pre_approved для 'lineman'.
LINEMAN_SECRETS=(
  DEEPSEEK_API_KEY
  GEMINI_API_KEY
  GEMINI_LINEMAN_API_TOKEN
  TELEGRAM_BOT_TOKEN
  KLOD_BOT_TOKEN
  LINEMAN_PROXY1_URL
  LINEMAN_PROXY6_URL
  SHECTORY_AUTH_BRIDGE_SECRET
  SHECTORY_PORTAL_URL
)

echo "[boot] fetching ${#LINEMAN_SECRETS[@]} secrets from Keymaster ($KMASTER_URL)..."
for name in "${LINEMAN_SECRETS[@]}"; do
  val=$(km_get "$name") || {
    echo "[boot] FATAL: cannot fetch $name — aborting boot" >&2
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
