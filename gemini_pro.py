"""Пер-агентный грант доступа к Gemini Pro.

Lineman — единственный держатель ключа Gemini и точка прохода всего google-трафика. Боря из ТГ
включает Pro конкретному агенту на ограниченное окно; по истечении Lineman сам возвращает агента
на дешёвую базовую модель. Состояние — рантайм-файл (НЕ config.json, чтобы нажатие кнопки не
триггерило lineman-guard и не требовало рестарта).

Политика моделей (apply_pro_gate):
  - агент с активным грантом → pro-модель проходит как есть
  - агент без гранта → любой `*-pro` переписывается на base_model (по умолчанию gemini-3.5-flash)
  - не-pro запросы не трогаются
Глобальный guard 3.1-pro→2.5-pro живёт в reverse_proxy ДО гейта (беречь 250 RPD).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Callable

# Что считаем pro-моделью в URL-пути (gemini-*-pro, с возможным суффиксом -preview/-latest).
_PRO_RE = re.compile(r"gemini-[0-9.]+-pro")

_DEFAULT_BASE = "gemini-3.5-flash"
_DEFAULT_PRO = "gemini-2.5-pro"


class ProGrants:
    """Стор грантов Pro: {agent: expires_at_epoch}. Атомарная запись, перечитка по mtime."""

    def __init__(self, path: str, default_hours: float = 3.0,
                 clock: Callable[[], float] = time.time):
        self._path = path
        self._default_hours = default_hours
        self._clock = clock
        self._cache: dict[str, float] = {}
        self._load()

    # --- персистентность ---

    def _load(self) -> None:
        # Всегда читаем свежее: файл крошечный, а грант пишет ОДИН процесс (API), читает гейт на
        # хот-пути — mtime-кэш давал флаку (грубое разрешение mtime → ранний возврат отдавал
        # устаревший кэш и агент с грантом получал базу). Корректность > микрооптимизация.
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            self._cache = {str(k): float(v) for k, v in data.items()}
        except FileNotFoundError:
            self._cache = {}
        except Exception:
            # Битый файл → пустые гранты (безопасно: агенты получат базу, не Pro).
            self._cache = {}

    def _save(self) -> None:
        d = os.path.dirname(self._path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".grants-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._cache, f)
            os.replace(tmp, self._path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass

    def _prune(self) -> bool:
        now = self._clock()
        live = {a: exp for a, exp in self._cache.items() if exp > now}
        changed = len(live) != len(self._cache)
        if changed:
            self._cache = live
        return changed

    # --- API ---

    def grant(self, agent: str, hours: float | None = None) -> float:
        self._load()
        h = self._default_hours if hours is None else float(hours)
        expires = self._clock() + h * 3600.0
        self._cache[agent] = expires
        self._prune()
        self._save()
        return expires

    def revoke(self, agent: str) -> None:
        self._load()
        if agent in self._cache:
            del self._cache[agent]
            self._save()

    def is_pro(self, agent: str | None) -> bool:
        if not agent:
            return False
        self._load()
        exp = self._cache.get(agent)
        return exp is not None and exp > self._clock()

    def status(self) -> dict[str, float]:
        """{agent: remaining_seconds} только для активных грантов."""
        self._load()
        now = self._clock()
        return {a: exp - now for a, exp in self._cache.items() if exp > now}


def _models_cfg(config: dict[str, Any] | None) -> tuple[str, str]:
    gp = (config or {}).get("gemini_pro", {}) if config else {}
    return gp.get("base_model", _DEFAULT_BASE), gp.get("pro_model", _DEFAULT_PRO)


def apply_pro_gate(rest_path: str, agent: str | None, grants: ProGrants,
                   config: dict[str, Any] | None) -> str:
    """Если в пути pro-модель и у агента нет активного гранта — переписать на base_model.

    Вызывается в reverse_proxy ПОСЛЕ глобального guard 3.1-pro→2.5-pro и ПОСЛЕ определения agent.
    """
    if "gemini-" not in rest_path or not _PRO_RE.search(rest_path):
        return rest_path
    if grants.is_pro(agent):
        return rest_path
    base, _pro = _models_cfg(config)
    return _PRO_RE.sub(base, rest_path)


# --- Production singleton ---
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_pro_grants.json")
_instance: ProGrants | None = None


def get_grants(default_hours: float = 3.0) -> ProGrants:
    global _instance
    if _instance is None:
        _instance = ProGrants(_DEFAULT_PATH, default_hours=default_hours)
    return _instance
