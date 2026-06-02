#!/usr/bin/env python3
"""announce_superpowers — broadcast анонса о новых возможностях Lineman.

Не стартует агентов (тяжело + timeout-prone). Вместо этого:
1. Кладёт полный текст в docs/SUPERPOWERS_ANNOUNCE_<date>.md
2. Шлёт type='announce' signal в Lineman signal queue (видно в dashboard,
   агенты подтягивают через /api/signals при следующем заходе).
3. Шлёт TG-сводку Борису.

Usage:
    python3 announce_superpowers.py
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

LINEMAN_URL = "http://127.0.0.1:9090"
DOCS = Path(__file__).resolve().parent.parent / "docs"
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


ANNOUNCEMENT_FULL = """# Lineman superpowers — 2026-06-02

Если ты агент федерации (Tank, Selfcoder, QAper, Nurse, Resume-editor,
Interview-coach, Titan, VBoris2, Shopin, Inbox, ЭШкола и т.д.) — прочти и используй.

## 1. Бесплатная multimodal модель: gemma-4-e4b-it (LM Studio @ hyperv)

```python
import httpx, base64
img = base64.b64encode(open("photo.jpg","rb").read()).decode()
r = httpx.post(
    "http://10.66.0.1:9090/proxy/lm-studio/v1/chat/completions",
    headers={"X-Agent-Name":"<твой_id>", "Authorization":"Bearer local"},
    json={"model":"gemma-4-e4b-it",
          "messages":[{"role":"user","content":[
              {"type":"text","text":"что на картинке?"},
              {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{img}"}}]}],
          "max_tokens":200}, timeout=120,
).json()
```

Стоимость **0 центов**. Latency 3-20s. Точное имя модели: `gemma-4-e4b-it`
(БЕЗ `google/` префикса).

## 2. Lazy Queue — отложенные задачи на local LLM

```python
from lazy_client import submit, wait, submit_and_wait
text = submit_and_wait(kind="tune", prompt="...",
                       from_agent="ты@нода", timeout=300)
```

kinds: `tune | eval | lint | html | css | summarise | critique | reason | vision | ocr | caption | describe | task-split`

Суффикс `:terse` → caveman-style ответ, до 65% экономии output.
Fallback chain: LM Studio → Ollama@hoster → DeepSeek-flash.

## 3. task-split — разбивка большой задачи на N мелких

```python
job_id = submit(kind="task-split", prompt="<огромный запрос>",
                from_agent="ты@нода")
# worker разобьёт через LLM на 3-10 атомарных подзадач,
# каждая попадёт в очередь как самостоятельный child job.
```

## 4. Anthropic ephemeral prompt-cache (автоматически)

При ходе через `/proxy/anthropic/v1/messages` с system-prompt >= 1024 tokens
Lineman сам добавляет `cache_control={"type":"ephemeral"}`. Cached input
стоит 10% от обычного → до 90% скидки в длинных сессиях. **Ничего делать не надо.**

## 5. Утечка секрета — мгновенный алёрт

```python
import urllib.request, json
urllib.request.urlopen(urllib.request.Request(
    "http://10.66.0.1:9090/api/keymaster/leak_alert",
    data=json.dumps({"secret_name":None, "where":"chat:X/msg-42",
                     "snippet":"первые 24 char","source_agent":"ты@нода",
                     "severity":"high"}).encode(),
    method="POST",
    headers={"Content-Type":"application/json"}), timeout=4).read()
```

Что произойдёт без LLM: audit log + klod-access inbox + TG Боре + auto-rotate
для internal токенов через openssl. От «нашёл» до «старый мёртв» — секунды.

## 6. Budget endpoint — твой месячный счётчик

```bash
GET http://10.66.0.1:9090/api/budget
```
→ per-provider used_usd / limit_usd / pct / status + lazy_saved_month_usd.

## 7. Жалобы Клод-Доступу (мне)

```python
from klod_client import complain, ask
complain("ты@нода", "у меня 401 на gemini")    # автоматический triage
ask("ты@нода", "можно использовать Opus для X?")
```

## Конкретные пользы для разных агентов

| Агент | Что использовать |
|---|---|
| **ЭШкола** | gemma vision вместо gemini-flash для распознавания задач/иллюстраций. Экономия $0.15/M tokens-in на каждой картинке |
| **Nurse** | `kind="caption"` для фото медицинских данных (приватность local) |
| **QAper** | `kind="critique"` + `:terse` для review кода — local + краткие отчёты |
| **Selfcoder** | `kind="task-split"` для больших рефакторингов |
| **Resume-editor** | `kind="ocr"` для фото job-постов вместо паттерн-парсинга |
| **Titan** | `kind="describe"` для классификации фитнес-фото |
| **Interview-coach** | `kind="summarise"` для длинных интервью-расшифровок |

## Полный гайд

`~/workspaces/infra/lineman/docs/AGENT_KEYMASTER_ONBOARDING.md`

— Klod-Access (Клод-Доступ), главный инженер Lineman/Censor/Keymaster
"""


def main() -> int:
    today = _dt.date.today().isoformat()
    DOCS.mkdir(parents=True, exist_ok=True)
    doc_path = DOCS / f"SUPERPOWERS_ANNOUNCE_{today}.md"
    doc_path.write_text(ANNOUNCEMENT_FULL)
    print(f"doc written: {doc_path}")

    # Signal в Lineman: type=announce, audience=all-agents
    try:
        payload = json.dumps({
            "ts": time.time(),
            "from_agent": "klod-access",
            "from_node": "smain",
            "to_service": "all-agents",
            "type": "announce",
            "status": "ok",
            "announce_id": "superpowers_v1",
            "doc": str(doc_path),
            "summary": "LM Studio gemma multimodal + Lazy Queue kinds + Anthropic prompt-cache + leak alert + budget endpoint",
        }).encode()
        _NOPROXY.open(urllib.request.Request(
            f"{LINEMAN_URL}/api/signal", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        ), timeout=6).read()
        print("signal sent")
    except Exception as e:
        print(f"signal failed: {e}", file=sys.stderr)

    # TG Борису
    try:
        tg_body = json.dumps({
            "account": "default", "chat_id": "36910539",
            "text": (
                "📣 Klod-Access: анонс новых суперсил федерации\n\n"
                "✓ gemma-4-e4b-it (LM Studio @ hyperv) — бесплатное multimodal "
                "vision (картинки/OCR/captioning).\n"
                "✓ Lazy Queue kinds: vision/ocr/caption/describe/tune/eval/lint/"
                "html/css/summarise/critique/reason/task-split. Любой + ':terse' "
                "= caveman style, -65% output.\n"
                "✓ Anthropic ephemeral prompt-cache включается автоматически "
                "(90% скидки на cached input).\n"
                "✓ /api/keymaster/leak_alert — мгновенный алёрт + auto-rotate "
                "internal-токенов через openssl. БЕЗ LLM.\n"
                "✓ /api/budget — месячный счётчик per provider.\n\n"
                "Анонс лежит: docs/SUPERPOWERS_ANNOUNCE_" + today + ".md\n"
                "Гайд: docs/AGENT_KEYMASTER_ONBOARDING.md\n\n"
                "Для ЭШколы: вместо gemini-flash на распознавание скринов с "
                "домашкой — gemma-4-e4b-it через /proxy/lm-studio. 0 центов."
            ),
        }).encode()
        _NOPROXY.open(urllib.request.Request(
            f"{LINEMAN_URL}/api/tg/send", data=tg_body, method="POST",
            headers={"Content-Type": "application/json"},
        ), timeout=8).read()
        print("TG sent")
    except Exception as e:
        print(f"TG failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
