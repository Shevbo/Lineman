"""Вотчдог Клода (#4): детерминированные проверки стандартов / безопасности / доков
федерации. Чистые функции (тестируемы на in-memory данных); запуск по диску — в
scripts/klod_watchdog.py. Боря: Клод следит за стандартами, секретами, дрейфом доков.

Severity: high (секрет/нарушение безопасности), medium (стандарт), low (доки/гигиена).
"""
from __future__ import annotations
import re
from dataclasses import dataclass, asdict

# Паттерны утечки секретов в трекаемых файлах (значения секретов в коде/доках — нельзя).
SECRET_PATTERNS = [
    ("deepseek/openai key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]

# Плейсхолдеры/примеры — не считаем утечкой.
_ALLOW_SUBSTR = ("sk-xxx", "sk-...", "sk-your", "ghp_xxx", "EXAMPLE", "<token>",
                 "123456:ABC", "your-key", "REDACTED", "sk-e9b", "sk-ba7")


@dataclass
class Violation:
    check: str
    severity: str
    path: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _is_allowed(line: str) -> bool:
    low = line.lower()
    return any(a.lower() in low for a in _ALLOW_SUBSTR)


def scan_text_for_secrets(text: str, path: str = "") -> list[Violation]:
    """Найти секреты в тексте файла (построчно, плейсхолдеры игнорируются)."""
    out: list[Violation] = []
    for i, line in enumerate(text.splitlines(), 1):
        if _is_allowed(line):
            continue
        for name, rx in SECRET_PATTERNS:
            if rx.search(line):
                out.append(Violation("secret_leak", "high", f"{path}:{i}",
                                     f"похоже на {name}"))
                break
    return out


def scan_files_for_secrets(files: dict[str, str]) -> list[Violation]:
    out: list[Violation] = []
    for path, text in files.items():
        out.extend(scan_text_for_secrets(text, path))
    return out


def check_required_docs(present_paths: set[str], required: list[str]) -> list[Violation]:
    """Канонические доки должны существовать (анти-амнезия)."""
    return [Violation("canonical_docs", "low", r, "канонический документ отсутствует")
            for r in required if r not in present_paths]


def check_paid_route_leak(routes_text: str) -> list[Violation]:
    """В lazy_queue не должно быть платного deepseek (инцидент: жёг миллионы/день)."""
    out: list[Violation] = []
    low = routes_text.lower()
    if "deepseek" in low and "lm-studio" not in low and "ollama" not in low:
        out.append(Violation("paid_route_leak", "high", "lazy_queue",
                             "платный deepseek в lazy_queue без локального фолбэка"))
    return out


def check_token_caps(config: dict) -> list[Violation]:
    """Должен быть дневной кап на deepseek (контроль трат)."""
    caps = (config.get("reverse_proxy", {}) or {}).get("daily_token_caps", {}) or {}
    if "deepseek" not in caps:
        return [Violation("token_caps", "medium", "config.json",
                          "нет дневного капа токенов на deepseek")]
    return []


def build_report(violations: list[Violation], now_iso: str = "") -> dict:
    by_sev: dict[str, int] = {}
    for v in violations:
        by_sev[v.severity] = by_sev.get(v.severity, 0) + 1
    return {
        "generated": now_iso,
        "total": len(violations),
        "by_severity": by_sev,
        "ok": len(violations) == 0,
        "violations": [v.to_dict() for v in violations],
    }


def signature(violations: list[Violation]) -> set[str]:
    """Идентичность набора нарушений — для алерта только на НОВЫЕ."""
    return {f"{v.check}|{v.path}|{v.detail}" for v in violations}


def new_violations(prev: list[dict], cur: list[Violation]) -> list[Violation]:
    prev_sig = {f"{p.get('check')}|{p.get('path')}|{p.get('detail')}" for p in prev}
    return [v for v in cur if f"{v.check}|{v.path}|{v.detail}" not in prev_sig]
