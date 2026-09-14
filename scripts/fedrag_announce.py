#!/usr/bin/env python3
"""Вставить блок про индекс fedrag в первые строки личности каждого агента федерации.

Блок обрамлён маркерами и переписывается целиком при повторном прогоне, поэтому
скрипт идемпотентен: второй запуск не плодит копии и подхватывает правки текста.

Ставится в НАЧАЛО файла (сразу после заголовка первого уровня, если он есть) — иначе
агент дочитает до инструкции уже после того, как полез искать ответ руками.

Использование:
    python3 fedrag_announce.py --dry-run   # показать, что изменится
    python3 fedrag_announce.py             # применить
"""
import os
import re
import sys

BEGIN = "<!-- fedrag BEGIN — блок обновляется Клодом, правки внутри затрутся -->"
END = "<!-- fedrag END -->"

TEMPLATE = """%s
> ## Канон федерации: спрашивай индекс, не читай файлы целиком
>
> Прежде чем искать ответ по файлам или писать Клоду — спроси индекс:
>
> ```bash
> curl -sS -m 60 -X POST http://10.66.0.1:9090/api/fedrag/search \\
>   -H 'X-Agent-Name: %s' -H 'Content-Type: application/json' \\
>   -d '{"query":"свой вопрос обычными словами"}' | jq -r .text
> ```
>
> Узел без WireGuard (Windows, IoT) — тот же запрос через ssh-jump на Pi:
> `echo '{"query":"..."}' | KLOD_AGENT=<ваш agent_id> ~/.claude/skills/onboarding/bin/klod_http.sh POST /api/fedrag/search - application/json`
>
> В индексе: канон онбординга, контракты всех агентов федерации, карта узлов, реестр
> компонентов, журналы инцидентов, проектная память. Ответ приходит с путём файла и
> номерами строк — первоисточник потом читается точечно, а не целиком.
>
> - `"source":"ragkit"` или `"cache"` — нормальный ответ.
> - `"source":"stale"` — индекс недоступен, ответ из кэша, возраст в `age_s`.
>   Для чтения годится; для необратимого действия сверься с первоисточником.
> - `"source":"fallback"` — индекса нет: действуй по `.onboarding/AGENT.md`,
>   чего там нет — спроси Клода. **Не выдумывай.**
>
> Лимит 20 запросов в минуту на агента: демон один на всю федерацию.
%s
""" % (BEGIN, "%s", END)


def build_block(agent_id):
    return TEMPLATE % agent_id


def strip_old(text):
    """Убрать прежний блок вместе с маркерами."""
    pattern = re.compile(
        re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n*",
        re.DOTALL,
    )
    return pattern.sub("", text)


def insert(text, block):
    """Вставить блок в начало, но после заголовка первого уровня, если он есть."""
    text = strip_old(text)
    lines = text.split("\n")
    at = 0
    for i, line in enumerate(lines[:5]):
        if line.startswith("# "):
            at = i + 1
            while at < len(lines) and not lines[at].strip():
                at += 1
            break
    head = "\n".join(lines[:at])
    tail = "\n".join(lines[at:])
    if head and not head.endswith("\n"):
        head += "\n"
    return (head + "\n" if head else "") + block + "\n" + tail


def main():
    dry = "--dry-run" in sys.argv
    home = os.path.expanduser("~")
    ws = os.path.join(home, "workspaces")

    # Личности лежат не по одному шаблону: на smain это ~/workspaces/<агент>/, на sdev
    # так же, на hoster часть каталогов прямо в доме. Поэтому обходим дом до третьего
    # уровня, отсекая заведомо не-агентское.
    SKIP = {"node_modules", ".venv", "venv", ".git", "backups", "__pycache__", "dist",
            "build", ".cache", ".pm2", ".claude", ".openclaw", "site-packages",
            ".vscode-server", ".cursor-server", "ragkit-src", "corpus", "_archive"}

    targets = []
    seen = set()
    for dirpath, dirnames, filenames in os.walk(home):
        depth = dirpath[len(home):].count(os.sep)
        if depth >= 3:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
        for fn in ("AGENTS.md", "CLAUDE.md"):
            if fn not in filenames:
                continue
            path = os.path.join(dirpath, fn)
            if path in seen:
                continue
            seen.add(path)
            name = os.path.basename(dirpath)
            # Контракт уровня дома читают все агенты узла — конкретный id туда нельзя.
            agent_id = "<ваш agent_id>" if dirpath == home else name
            targets.append((agent_id, path))
    targets.sort(key=lambda t: t[1])

    changed = skipped = 0
    for agent_id, path in targets:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        new = insert(src, build_block(agent_id))
        if new == src:
            skipped += 1
            print("  без изменений  %s" % path.replace(home, "~"))
            continue
        changed += 1
        state = "БУДЕТ ОБНОВЛЁН" if dry else "обновлён"
        print("  %-15s %s" % (state, path.replace(home, "~")))
        if not dry:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)

    print("\nфайлов: %d, изменено: %d, без изменений: %d" % (len(targets), changed, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
