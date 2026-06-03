#!/usr/bin/env python3
"""oauth_refresh_sweep — proactive refresh всех OAuth-секретов Ключника.

Запускается из cron каждые ~30 мин. Сценарий:
- Сканирует manifest.secrets, ищет блоки `oauth`.
- Для каждого вызывает keymaster.oauth_refresh_if_needed(NAME).
- Если refresh не получился и до истечения < 1 часа — P0 TG-алёрт.

Это дополняет lazy-refresh в deliver(): если агент долго не зовёт get(),
токен может протухнуть раньше чем мы заметим. Proactive sweep гарантирует
что refresh случается всегда вовремя.

Cron: 19 * * * * /usr/bin/python3 .../oauth_refresh_sweep.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.home() / "keymaster"))
import keymaster  # noqa: E402

LINEMAN = "http://127.0.0.1:9090"
BORIS_TG = "36910539"
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _tg_alert(text: str) -> None:
    try:
        body = json.dumps({"account": "default", "chat_id": BORIS_TG, "text": text}).encode()
        _NOPROXY.open(urllib.request.Request(
            f"{LINEMAN}/api/tg/send", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        ), timeout=8).read()
    except Exception:
        pass


def main() -> int:
    try:
        m = keymaster._read_manifest()
    except Exception as e:
        print(f"manifest read failed: {e}", file=sys.stderr)
        return 1
    refreshed = skipped = failed = 0
    now = time.time()
    fail_alerts: list[str] = []
    for name, info in (m.get("secrets") or {}).items():
        oauth = info.get("oauth")
        if not isinstance(oauth, dict):
            continue
        res = keymaster.oauth_refresh_if_needed(name)
        if res.get("refreshed"):
            refreshed += 1
            print(f"[oauth-sweep] {name}: refreshed, expires_at={res.get('expires_at')}")
        elif res.get("reason") == "still fresh":
            skipped += 1
        else:
            failed += 1
            expires_at = float(oauth.get("expires_at") or 0)
            ttl_left_h = (expires_at - now) / 3600 if expires_at else None
            print(f"[oauth-sweep] {name}: refresh FAIL — {res.get('error', res.get('reason'))}",
                  file=sys.stderr)
            if ttl_left_h is not None and ttl_left_h < 1:
                fail_alerts.append(
                    f"{name}: refresh failed, истекает через {ttl_left_h * 60:.0f} мин. "
                    f"Reason: {res.get('error', res.get('reason'))[:120]}"
                )

    print(f"[oauth-sweep] refreshed={refreshed} skipped={skipped} failed={failed}")
    if fail_alerts:
        _tg_alert(
            "🚨 OAuth refresh sweep — критичные ошибки\n\n" + "\n\n".join(fail_alerts)
        )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
