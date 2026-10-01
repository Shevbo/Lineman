#!/usr/bin/env bash
# install-claude-creds.sh — принимает JSON учётных данных Claude со stdin и ставит его
# в ~/.claude/.credentials.json.
#
# Зачем: с smain нельзя войти на claude.ai — Cloudflare отдаёт challenge с датацентрового
# IP прокси. Вход выполняется на Windows-машине под VPN, сюда переносится результат.
# refreshTokenExpiresAt при обновлении НЕ продлевается, поэтому перенос нужен регулярно.
#
# Значения токенов не печатаются и не логируются (канон §7) — только даты и подписка.
#
# Использование (с Windows, PowerShell):
#   Get-Content -Raw "$env:USERPROFILE\.claude\.credentials.json" | ssh smain "bin/install-claude-creds.sh"
#   ... --force   — поставить, даже если здешний refresh-токен живёт дольше присланного
set -uo pipefail

DEST="$HOME/.claude/.credentials.json"
LOG="$HOME/logs/claude-creds-install.log"

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

FORCE=0; [ "${1:-}" = "--force" ] && FORCE=1

# Без этой проверки запуск руками (без файла на входе) молча висел бы на `cat`,
# и это выглядело бы как зависание, а не как забытый конвейер.
if [ -t 0 ]; then
    echo "Нечего читать: JSON подаётся на стандартный вход." >&2
    echo >&2
    usage >&2
    exit 2
fi

mkdir -p "$(dirname "$DEST")" "$(dirname "$LOG")"

TMP="$(mktemp "${DEST}.new.XXXXXX")" || exit 1
chmod 600 "$TMP"
trap 'rm -f "$TMP"' EXIT
cat > "$TMP"

python3 - "$TMP" "$DEST" "$FORCE" <<'PY'
import json, sys, datetime, os
new_p, dest_p, force = sys.argv[1], sys.argv[2], sys.argv[3] == "1"

def oauth(p):
    # BOM снимаем явно: Windows отдаёт файл с ним и через `type`, и через
    # PowerShell, а json.load на нём падает невнятным «Expecting value: line 1
    # column 1». Для человека у ярлыка это выглядит как «скрипт сломался».
    with open(p, encoding="utf-8-sig") as fh:
        d = json.load(fh)
    return d.get("claudeAiOauth", d)

try:
    n = oauth(new_p)
except Exception as e:
    print("ОТКАЗ: на входе не JSON (%s)" % type(e).__name__); sys.exit(1)
for k in ("accessToken", "refreshToken", "refreshTokenExpiresAt"):
    if not n.get(k):
        print("ОТКАЗ: в JSON нет поля %s" % k); sys.exit(1)

def when(ms):
    return datetime.datetime.fromtimestamp(ms / 1000)

now = datetime.datetime.now()
if when(n["refreshTokenExpiresAt"]) <= now:
    print("ОТКАЗ: присланный refresh-токен уже истёк (%s)" % when(n["refreshTokenExpiresAt"]).strftime("%d.%m %H:%M"))
    sys.exit(1)

old_txt = "нет файла"
if os.path.exists(dest_p):
    try:
        o = oauth(dest_p)
        old_txt = when(o["refreshTokenExpiresAt"]).strftime("%d.%m %H:%M")
        if o["refreshTokenExpiresAt"] >= n["refreshTokenExpiresAt"] and not force:
            print("ПРОПУСК: здесь refresh-токен живёт не меньше (%s против %s). Нужен --force."
                  % (old_txt, when(n["refreshTokenExpiresAt"]).strftime("%d.%m %H:%M")))
            sys.exit(2)
    except SystemExit:
        raise
    except Exception:
        old_txt = "нечитаемый файл"

print("ГОТОВ: подписка=%s, refresh %s -> %s, access до %s" % (
    n.get("subscriptionType", "?"), old_txt,
    when(n["refreshTokenExpiresAt"]).strftime("%d.%m %H:%M"),
    when(n["expiresAt"]).strftime("%d.%m %H:%M") if n.get("expiresAt") else "?"))
PY
rc=$?
# 2 = «пропуск», это не ошибка: планировщик не должен краснеть на штатном no-op.
if [ "$rc" = "2" ]; then exit 0; fi
if [ "$rc" != "0" ]; then exit 1; fi

BAK="${DEST}.bak-$(date +%Y%m%d-%H%M%S)"
[ -f "$DEST" ] && cp -p "$DEST" "$BAK" && ls -1t "${DEST}".bak-* 2>/dev/null | tail -n +6 | xargs -r rm -f

mv "$TMP" "$DEST"; chmod 600 "$DEST"; trap - EXIT
echo "$(date -Iseconds) установлены новые учётные данные, копия: $(basename "${BAK:-нет}")" >> "$LOG"
echo "УСТАНОВЛЕНО (копия: $(basename "${BAK:-нет}"))"

if HTTPS_PROXY=http://127.0.0.1:9090 HTTP_PROXY=http://127.0.0.1:9090 timeout 90 claude --print "ok" >/dev/null 2>&1; then
  echo "ПРОВЕРКА: claude отвечает"
else
  echo "ПРОВЕРКА НЕ ПРОШЛА: claude не отвечает. Откат: cp ${BAK:-<копия>} $DEST"
  exit 1
fi
