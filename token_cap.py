"""Жёсткий дневной кап токенов на провайдера LLM — защита от бесконтрольной траты.

Контекст: deepseek жёг МИЛЛИОНЫ токенов/день бесконтрольно (lazy-фолбэк + краны).
Этот кап даёт ПРЕДСКАЗУЕМЫЙ потолок: при превышении дневного лимита провайдера
reverse_proxy возвращает 429 и не форвардит. День = дата федерации (UTC+3/MSK).

Счётчик in-memory + сид из request_log при старте (рестарт не обнуляет дневной расход).
"""
from __future__ import annotations


class DailyTokenCap:
    def __init__(self, caps: dict | None = None,
                 agent_caps: dict | None = None) -> None:
        self.caps: dict[str, int] = {}
        for k, v in (caps or {}).items():
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                self.caps[k] = iv
        # Per-agent дневной кап: {"<agent>": {"<provider>": tokens}}. Защита от
        # одного жадного потребителя, роняющего общий аккаунт (career-bot 71M/сутки).
        self.agent_caps: dict[str, dict[str, int]] = {}
        for ag, pv in (agent_caps or {}).items():
            row = {}
            for p, v in (pv or {}).items():
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if iv > 0:
                    row[p] = iv
            if row:
                self.agent_caps[ag] = row
        self._day: str | None = None
        self._used: dict[str, int] = {}
        self._agent_used: dict[tuple[str, str], int] = {}

    def _roll(self, day: str) -> None:
        if day != self._day:
            self._day = day
            self._used = {}
            self._agent_used = {}

    def allow(self, provider: str, day: str) -> bool:
        """True если провайдеру ещё можно слать (нет капа или не превышен)."""
        self._roll(day)
        cap = self.caps.get(provider)
        if cap is None:
            return True
        return self._used.get(provider, 0) < cap

    def allow_agent(self, agent: str, provider: str, day: str) -> bool:
        """True если конкретному агенту ещё можно к провайдеру (per-agent кап)."""
        self._roll(day)
        cap = (self.agent_caps.get(agent) or {}).get(provider)
        if cap is None:
            return True
        return self._agent_used.get((agent, provider), 0) < cap

    def agent_cap(self, agent: str, provider: str) -> int | None:
        return (self.agent_caps.get(agent) or {}).get(provider)

    def record(self, provider: str, tokens: int, day: str,
               agent: str | None = None) -> None:
        """Учесть потраченные токены (для capped-провайдеров и capped-агентов)."""
        self._roll(day)
        t = max(0, int(tokens or 0))
        if provider in self.caps:
            self._used[provider] = self._used.get(provider, 0) + t
        if agent and (self.agent_caps.get(agent) or {}).get(provider) is not None:
            self._agent_used[(agent, provider)] = self._agent_used.get((agent, provider), 0) + t

    def seed(self, provider: str, tokens: int, day: str,
             agent: str | None = None) -> None:
        """Сид расхода из request_log при старте (берём максимум, не перетираем вниз)."""
        self._roll(day)
        t = max(0, int(tokens or 0))
        if provider in self.caps:
            self._used[provider] = max(self._used.get(provider, 0), t)
        if agent and (self.agent_caps.get(agent) or {}).get(provider) is not None:
            k = (agent, provider)
            self._agent_used[k] = max(self._agent_used.get(k, 0), t)

    def status(self, day: str) -> dict:
        self._roll(day)
        return {
            p: {"used": self._used.get(p, 0), "cap": c,
                "remaining": max(0, c - self._used.get(p, 0))}
            for p, c in self.caps.items()
        }
