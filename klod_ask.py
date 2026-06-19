"""Klod-Access LLM Gateway: единая точка LLM-доступа для агентов федерации.

Боря 2026-06-18: «доступ к LLM делай строго через себя». Этот модуль реализует
политику: агенты не ходят в /proxy/anthropic или /proxy/google напрямую,
а вызывают POST /api/klod/ask {agent, prompt, model_hint?, max_tokens?},
который от имени Klod применяет:
- allowlist агентов (с расширением по конфигу)
- бюджет per-agent (per-hour, per-day)
- резолв model_hint → реальный (provider, model_id)
- аудит каждого запроса в klod_ask.log.jsonl

Чистая логика — без HTTP/I/O. Все side-effects делает proxy_server.
"""
from __future__ import annotations
import json
import os
import pathlib
import time
from typing import Optional


# Дефолтные пресеты модельных подсказок. Агент шлёт model_hint, мы решаем.
# fast=haiku — короткие ответы дёшево. normal=sonnet — рабочая лошадка.
# deep=opus — для аналитики/больших задач (расход выше).
# Gemini как альтернатива когда Anthropic-квота просела или агент явно просит.
MODEL_PRESETS: dict[str, tuple[str, str]] = {
    "fast":           ("anthropic", "claude-haiku-4-5-20251001"),
    "normal":         ("anthropic", "claude-sonnet-4-6"),
    "deep":           ("anthropic", "claude-opus-4-8"),
    "gemini-flash":   ("google",    "gemini-2.5-flash"),
    "gemini-flash-lite": ("google", "gemini-2.5-flash-lite"),
    "gemini-pro":     ("google",    "gemini-2.5-pro"),
    # DeepSeek через Lineman /proxy/deepseek (OpenAI-compat /v1/chat/completions).
    # deepseek-fast = быстрая модель chat. deepseek-reason = reasoning для критика/анализа.
    "deepseek-fast":  ("deepseek",  "deepseek-chat"),
    "deepseek-reason": ("deepseek", "deepseek-reasoner"),
}

VALID_PROVIDERS = {"anthropic", "google", "deepseek", "lm-studio"}

DEFAULT_BUDGET_PER_HOUR = 30
DEFAULT_BUDGET_PER_DAY = 200
DEFAULT_MAX_TOKENS = 1000
HARD_MAX_TOKENS = 4000  # выше — отбиваем 400, чтобы агент не выжег квоту

KLOD_ASK_LOG = pathlib.Path(os.environ.get(
    "KLOD_ASK_LOG", str(pathlib.Path.home() / ".cache/klod_ask.log.jsonl")))


def resolve_model(hint: Optional[str]) -> tuple[str, str]:
    """hint → (provider, model_id). Неизвестный hint падает в 'normal'."""
    if not hint:
        return MODEL_PRESETS["normal"]
    return MODEL_PRESETS.get(hint.strip().lower(), MODEL_PRESETS["normal"])


def resolve_explicit(provider: str, model: str) -> tuple[str, str]:
    """Явные provider+model от вызывающего. Валидируем провайдера."""
    p = (provider or "").strip().lower()
    m = (model or "").strip()
    if p not in VALID_PROVIDERS:
        raise ValueError(f"unknown provider: {provider!r}")
    if not m:
        raise ValueError("model required")
    return p, m


def is_agent_allowed(agent: str, allowlist: Optional[list]) -> bool:
    """allowlist == None или ['*'] → пускаем всех, кто прислал непустой agent.
    Иначе требуем имя в списке."""
    agent = (agent or "").strip()
    if not agent:
        return False
    if not allowlist or allowlist == ["*"]:
        return True
    return agent in allowlist


def check_budget(agent: str, recent: list[tuple[float, str]],
                 budget_cfg: dict, now: float) -> tuple[bool, str]:
    """recent = список (ts, agent_id) за окно (обычно 24ч). Возвращает
    (allowed, reason). При исчерпании — (False, текстовая причина для 429)."""
    per_agent = (budget_cfg.get("agents", {}) or {}).get(agent) or {}
    default = budget_cfg.get("default", {}) or {}
    per_h = int(per_agent.get("per_hour", default.get("per_hour", DEFAULT_BUDGET_PER_HOUR)))
    per_d = int(per_agent.get("per_day", default.get("per_day", DEFAULT_BUDGET_PER_DAY)))
    if per_h <= 0 and per_d <= 0:
        return True, ""
    used_h = sum(1 for ts, a in recent if a == agent and now - ts <= 3600)
    if per_h > 0 and used_h >= per_h:
        return False, f"per-hour budget exhausted: {used_h}/{per_h}"
    used_d = sum(1 for ts, a in recent if a == agent and now - ts <= 86400)
    if per_d > 0 and used_d >= per_d:
        return False, f"per-day budget exhausted: {used_d}/{per_d}"
    return True, ""


def trim_recent(recent: list[tuple[float, str]], now: float,
                window_s: float = 86400) -> list[tuple[float, str]]:
    """Срезать запись старше окна — чтобы in-memory счётчик не рос."""
    cutoff = now - window_s
    return [r for r in recent if r[0] >= cutoff]


def clamp_max_tokens(requested: Optional[int]) -> int:
    """Безопасный потолок: если агент попросил больше HARD_MAX или ничего — 1000."""
    if requested is None:
        return DEFAULT_MAX_TOKENS
    try:
        n = int(requested)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS
    if n <= 0:
        return DEFAULT_MAX_TOKENS
    return min(n, HARD_MAX_TOKENS)


def audit_log(record: dict, path: Optional[pathlib.Path] = None) -> None:
    """JSONL-аудит. Каждая строка = один запрос. Без секретов в логе."""
    p = path or KLOD_ASK_LOG
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # аудит не должен ронять основной поток


def build_request_payload(provider: str, model_id: str, prompt: str,
                          max_tokens: int) -> tuple[str, dict, dict]:
    """Готовит URL-путь (относительно Lineman) и тело + заголовки для LLM-вызова.

    Возвращает (path, body_dict, headers_dict). proxy_server делает реальный POST."""
    if provider == "anthropic":
        path = "/proxy/anthropic/v1/messages"
        body = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        return path, body, headers
    if provider == "google":
        path = f"/proxy/google/v1beta/models/{model_id}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4},
        }
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Name": "klod-access",  # для роутинга Lineman
        }
        return path, body, headers
    if provider == "deepseek":
        # OpenAI-совместимый эндпоинт. Lineman /proxy/deepseek подставляет
        # Bearer-токен из конфига (тем же путём, что eshkola@hoster).
        path = "/proxy/deepseek/v1/chat/completions"
        body = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Name": "klod-access",
        }
        return path, body, headers
    if provider == "lm-studio":
        # OpenAI-совместимый локальный эндпоинт. Без auth — локальный сервис.
        path = "/proxy/lm-studio/v1/chat/completions"
        body = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {"Content-Type": "application/json", "X-Agent-Name": "klod-access"}
        return path, body, headers
    raise ValueError(f"unknown provider: {provider}")


def extract_text(provider: str, response: dict) -> str:
    """Вытащить чистый текст из ответа LLM. Без выдумок — пустая строка если нет."""
    if provider == "anthropic":
        parts = response.get("content") or []
        return "".join(p.get("text", "") for p in parts
                       if isinstance(p, dict) and p.get("type") == "text").strip()
    if provider == "google":
        try:
            candidates = response.get("candidates") or []
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        except Exception:
            return ""
    if provider in ("deepseek", "lm-studio"):
        try:
            choices = response.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            if content:
                return content
            return (msg.get("reasoning_content") or "").strip()
        except Exception:
            return ""
    return ""
