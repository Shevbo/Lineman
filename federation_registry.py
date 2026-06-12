"""Резолвер репозитория федерации по тексту задачи (Боря: Klod/Builder сами подбирают репо,
если сомневаются — предлагают похожие). Источник — federation_registry.json (build_registry.py)."""
from __future__ import annotations
import json
import os
import re

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "federation_registry.json")


def load_registry(path: str = DEFAULT_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"components": [], "count": 0}


def _toks(s: str) -> set:
    return set(re.findall(r"[a-zа-яё0-9]{3,}", (s or "").lower()))


def score(query: str, comp: dict) -> int:
    q = (query or "").lower()
    qt = _toks(query)
    s = 0
    for kw in comp.get("keywords", []):
        if kw and kw.lower() in q:           # keyword-фраза как подстрока — сильный сигнал
            s += 2
    s += len(_toks(comp.get("id", "")) & qt)  # совпадение токенов id/имени
    s += len(_toks(comp.get("name", "")) & qt)
    return s


def resolve(query: str, registry: dict, top: int = 4) -> dict:
    """Вернуть {best, best_score, confident, candidates[]}. confident=True → можно брать best.path
    без вопросов; False → предложить candidates на выбор."""
    comps = registry.get("components", []) if isinstance(registry, dict) else (registry or [])
    scored = sorted(((score(query, c), c) for c in comps), key=lambda x: -x[0])
    ranked = [c for sc, c in scored if sc > 0]
    best_sc = scored[0][0] if scored else 0
    second = scored[1][0] if len(scored) > 1 else 0
    best = scored[0][1] if scored and best_sc > 0 else None
    # уверены: явный лидер с отрывом >=2 от второго
    confident = bool(best) and best_sc >= 2 and (best_sc - second) >= 2
    return {
        "query": query,
        "best": best,
        "best_score": best_sc,
        "confident": confident,
        "candidates": ranked[:top],
    }
