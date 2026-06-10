#!/usr/bin/env python3
"""Выставить кнопку-меню бота Klod на миниаппу. Запускать ОДИН раз когда KLOD_BOT_TOKEN в env.

  KLOD_BOT_TOKEN=... python3 scripts/klod_set_menu_button.py

Открывает у всех чатов с ботом кнопку «Клод» → https://dashboard.shectory.ru/miniapp.
"""
import json
import os
import sys
import urllib.request

TOKEN = (os.environ.get("KLOD_BOT_TOKEN") or "").strip()
URL = os.environ.get("KLOD_MINIAPP_URL", "https://dashboard.shectory.ru/miniapp")

if not TOKEN:
    sys.exit("KLOD_BOT_TOKEN не задан в env")

payload = {
    "menu_button": {
        "type": "web_app",
        "text": "Клод",
        "web_app": {"url": URL},
    }
}
req = urllib.request.Request(
    f"https://api.telegram.org/bot{TOKEN}/setChatMenuButton",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=20) as r:
    body = r.read().decode("utf-8", "ignore")
print(body)
ok = json.loads(body).get("ok")
sys.exit(0 if ok else 1)
