"""Client helper для агентов federation — единственный легальный способ
получать секреты из Ключника (smain :9093 через WG 10.66.0.1:9093).

Контракт:
- Значения секретов живут только в RAM процесса (in-memory cache).
- Никаких файлов, env-переменных, логов с реальными значениями.
- Идентификация: requester="<agent_id>@<node>" (e.g. "eshkola@sdev").
- При первом запросе с нового requester'а — pending в Keymaster +
  TG-аппрув Бори. Pre-approved кеш в manifest пропускает TG-этап.

Usage:
    from klod_keymaster import get, release, on_rotation
    key = get("ESHKOLA_DEEPSEEK", requester="eshkola@sdev",
              purpose="dev — пишу LLM-клиент")

    # на key_rotated signal:
    on_rotation("ESHKOLA_DEEPSEEK")  # сбросит cache, следующий get() заберёт свежий

Env override:
    KEYMASTER_URL  default http://10.66.0.1:9093
    LINEMAN_URL    default http://10.66.0.1:9090   (для совместимости)
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from threading import RLock

KEYMASTER_URL = os.environ.get("KEYMASTER_URL", "http://10.66.0.1:9093")
DEFAULT_NODE = os.environ.get("LINEMAN_NODE", socket.gethostname())
POLL_TIMEOUT_S = 60.0     # ждём аппрува Бори до 60 сек на первый запрос
POLL_INTERVAL_S = 2.0

# Bypass system HTTP_PROXY (windows boxes + corp proxy на WG = 407)
_NOPROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_cache: dict[str, str] = {}
_lock = RLock()


class KeymasterError(RuntimeError):
    pass


def _http(method: str, path: str, params: dict | None = None, body: dict | None = None,
          timeout: float = 6.0) -> dict:
    qs = "?" + urllib.parse.urlencode(params) if params else ""
    url = f"{KEYMASTER_URL}{path}{qs}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _NOPROXY_OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        raise KeymasterError(f"{type(e).__name__}: {e}") from e


def _request_value(name: str, requester: str, purpose: str) -> str:
    """Полный цикл: request-value → poll pending → deliver."""
    res = _http("POST", "/keymaster/request-value",
                params={"name": name, "requester": requester, "purpose": purpose})
    if "error" in res:
        raise KeymasterError(f"request-value failed: {res['error']}")

    req_id = res.get("request_id")
    status = res.get("status")
    if not req_id:
        raise KeymasterError(f"no request_id: {res}")

    # status=approved (pre_approved fast-path) → deliver already prepared
    if status == "approved":
        return _deliver(req_id, name)

    # else: pending — wait for Boris
    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        try:
            v = _deliver(req_id, name)
            return v
        except KeymasterError:
            continue
    raise KeymasterError(f"timeout waiting Boris approval for {name} (req_id={req_id}). "
                         f"Once Boris approves in TG, restart will pick it up via pre_approved.")


def _deliver(req_id: str, name: str) -> str:
    res = _http("GET", "/keymaster/deliver", params={"request_id": req_id})
    if "value" in res:
        return res["value"]
    err = res.get("error", "no value")
    raise KeymasterError(f"deliver {name}: {err}")


def get(name: str, *, requester: str, purpose: str = "") -> str:
    """Получить значение секрета. Кешируется в RAM до release/restart."""
    name = name.upper()
    with _lock:
        if name in _cache:
            return _cache[name]
    val = _request_value(name, requester, purpose)
    with _lock:
        _cache[name] = val
    return val


def release(name: str | None = None) -> None:
    """Сбросить cache (для всего или одного ключа). После release следующий
    get() полезет в Keymaster за свежим значением."""
    with _lock:
        if name is None:
            _cache.clear()
        else:
            _cache.pop(name.upper(), None)


def on_rotation(name: str) -> None:
    """Вызывать когда пришёл signal type=key_rotated key_name=<name>.
    Эквивалент release(name) — следующий get() заберёт свежее."""
    release(name)


LINEMAN_URL = os.environ.get("LINEMAN_URL", "http://10.66.0.1:9090")
_watcher_thread = None
_watcher_stop = False


def start_rotation_watcher(requester: str, *, interval: float = 30.0) -> None:
    """Запускает фоновый поток, который опрашивает Lineman /api/signals,
    ловит type=key_rotated для нашего requester и автоматически вызывает
    on_rotation(name). Безопасно вызывать многократно — повторно не запустит.

    Без этого агент должен сам poll'ить signals или вручную звать on_rotation.
    """
    global _watcher_thread, _watcher_stop
    if _watcher_thread and _watcher_thread.is_alive():
        return

    import threading

    def _loop() -> None:
        agent_id = requester.split("@", 1)[0]
        since = time.time()
        while not _watcher_stop:
            try:
                req = urllib.request.Request(
                    f"{LINEMAN_URL}/api/signals?since={since}&limit=50"
                )
                with _NOPROXY_OPENER.open(req, timeout=8) as r:
                    sigs = json.loads(r.read()).get("signals", [])
                for s in sigs:
                    if float(s.get("ts", 0)) > since:
                        since = float(s["ts"])
                    if s.get("type") == "key_rotated" and s.get("to_service") == agent_id:
                        kn = s.get("key_name") or s.get("name")
                        if kn:
                            on_rotation(kn)
            except Exception:
                pass
            time.sleep(interval)

    _watcher_stop = False
    _watcher_thread = threading.Thread(target=_loop, daemon=True, name="klod-kmw")
    _watcher_thread.start()


def stop_rotation_watcher() -> None:
    global _watcher_stop
    _watcher_stop = True


def manifest(name: str | None = None) -> dict:
    """Метаданные секрета (БЕЗ значения). Безопасно логировать."""
    params = {"name": name} if name else None
    return _http("GET", "/keymaster/manifest", params=params)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: klod_keymaster.py <NAME> <requester@node> [purpose]")
        print("       klod_keymaster.py --manifest [NAME]")
        sys.exit(2)
    if sys.argv[1] == "--manifest":
        print(json.dumps(manifest(sys.argv[2] if len(sys.argv) > 2 else None),
                         ensure_ascii=False, indent=2))
        sys.exit(0)
    name = sys.argv[1]
    requester = sys.argv[2]
    purpose = sys.argv[3] if len(sys.argv) > 3 else "manual cli probe"
    try:
        v = get(name, requester=requester, purpose=purpose)
        # Не печатаем значение, только подтверждение
        print(f"OK: got {name} (len={len(v)}, prefix={v[:4]}***)")
    except KeymasterError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
