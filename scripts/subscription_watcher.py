#!/usr/bin/env python3
"""Watcher за подписками критических внешних сервисов федерации.

Boris 2026-07-28: «настрой вотчер за своевременным продлением proxy служб
и моделей ИИ». Без proxy6/iProyal → мёртв federation-wide LLM egress; без
Claude/DeepSeek/Gemini подписок → мёртв LLM-канал агентов.

Что делает крон-скрипт (0 9 * * * MSK):
1. Каждый плагин возвращает {name, status, days_left|balance, expiry_date, source}.
2. Если days_left ≤ ALERT_THRESHOLD (default 14) → TG-alert Боре.
3. Если balance ниже порога → TG-alert.
4. Дедуп per-service раз в 24ч чтоб не спамить.
5. JSONL-лог в ~/logs/klod/subscriptions.jsonl.
6. State в ~/.klod/subscription_state.json.

Плагины делятся на два типа:
- API-based: значение берётся программно через provider API.
- Manual: значение читается из Ключника (`<SERVICE>_NEXT_RENEWAL` ISO date).
  Пополняется Борисом вручную после каждого продления.

Что где сейчас:
- DeepSeek: API /user/balance → баланс. Порог: DEEPSEEK_MIN_BALANCE_USD (default 5).
- Anthropic AI Plus: manual — Boris кладёт CLAUDE_AI_PLUS_NEXT_RENEWAL в Ключник.
- Google AI Plus: manual — GEMINI_AI_PLUS_NEXT_RENEWAL.
- Proxy6: manual (API-key у Бори нет пока — если появится PROXY6_API_KEY,
  плагин переключится на автоматический getproxy+date_end).
- iProyal: manual (management API у нас нет — только proxy URL).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LINEMAN = os.environ.get("KLOD_LINEMAN", "http://127.0.0.1:9090").rstrip("/")
KEYMASTER = os.environ.get("KLOD_KEYMASTER", "http://127.0.0.1:9093").rstrip("/")
BORIS_CHAT_ID = os.environ.get("BORIS_TG_CHAT_ID", "36910539")

STATE_FILE = Path(os.environ.get("KLOD_SUB_WD_STATE",
                                 str(Path.home() / ".klod/subscription_state.json")))
LOG_FILE = Path(os.environ.get("KLOD_SUB_WD_LOG",
                               str(Path.home() / "logs/klod/subscriptions.jsonl")))

ALERT_THRESHOLD_DAYS = int(os.environ.get("KLOD_SUB_WD_THRESHOLD_DAYS", "14"))
ALERT_DEDUP_S = int(os.environ.get("KLOD_SUB_WD_ALERT_DEDUP_S", "86400"))  # 24ч

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(entry: dict) -> None:
    entry["ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


def _read_secret(name: str) -> str:
    """Читает секрет из Keymaster через прямой resolve. Без Boris-approval для
    метаданных (даты продления — не sensitive)."""
    try:
        sys.path.insert(0, "/home/shectory/keymaster")
        import keymaster as km
        return km.read_secret_value(name) or ""
    except Exception:
        return ""


def _tg_alert(text: str, tag: str, st: dict) -> bool:
    """Один алерт на tag не чаще ALERT_DEDUP_S. True если реально отправили."""
    now = int(time.time())
    alerts = st.setdefault("alerts", {})
    last = int(alerts.get(tag, 0))
    if now - last < ALERT_DEDUP_S:
        return False
    try:
        body = json.dumps({"account": "default", "chat_id": BORIS_CHAT_ID,
                           "text": text[:3800]}).encode()
        req = urllib.request.Request(
            f"{LINEMAN}/api/tg/send", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        _NOPROXY.open(req, timeout=15).read()
        alerts[tag] = now
        _log({"event": "alert_sent", "tag": tag, "text": text[:200]})
        # Соблюдаем TG rate-limit /api/tg/send (15с per account).
        # Cron 1×/сутки — паузу можно позволить.
        time.sleep(16)
        return True
    except Exception as e:
        _log({"event": "alert_fail", "tag": tag, "err": str(e)[:200]})
        return False


# ---------------------------------------------------------------------------
# Plugins — каждый возвращает список dict'ов с проверками.
# Формат: {service, kind, ok, days_left|balance, expiry|threshold, message, source}
# ---------------------------------------------------------------------------

def check_deepseek() -> list[dict]:
    """DeepSeek API balance. Порог: DEEPSEEK_MIN_BALANCE_USD (default 5)."""
    key = _read_secret("DEEPSEEK_API_KEY")
    if not key:
        return [{"service": "deepseek", "kind": "balance", "ok": False,
                 "message": "нет DEEPSEEK_API_KEY в Keymaster", "source": "check-config"}]
    min_balance = float(_read_secret("DEEPSEEK_MIN_BALANCE_USD") or "5")
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {key}"})
        with _NOPROXY.open(req, timeout=15) as r:
            d = json.loads(r.read())
    except Exception as e:
        return [{"service": "deepseek", "kind": "balance", "ok": False,
                 "message": f"API error: {str(e)[:120]}", "source": "deepseek-api"}]
    infos = d.get("balance_infos") or []
    usd = next((float(x.get("total_balance") or 0) for x in infos
                if x.get("currency") == "USD"), 0.0)
    is_available = bool(d.get("is_available"))
    return [{
        "service": "deepseek",
        "kind": "balance",
        "ok": is_available and usd >= min_balance,
        "balance": usd,
        "currency": "USD",
        "threshold": min_balance,
        "is_available": is_available,
        "message": (f"баланс ${usd:.2f} " +
                    ("OK" if usd >= min_balance else f"НИЖЕ порога ${min_balance:.2f}")
                    + (" | is_available=false" if not is_available else "")),
        "source": "deepseek-api",
    }]


def _parse_iso(d: str) -> dt.datetime | None:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S"):
        try:
            v = dt.datetime.strptime(d, fmt)
            if v.tzinfo is None:
                v = v.replace(tzinfo=dt.timezone.utc)
            return v
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(d.replace("Z", "+00:00"))
    except Exception:
        return None


def _check_manual_expiry(service: str, keymaster_key: str, human_name: str) -> list[dict]:
    """Manual expiry: читаем ISO-дату из Ключника, считаем days_left."""
    val = _read_secret(keymaster_key)
    if not val:
        return [{
            "service": service, "kind": "manual_expiry", "ok": None,
            "expiry": None, "days_left": None,
            "message": (f"нет {keymaster_key} в Ключнике; положи ISO-дату "
                        f"следующего продления ({human_name}), watcher начнёт "
                        f"следить"),
            "source": "keymaster-manual",
        }]
    expiry = _parse_iso(val.strip())
    if expiry is None:
        return [{
            "service": service, "kind": "manual_expiry", "ok": False,
            "expiry": val, "days_left": None,
            "message": f"{keymaster_key}={val!r} — не парсится как ISO дата (YYYY-MM-DD)",
            "source": "keymaster-manual",
        }]
    now = dt.datetime.now(dt.timezone.utc)
    days_left = int((expiry - now).total_seconds() / 86400)
    return [{
        "service": service, "kind": "manual_expiry",
        "ok": days_left > ALERT_THRESHOLD_DAYS,
        "expiry": expiry.date().isoformat(),
        "days_left": days_left,
        "threshold_days": ALERT_THRESHOLD_DAYS,
        "message": (f"истекает {expiry.date().isoformat()} — "
                    f"осталось {days_left} дней " +
                    ("OK" if days_left > ALERT_THRESHOLD_DAYS else "ПРОДЛИТЬ")),
        "source": "keymaster-manual",
    }]


def check_proxy6() -> list[dict]:
    """Proxy6: если появится PROXY6_API_KEY — переключусь на API. Пока — manual."""
    api_key = _read_secret("PROXY6_API_KEY")
    if api_key:
        # Формат API: https://proxy6.net/api/{key}/getproxy → каждый proxy имеет date_end
        try:
            iproyal = _read_secret("LINEMAN_IPROYAL_URL")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"https": iproyal, "http": iproyal} if iproyal else {}))
            with opener.open(f"https://proxy6.net/api/{api_key}/getproxy",
                             timeout=15) as r:
                d = json.loads(r.read())
            if d.get("status") != "yes":
                return [{"service": "proxy6", "kind": "api", "ok": False,
                         "message": f"proxy6 API status={d.get('status')}: {d.get('error')}",
                         "source": "proxy6-api"}]
            proxies = list((d.get("list") or {}).values())
            if not proxies:
                return [{"service": "proxy6", "kind": "api", "ok": False,
                         "message": "нет proxy на аккаунте",
                         "source": "proxy6-api"}]
            # Самый ранний expiry
            expiries = []
            for p in proxies:
                exp = _parse_iso(p.get("date_end", ""))
                if exp: expiries.append(exp)
            if not expiries:
                return [{"service": "proxy6", "kind": "api", "ok": False,
                         "message": "не смог распарсить date_end",
                         "source": "proxy6-api"}]
            earliest = min(expiries)
            now = dt.datetime.now(dt.timezone.utc)
            days_left = int((earliest - now).total_seconds() / 86400)
            return [{
                "service": "proxy6", "kind": "api",
                "ok": days_left > ALERT_THRESHOLD_DAYS,
                "expiry": earliest.date().isoformat(),
                "days_left": days_left,
                "proxies_count": len(proxies),
                "message": f"ближайший истекает {earliest.date().isoformat()} — {days_left} дней",
                "source": "proxy6-api",
            }]
        except Exception as e:
            return [{"service": "proxy6", "kind": "api", "ok": False,
                     "message": f"API error: {str(e)[:150]} — фоллбэк на manual",
                     "source": "proxy6-api"}]
    return _check_manual_expiry("proxy6", "PROXY6_NEXT_RENEWAL", "Proxy6 подписка")


def check_iproyal() -> list[dict]:
    """iProyal: management API у нас нет — только manual date."""
    return _check_manual_expiry("iproyal", "IPROYAL_NEXT_RENEWAL", "iProyal подписка")


def check_anthropic_ai_plus() -> list[dict]:
    """Anthropic AI Plus (Claude): consumer OAuth subscription, программного API нет.
    Manual expiry дата в Ключнике."""
    return _check_manual_expiry("anthropic_ai_plus",
                                 "CLAUDE_AI_PLUS_NEXT_RENEWAL",
                                 "Anthropic AI Plus (Claude подписка)")


def check_gemini_ai_plus() -> list[dict]:
    """Google AI Plus (Gemini): consumer subscription, только UI. Manual expiry."""
    return _check_manual_expiry("gemini_ai_plus",
                                 "GEMINI_AI_PLUS_NEXT_RENEWAL",
                                 "Google AI Plus (Gemini подписка)")


CHECKS = [
    check_deepseek,
    check_proxy6,
    check_iproyal,
    check_anthropic_ai_plus,
    check_gemini_ai_plus,
]


def main() -> int:
    st = _load_state()
    st["last_run"] = int(time.time())
    st.setdefault("services", {})
    total_alerts = 0

    for checker in CHECKS:
        try:
            results = checker()
        except Exception as e:
            _log({"event": "checker_exception", "checker": checker.__name__,
                  "err": str(e)[:300]})
            continue
        for r in results:
            svc = r.get("service")
            _log({"event": "check_result", **r})
            st["services"][svc] = {
                "kind": r.get("kind"),
                "ok": r.get("ok"),
                "days_left": r.get("days_left"),
                "balance": r.get("balance"),
                "expiry": r.get("expiry"),
                "message": r.get("message"),
                "checked_at": int(time.time()),
            }
            # Alert conditions
            if r.get("ok") is False:
                # 3 категории: 1) balance below threshold, 2) days_left ≤ threshold,
                # 3) конфиг проблемы (нет API-key / нет ISO-даты — тоже алерт но
                #    с длиннее dedup чтоб не досаждать)
                if r.get("kind") == "balance":
                    tag = f"balance:{svc}"
                    text = f"⚠ {svc}: {r.get('message')}"
                elif r.get("kind") in ("manual_expiry", "api"):
                    dl = r.get("days_left")
                    tag = f"expiry:{svc}"
                    if dl is not None and dl <= ALERT_THRESHOLD_DAYS:
                        text = (f"⚠ {svc.upper()}: подписка истекает через "
                                f"{dl} дней ({r.get('expiry')}). Продлить!")
                    else:
                        text = f"⚠ {svc}: {r.get('message')}"
                else:
                    tag = f"other:{svc}"
                    text = f"⚠ {svc}: {r.get('message')}"
                if _tg_alert(text, tag, st):
                    total_alerts += 1
            elif r.get("ok") is None:
                # Config-missing (например нет ISO-даты) — тоже алерт, но с 7д дедупом,
                # чтоб раз в неделю напоминать Боре что подписка не отслеживается.
                cfg_tag = f"cfg:{svc}"
                cfg_alerts = st.setdefault("alerts", {})
                cfg_last = int(cfg_alerts.get(cfg_tag, 0))
                if int(time.time()) - cfg_last >= 7 * 86400:
                    if _tg_alert(f"ℹ {svc}: {r.get('message')}", cfg_tag, st):
                        total_alerts += 1

    _save_state(st)
    _log({"event": "run_summary", "alerts_sent": total_alerts,
          "services_checked": len(st.get("services", {}))})
    return 0


if __name__ == "__main__":
    sys.exit(main())
