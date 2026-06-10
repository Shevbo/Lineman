"""Валидация Telegram Mini App initData (для миниаппы Клода).

Telegram подписывает initData ключом бота:
  secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  hash       = HMAC_SHA256(key=secret_key,  msg=data_check_string)
data_check_string — все поля кроме hash, отсортированы, "k=v" через \\n.

Новые клиенты добавляют поле `signature` (Ed25519, для сторонней проверки). Часть
клиентов включает его в data_check_string при расчёте hash, часть — нет. Чтобы не гадать,
пробуем оба варианта (без signature и с ним).
"""
from __future__ import annotations
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def _check(data: dict, recv_hash: str, bot_token: str) -> bool:
    dcs = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, recv_hash)


def validate_init_data(init_data: str, bot_token: str,
                       max_age: int | None = 86400, now: float | None = None) -> dict | None:
    """Вернуть {'user': {...}, 'auth_date': int} если подпись валидна, иначе None."""
    if not init_data or not bot_token:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv_hash = data.pop("hash", "")
    if not recv_hash:
        return None
    # Вариант A: signature остаётся в строке проверки. Вариант B: исключаем его.
    ok = _check(dict(data), recv_hash, bot_token)
    if not ok and "signature" in data:
        d2 = dict(data)
        d2.pop("signature", None)
        ok = _check(d2, recv_hash, bot_token)
    if not ok:
        return None
    if max_age is not None:
        try:
            ad = int(data.get("auth_date", "0"))
        except ValueError:
            return None
        t = now if now is not None else time.time()
        if ad and (t - ad) > max_age:
            return None
    user = {}
    raw_user = data.get("user")
    if raw_user:
        try:
            user = json.loads(raw_user)
        except Exception:
            user = {}
    return {"user": user, "auth_date": data.get("auth_date")}


def user_id_allowed(parsed: dict | None, allowed_ids: set[str]) -> bool:
    if not parsed:
        return False
    uid = str((parsed.get("user") or {}).get("id", ""))
    return bool(uid) and uid in allowed_ids
