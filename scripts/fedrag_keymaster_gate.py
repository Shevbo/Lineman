#!/usr/bin/env python3
"""Второй гейт корпуса: сверка с реальными значениями Ключника.

Решение Бориса 2026-09-18: «RAG и все агенты могут работать без ограничений, но
фильтр секретов нужно ставить до выгрузки в RAG — для этого есть Ключник».

Зачем он рядом с шаблонным гейтом, а не вместо. `fedrag_secret_scan.py` ищет то, что
ПОХОЖЕ на ключ известного вида: sk-ant-..., AIza..., ghp_..., токен бота. Он полезен
против ключей, которых Ключник не знает — чужих, новых, случайно вставленных. Но
пароль от прокси или SHECTORY_AUTH_BRIDGE_SECRET выглядят как обычная строка, ни под
один шаблон не подходят и прошли бы свободно. Этот гейт закрывает ровно ту дыру:
он знает, что именно является нашим секретом.

Значения при этом никуда не уезжают. Текст уходит Ключнику, у которого они и так
есть, а обратно приходят только имена — см. keymaster.scan_documents.

Выход: 0 — чисто, 1 — найдено, 2 — проверить не удалось (Ключник недоступен).
Код 2 отделён намеренно: «не смог проверить» это не то же самое, что «чисто»,
и выгрузку в этом случае продолжать нельзя.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

KEYMASTER = os.environ.get("KEYMASTER_URL", "http://127.0.0.1:9093")
# Пакет ограничен по объёму, а не по числу файлов: корпус содержит и карточки на
# пару килобайт, и журналы инцидентов на сотни. Считать штуками — значит однажды
# собрать пакет в десятки мегабайт и упереться в таймаут.
BATCH_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 120

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz",
                 ".woff", ".woff2", ".ico", ".mp3", ".mp4", ".bin", ".so", ".pyc")

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def collect(root: str) -> list[dict]:
    """Текстовые файлы корпуса как [{id, text}]. id — путь относительно корня."""
    docs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        for fn in filenames:
            if fn.lower().endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            docs.append({"id": os.path.relpath(full, root), "text": text})
    return docs


def batches(docs: list[dict]):
    cur, size = [], 0
    for doc in docs:
        n = len(doc["text"])
        if cur and size + n > BATCH_BYTES:
            yield cur
            cur, size = [], 0
        cur.append(doc)
        size += n
    if cur:
        yield cur


def ask_keymaster(docs: list[dict]) -> dict:
    body = json.dumps({"documents": docs}).encode("utf-8")
    req = urllib.request.Request(f"{KEYMASTER}/keymaster/scan", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with _NOPROXY.open(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    if len(sys.argv) < 2:
        print("использование: fedrag_keymaster_gate.py <каталог корпуса>",
              file=sys.stderr)
        return 2
    root = sys.argv[1]
    if not os.path.isdir(root):
        print(f"нет каталога: {root}", file=sys.stderr)
        return 2

    docs = collect(root)
    if not docs:
        print("[гейт-ключник] в корпусе нет текстовых файлов — нечего проверять")
        return 0

    checked, flagged = 0, []
    for batch in batches(docs):
        try:
            res = ask_keymaster(batch)
        except urllib.error.HTTPError as e:
            print(f"[гейт-ключник] Ключник ответил {e.code}: проверка не выполнена",
                  file=sys.stderr)
            return 2
        except Exception as e:
            print(f"[гейт-ключник] Ключник недоступен ({type(e).__name__}): "
                  f"проверка не выполнена", file=sys.stderr)
            return 2
        checked += int(res.get("checked") or 0)
        flagged.extend(res.get("flagged") or [])

    if not flagged:
        print(f"[гейт-ключник] чисто: {checked} файлов сверено со значениями Ключника")
        return 0

    print(f"[гейт-ключник] НАЙДЕНЫ СЕКРЕТЫ в {len(flagged)} файлах "
          f"из {checked}:", file=sys.stderr)
    for item in flagged[:40]:
        # Печатаем путь и ИМЕНА секретов. Значений нет ни здесь, ни в ответе Ключника.
        print("  %s -> %s" % (item["id"], ", ".join(item["names"])), file=sys.stderr)
    if len(flagged) > 40:
        print(f"  ... и ещё {len(flagged) - 40}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
