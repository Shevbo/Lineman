"""Жёсткий дневной кап токенов на провайдера LLM — защита от бесконтрольной траты.

Контекст: deepseek жёг МИЛЛИОНЫ токенов/день бесконтрольно (lazy-фолбэк + краны).
Этот кап даёт ПРЕДСКАЗУЕМЫЙ потолок: при превышении дневного лимита провайдера
reverse_proxy возвращает 429 и не форвардит. День = дата федерации (UTC+3/MSK).

Счётчик in-memory + сид из request_log при старте (рестарт не обнуляет дневной расход).
"""
from __future__ import annotations


class DailyTokenCap:
    def __init__(self, caps: dict | None = None) -> None:
        self.caps: dict[str, int] = {}
        for k, v in (caps or {}).items():
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                self.caps[k] = iv
        self._day: str | None = None
        self._used: dict[str, int] = {}

    def _roll(self, day: str) -> None:
        if day != self._day:
            self._day = day
            self._used = {}

    def allow(self, provider: str, day: str) -> bool:
        """True если провайдеру ещё можно слать (нет капа или не превышен)."""
        self._roll(day)
        cap = self.caps.get(provider)
        if cap is None:
            return True
        return self._used.get(provider, 0) < cap

    def record(self, provider: str, tokens: int, day: str) -> None:
        """Учесть потраченные токены (только для capped-провайдеров)."""
        self._roll(day)
        if provider in self.caps:
            self._used[provider] = self._used.get(provider, 0) + max(0, int(tokens or 0))

    def seed(self, provider: str, tokens: int, day: str) -> None:
        """Сид расхода из request_log при старте (берём максимум, не перетираем вниз)."""
        self._roll(day)
        if provider in self.caps:
            self._used[provider] = max(self._used.get(provider, 0), max(0, int(tokens or 0)))

    def status(self, day: str) -> dict:
        self._roll(day)
        return {
            p: {"used": self._used.get(p, 0), "cap": c,
                "remaining": max(0, c - self._used.get(p, 0))}
            for p, c in self.caps.items()
        }
