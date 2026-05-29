"""Маскирование секретов перед записью в request_log.

Используется reverse_proxy и /api/log endpoint. Покрывает:
- "api_key" / "apiKey" / "api-key" / "key" в JSON
- query-параметры key=, api_key=, x-goog-api-key=
- Заголовки Authorization: Bearer ..., x-api-key
- Голые префиксы sk-, AIza, ya29 (Google OAuth), ghp_/ghs_/gho_ (GitHub), <num>:<35char> (TG bot)
- Telegram bot URL: api.telegram.org/bot<TOKEN>/

Маскированная строка сохраняет первые ~6 символов значения для отладки + "***REDACTED***".
"""
from __future__ import annotations

import re
from typing import Any

# Order matters: more specific patterns first.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Telegram bot URL: api.telegram.org/bot<TOKEN>/  — first, very specific
    (re.compile(r'(api\.telegram\.org/bot)([0-9]{6,12}:[A-Za-z0-9_-]{20,})', re.I),
     lambda m: m.group(1) + m.group(2).split(':')[0] + ':***REDACTED***'),

    # Telegram bot token shape (digits:35-char) — must come before generic token= query match
    (re.compile(r'([0-9]{8,12}):[A-Za-z0-9_-]{30,}'),
     lambda m: m.group(1) + ':***REDACTED***'),

    # JSON / dict values, common key naming
    (re.compile(r'("?(?:api[_-]?key|apiKey|x-api-key|botToken|bot[_-]?token|auth[_-]?token|access[_-]?token|client[_-]?secret|secret|password|cred|token)"?\s*[:=]\s*"?)([A-Za-z0-9._\-]{8,})', re.I),
     lambda m: m.group(1) + m.group(2)[:4] + '***REDACTED***'),

    # HTTP headers (Authorization: Bearer ..., x-api-key: ...)
    (re.compile(r'(Authorization\s*:\s*Bearer\s+)([A-Za-z0-9._\-]{12,})', re.I),
     lambda m: m.group(1) + m.group(2)[:4] + '***REDACTED***'),

    # Query parameters: key=..., api_key=..., x-goog-api-key=..., access_token=...
    (re.compile(r'([?&](?:api[_-]?key|key|x-goog-api-key|access[_-]?token|token)=)([A-Za-z0-9._\-]{12,})', re.I),
     lambda m: m.group(1) + m.group(2)[:4] + '***REDACTED***'),

    # OpenAI / Anthropic / OpenRouter prefixes
    (re.compile(r'\b(sk-(?:proj-|ant-|or-v1-)?[A-Za-z0-9_-]{12,})\b'),
     lambda m: m.group(1)[:6] + '***REDACTED***'),

    # Google API keys
    (re.compile(r'\b(AIza[A-Za-z0-9_-]{30,})\b'),
     lambda m: m.group(1)[:6] + '***REDACTED***'),

    # Google OAuth ya29 tokens
    (re.compile(r'\b(ya29\.[A-Za-z0-9._\-]{20,})\b'),
     lambda m: m.group(1)[:8] + '***REDACTED***'),

    # GitHub PAT / OAuth
    (re.compile(r'\b((?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9_]{20,})\b'),
     lambda m: m.group(1)[:6] + '***REDACTED***'),
]


def mask_secrets(text: str | None) -> str | None:
    """Apply all redaction rules and return masked text. None → None."""
    if not text:
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def mask_row(row: dict[str, Any]) -> dict[str, Any]:
    """Mask sensitive fields in a request_log row in place; returns row.

    Touches: request_body, error, target_url, prompt_snippet.
    """
    for k in ("request_body", "error", "target_url", "prompt_snippet"):
        v = row.get(k)
        if isinstance(v, str):
            row[k] = mask_secrets(v)
    return row
