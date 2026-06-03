# Обновление: локальные модели LM Studio — 2026-06-03

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
    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
    {"type": "text", "text": "Extract all text from this image."},
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
ssh smain "cd /home/shectory/workspaces/infra/lineman && \
  .venv/bin/python3 scripts/ocr_batch.py \
  --pdf /путь/к/book.pdf --agent <твой_id>@<нода> \
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
