#!/usr/bin/env python3
"""announce_local_models — broadcast об обновлении документации по локальным моделям.

1. Пишет doc docs/LOCAL_MODELS_ANNOUNCE_<date>.md
2. Кладёт файл в inbox каждого агента на smain
3. Шлёт type='announce' signal в Lineman
4. TG Борису

Usage:
    python3 announce_local_models.py
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
INBOX_BASE = Path("/home/shectory/workspaces/inbox")
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

TODAY = _dt.date.today().isoformat()

ANNOUNCE_TEXT = f"""# Обновление: локальные модели LM Studio — {TODAY}

Документация обновлена. Прочти FEDERATION.md (раздел "БЕСПЛАТНЫЕ ЛОКАЛЬНЫЕ МОДЕЛИ")
и docs/AGENT_KEYMASTER_ONBOARDING.md (секция 3.5).

## Что изменилось

### LM Studio — три модели, бесплатно

| Модель | Задачи |
|--------|--------|
| `gemma-4-e4b-it` | OCR, vision, распознавание картинок |
| `gemma-4-26b-a4b-it-imatrix` | Суммаризация, HTML, длинный контекст |
| `deepseek-r1-distill-qwen-14b` | Рассуждения, сложный анализ |

**Единый эндпоинт (Lineman роутит через Pi→hyperv, детали скрыты):**

| Узел | URL |
|------|-----|
| smain | `http://127.0.0.1:9090/proxy/lm-studio/v1/chat/completions` |
| sdev, hoster, pi, cloud | `http://10.66.0.1:9090/proxy/lm-studio/v1/chat/completions` |
| vibe (Windows) | `http://127.0.0.1:19090/proxy/lm-studio/v1/chat/completions` |

Если hyperv выключен — автоматический фолбэк через Lazy Queue.

### Lazy Queue — OCR и vision через JSON-массив

ВАЖНО: формат user_prompt для vision-задач — JSON-массив (НЕ строка, НЕ dict):

```python
import json, base64
from lazy_client import submit_and_wait

img_b64 = base64.b64encode(open("image.png","rb").read()).decode()
vision_parts = [
    {{"type": "image_url", "image_url": {{"url": f"data:image/png;base64,{{img_b64}}"}}}},
    {{"type": "text", "text": "Extract all text from this image."}},
]
text = submit_and_wait(
    kind="ocr",
    prompt=json.dumps(vision_parts),  # ← JSON-массив!
    from_agent="<твой_id>@<нода>",
    max_tokens=2000, timeout=300,
)
```

### Параллельность

4 воркера одновременно. Для batch OCR страниц — все 4 идут к LM Studio параллельно.
Throughput ~4 страниц/20 секунд при включённом hyperv.

### Batch-OCR PDF

На smain есть `scripts/ocr_batch.py` для batch-OCR учебников:

```bash
ssh smain "cd /home/shectory/workspaces/infra/lineman && \\
  .venv/bin/python3 scripts/ocr_batch.py \\
  --pdf /путь/к/book.pdf --agent <твой_id>@<нода> \\
  --out /tmp/result.json --workers 4"
```

### Связь с Клод-Доступом (Klod-Access)

```bash
# С любого узла через WG:
curl -X POST "http://10.66.0.1:9090/api/agent/klod-access/message?from=<id>&node=<нода>" -d "Сообщение"

# С vibe:
curl --noproxy "*" -X POST "http://127.0.0.1:19090/api/agent/klod-access/message?from=<id>&node=vibe" -d "Сообщение"
```

### Полная документация

- `FEDERATION.md` (~/.../FEDERATION.md) → раздел "БЕСПЛАТНЫЕ ЛОКАЛЬНЫЕ МОДЕЛИ"
- `docs/AGENT_KEYMASTER_ONBOARDING.md` → секция 3.5
- `docs/ESHKOLA_KEYMASTER_ONBOARDING.md` → секция "Бесплатный OCR и vision"
"""


def post(url: str, body: dict) -> None:
    _NOPROXY.open(urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    ), timeout=8).read()


def write_inbox(agent_id: str, filename: str, text: str) -> None:
    inbox_dir = INBOX_BASE / agent_id
    if inbox_dir.exists():
        (inbox_dir / filename).write_text(text, encoding="utf-8")
        print(f"  inbox/{agent_id}/{filename}")
    else:
        print(f"  skip inbox/{agent_id}/ (not found)")


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    doc_path = DOCS / f"LOCAL_MODELS_ANNOUNCE_{TODAY}.md"
    doc_path.write_text(ANNOUNCE_TEXT, encoding="utf-8")
    print(f"doc: {doc_path}")

    # Кладём в inbox агентов на smain
    print("writing agent inboxes...")
    fname = f"LOCAL_MODELS_ANNOUNCE_{TODAY}.md"
    for agent in ("main", "tank", "selfcoder", "qaper"):
        write_inbox(agent, fname, ANNOUNCE_TEXT)

    # Lineman signal
    try:
        post(f"{LINEMAN_URL}/api/signal", {
            "ts": time.time(),
            "from_agent": "klod-access",
            "from_node": "smain",
            "to_service": "all-agents",
            "type": "announce",
            "status": "ok",
            "announce_id": "local_models_v2",
            "doc": str(doc_path),
            "summary": (
                "LM Studio 3 models + correct Lazy Queue vision format + "
                "4 parallel workers + ocr_batch.py + vibe direct LAN access "
                "(192.168.1.70:1234) + FEDERATION.md updated"
            ),
        })
        print("signal sent")
    except Exception as e:
        print(f"signal failed: {e}", file=sys.stderr)

    # TG Борису
    try:
        post(f"{LINEMAN_URL}/api/tg/send", {
            "account": "default",
            "chat_id": "36910539",
            "text": (
                "[Klod-Access] Обновление документации: локальные модели\n\n"
                "FEDERATION.md и оба ONBOARDING-файла обновлены.\n\n"
                "Что нового:\n"
                "- LM Studio: 3 модели, бесплатно (gemma-4e4b, gemma-26b, deepseek-r1)\n"
                "- Lazy Queue vision: правильный формат user_prompt — JSON-массив\n"
                "- 4 параллельных воркера (был 1)\n"
                "- ocr_batch.py — batch OCR PDF/изображений\n"
                "- vibe: прямой LAN к LM Studio http://192.168.1.70:1234\n"
                "- ЭШкола ONBOARDING: полный раздел про OCR + Klod-Access API\n\n"
                f"Анонс: docs/LOCAL_MODELS_ANNOUNCE_{TODAY}.md\n"
                "Inbox агентов smain обновлён."
            ),
        })
        print("TG sent")
    except Exception as e:
        print(f"TG failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
