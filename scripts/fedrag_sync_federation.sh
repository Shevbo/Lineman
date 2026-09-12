#!/usr/bin/env bash
# Сборка корпуса федерации для индекса fedrag на sdev.
#
# Отличия от первой версии (scripts/sync_federation.sh в ~/ragkit):
#   1. Собирает не только канон smain, но и личности агентов, память проектов,
#      память Lineman и реестр федерации — всё, что агент может спросить.
#   2. Тянет с трёх узлов: smain, hoster, sdev. Документация федерации живёт не на одном.
#   3. Собирает в staging и подменяет корпус только после проверки сканером секретов.
#      Значение секрета, попавшее в индекс, вытаскивается поиском любым агентом,
#      поэтому провал сканера обязан оставить прошлый корпус нетронутым.
#
# Запуск на sdev из ~/ragkit:  bash scripts/fedrag_sync_federation.sh
# Источник правды — этот файл в репозитории Lineman, на sdev он копия.
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$BASE/corpus/federation"
STAGE="$BASE/corpus/.staging-federation"
SCAN="$BASE/scripts/fedrag_secret_scan.py"
PY="${RAGKIT_PY:-$BASE/.venv/bin/python}"

log() { printf '[fedrag-sync] %s\n' "$*"; }

# Узел, на котором крутится сам синк, в себя по ssh не ходит: на sdev ключа для самого
# себя нет, и первый прогон потерял его контракты на Permission denied.
# Имена машин не совпадают с ssh-алиасами федерации (sdev это хост cursorrpa),
# поэтому соответствие задаётся явно, а не угадывается.
SELF="$(hostname -s)"
is_local() {
  case "$1" in
    "$SELF") return 0 ;;
    sdev)    [ "$SELF" = "cursorrpa" ] ;;
    smain)   [ "$SELF" = "smain" ] ;;
    hoster)  [ "$SELF" = "hoster" ] ;;
    *)       return 1 ;;
  esac
}

# Забрать с узла файлы, отобранные find-выражением, в подкаталог staging.
#
# ВАЖНО: find всегда получает конкретный корень ($root), а не «.». Обход от домашнего
# каталога уходит в backups/, кэши и .vscode-server — первая версия на этом зависла
# на несколько минут и была снята.
#
# Пустой результат не считается ошибкой: узел может быть выключен, и это не повод
# ронять весь синк и оставлять индекс без обновления.
pull() {
  local node="$1" subdir="$2" root="$3" findexpr="$4"
  local into="$STAGE/$subdir"
  mkdir -p "$into"
  local before after runner
  before=$(find "$into" -type f 2>/dev/null | wc -l)

  if is_local "$node"; then
    runner="bash -c"
  else
    runner="ssh -o ConnectTimeout=10 $node"
  fi

  # Корпус пересобирается с нуля, поэтому разовый сетевой сбой молча выкидывает
  # целый источник. Три попытки: сеть до smain уже роняла синк на ровном месте.
  local attempt
  for attempt in 1 2 3; do
    if $runner \
          "cd ~ && [ -e '$root' ] && find '$root' $findexpr -print0 2>/dev/null \
           | tar czf - --null -T - 2>/dev/null" \
        | tar xzf - -C "$into" 2>/dev/null; then
      after=$(find "$into" -type f 2>/dev/null | wc -l)
      if [ "$after" -gt "$before" ] || [ "$attempt" = 3 ]; then
        log "  $root: +$((after - before)) файлов"
        return 0
      fi
    fi
    [ "$attempt" -lt 3 ] && sleep $((attempt * 3))
  done
  log "  $root: недоступен или пусто после 3 попыток, пропускаю"
}

rm -rf "$STAGE"
mkdir -p "$STAGE"

# Пути внутри staging повторяют пути на smain: в выдаче агент видит привычное
# `docs/FEDERATION_AGENT_ONBOARDING.md`, а не искусственный префикс. Скрытые каталоги
# вроде `.claude` индексатор не режет — он отбрасывает только DEFAULT_EXCLUDE_DIRS.

# ---------------------------------------------------------------- smain: канон
# Ядро: корневые .md, docs/, memory/. Без этого синк не имеет смысла.
log "smain: канон"
pull smain . '.'      '-maxdepth 1 -name "*.md"'
pull smain . 'docs'   '\( -name "*.md" -o -name "*.txt" \) -not -name "*.bak*"'
pull smain . 'memory' '\( -name "*.md" -o -name "*.txt" \) -not -name "*.bak*"'

# ------------------------------------------------- smain: личности и память агентов
# AGENTS.md / CLAUDE.md каждого воркспейса — контракт агента: кто он, что ему можно,
# куда отвечать. Раньше не индексировалось, хотя агенты спрашивают именно это.
# maxdepth 3 держит обход внутри воркспейсов и не даёт уйти в их node_modules и сборки.
log "smain: личности агентов"
pull smain . 'workspaces' \
  '-maxdepth 3 \( -name "AGENTS.md" -o -name "CLAUDE.md" \) -not -path "*/node_modules/*"'

# Память Lineman: архитектура, критические пути, журнал инцидентов.
log "smain: память Lineman"
pull smain . 'workspaces/infra/lineman/.claude/memory' '-name "*.md"'

# Проектная память Claude: накопленные грабли, решения, инциденты по каждому проекту.
log "smain: память проектов"
pull smain . '.claude/projects' \
  '-path "*/memory/*" -name "*.md" -not -name "*.bak*"'

# ------------------------------------------------------- машинные реестры в markdown
# include_ext индекса — только md и txt, поэтому JSON рендерится в таблицы.
# Из config.json берётся ТОЛЬКО agents.node_map, credentials не читаются.
log "smain: реестр и карта узлов в markdown"
mkdir -p "$STAGE/docs/generated"
# Вывод генератора уводится в stderr: его строки, попав в stdout, ломают поток tar —
# на этом первый прогон и потерял реестры.
if ssh -o ConnectTimeout=10 smain \
      "python3 ~/workspaces/infra/lineman/scripts/fedrag_render_registry.py /tmp/fedrag_gen 1>&2 \
       && cd /tmp/fedrag_gen && tar czf - ." \
    | tar xzf - -C "$STAGE/docs/generated" 2>/dev/null; then
  log "  реестры: $(find "$STAGE/docs/generated" -type f | wc -l) файлов"
else
  log "  реестры: не сгенерированы, пропускаю"
fi

# ------------------------------------------------------- hoster и sdev: контракты
# Документация федерации живёт не только на smain: у агентов на других узлах свои
# AGENTS.md, и агент, спрашивающий про соседа, должен их находить.
for node in hoster sdev; do
  log "$node: контракты агентов"
  pull "$node" "nodes/$node" '.' \
    '-maxdepth 3 \( -name "AGENTS.md" -o -name "CLAUDE.md" \) -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/backups/*"'
done

# ---------------------------------------------------- шлюз: корпус не должен усыхать
# Сканер секретов на пустом staging отработает «чисто», и подмена уничтожит корпус.
# Поэтому до него — проверка объёма: если новый корпус заметно меньше прошлого,
# значит источник отвалился, а не документация исчезла.
NEW_COUNT=$(find "$STAGE" \( -name '*.md' -o -name '*.txt' \) | wc -l)
OLD_COUNT=0
[ -d "$DEST" ] && OLD_COUNT=$(find "$DEST" \( -name '*.md' -o -name '*.txt' \) | wc -l)
MIN_COUNT=$(( OLD_COUNT * 80 / 100 ))

log "файлов: было $OLD_COUNT, стало $NEW_COUNT"
if [ "$NEW_COUNT" -lt 50 ] || { [ "$OLD_COUNT" -gt 0 ] && [ "$NEW_COUNT" -lt "$MIN_COUNT" ]; }; then
  log "ОСТАНОВЛЕНО: корпус усох ($NEW_COUNT против $OLD_COUNT, порог $MIN_COUNT)."
  log "Похоже, узел или источник был недоступен. Корпус НЕ тронут."
  log "Staging оставлен для разбора: $STAGE"
  exit 1
fi

# ------------------------------------------------------------ шлюз: сканер секретов
log "проверка на секреты перед подменой корпуса"
if ! "$PY" "$SCAN" "$STAGE"; then
  log "ОСТАНОВЛЕНО: в staging найдены похожие на секреты значения."
  log "Корпус НЕ тронут, индекс продолжает работать на прошлой версии."
  log "Staging оставлен для разбора: $STAGE"
  exit 1
fi

# --------------------------------------------------------------------- подмена
# Прошлый корпус сохраняется в $DEST.prev до следующего успешного синка: если новая
# выдача окажется хуже, откат делается одним mv, без повторного обхода узлов.
log "чисто, подменяю корпус"
rm -rf "$DEST.prev"
[ -d "$DEST" ] && mv "$DEST" "$DEST.prev"
mv "$STAGE" "$DEST"

log "готово: $DEST"
du -sh "$DEST"
printf '[fedrag-sync] файлов для индекса: %s\n' \
  "$(find "$DEST" \( -name '*.md' -o -name '*.txt' \) | wc -l)"
