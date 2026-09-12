#!/usr/bin/env bash
# Включить профили поиска в боевом демоне ragkit на sdev.
#
# Почему отдельным скриптом: демон обслуживает и окна разработки STL, поэтому правка
# его файлов и юнита — изменение общего ресурса. Здесь она собрана целиком, с бэкапом
# и проверкой, чтобы выполнялась одним осознанным действием и откатывалась одной строкой.
#
# Что делает: переводит ExecStart на модуль ragkit.httpd_profiles и передаёт ему два
# конфига. Существующий httpd.py НЕ трогается и остаётся запасным вариантом.
# Профиль по умолчанию остаётся stl, поэтому окна разработки ничего не заметят.
#
# Запуск:  bash scripts/fedrag_enable_profiles.sh        (с smain, ходит на sdev по ssh)
# Откат:   bash scripts/fedrag_enable_profiles.sh --rollback
set -euo pipefail

NODE="${FEDRAG_NODE:-sdev}"
UNIT=/etc/systemd/system/ragkit.service
BAK="$UNIT.bak-before-profiles"

if [[ "${1:-}" == "--rollback" ]]; then
  ssh "$NODE" "sudo test -f $BAK && sudo cp $BAK $UNIT && sudo systemctl daemon-reload \
    && sudo systemctl restart ragkit && echo 'откат выполнен'"
  exit 0
fi

ssh "$NODE" bash -s <<'REMOTE'
set -euo pipefail
UNIT=/etc/systemd/system/ragkit.service
BAK="$UNIT.bak-before-profiles"

test -r ~/ragkit/ragkit/httpd_profiles.py \
  || { echo "ОШИБКА: ragkit/httpd_profiles.py не найден на узле" >&2; exit 1; }
test -r ~/ragkit/configs/federation.toml \
  || { echo "ОШИБКА: configs/federation.toml не найден" >&2; exit 1; }

[ -f "$BAK" ] || sudo cp "$UNIT" "$BAK"
echo "бэкап юнита: $BAK"

sudo sed -i \
  's|-m ragkit\.httpd configs/stl\.toml.*|-m ragkit.httpd_profiles configs/stl.toml configs/federation.toml|' \
  "$UNIT"
grep ExecStart "$UNIT"

sudo systemctl daemon-reload
sudo systemctl restart ragkit

# Демон грузит модели на старте: ждём готовности, а не спим вслепую.
for _ in $(seq 1 60); do
  if curl -sS -m 3 http://127.0.0.1:8791/profiles >/dev/null 2>&1; then break; fi
  sleep 5
done

echo "--- профили ---"
curl -sS -m 5 http://127.0.0.1:8791/profiles
echo
REMOTE

echo
echo "--- проверка с smain через Lineman (профиль fed) ---"
curl -sS -m 90 -X POST http://127.0.0.1:9090/api/fedrag/search \
  -H 'X-Agent-Name: klod-access' -H 'Content-Type: application/json' \
  -d '{"query":"что в Lineman нельзя трогать"}' \
  -w '\n[code=%{http_code} t=%{time_total}s]\n' | head -c 300
