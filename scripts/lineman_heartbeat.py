#!/usr/bin/env python3
"""Heartbeat emitter for a federation node — посылает signal в Lineman dashboard.

Запуск (раз в минуту через cron на каждом узле кроме smain):

    * * * * * /usr/bin/python3 /opt/lineman_heartbeat.py >> /tmp/lineman_heartbeat.log 2>&1

Reads optional ~/.openclaw/openclaw.json (agents.list) и emit-ит heartbeat
для каждого зарегистрированного агента. Если файла нет — emit-ит один
node-level heartbeat с from_agent='_node'.

Endpoint берётся из LINEMAN_URL env, default http://10.66.0.1:9090.
"""
from __future__ import annotations
import json
import os
import socket
import sys
import time
import urllib.request

LINEMAN = os.environ.get("LINEMAN_URL", "http://10.66.0.1:9090")
HOSTNAME = socket.gethostname()


def _read_agents() -> list[dict]:
    for cand in [
        os.path.expanduser("~/.openclaw/openclaw.json"),
        "/home/ubuntu/.openclaw/openclaw.json",
        "/home/shevbo/.openclaw/openclaw.json",
    ]:
        try:
            d = json.load(open(cand))
            ag = d.get("agents", {})
            if isinstance(ag, dict) and "list" in ag:
                return ag["list"]
            if isinstance(ag, list):
                return ag
        except Exception:
            continue
    return []


# Bypass any system HTTP_PROXY (Windows boxes often have corp proxy that 407s
# on internal WG addresses). Build a dedicated opener with empty ProxyHandler.
_NOPROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _emit(payload: dict) -> tuple[bool, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{LINEMAN}/api/signal", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _NOPROXY_OPENER.open(req, timeout=4) as r:
            return r.status < 400, str(r.status)
    except Exception as e:
        return False, type(e).__name__ + ":" + str(e)[:80]


def main() -> int:
    agents = _read_agents()
    emitted, failed = 0, 0
    if not agents:
        ok, info = _emit({
            "ts": time.time(),
            "from_agent": "_node",
            "to_service": "lineman",
            "type": "heartbeat",
            "status": "ok",
            "hostname": HOSTNAME,
        })
        print(f"[{time.strftime('%H:%M:%S')}] node-level heartbeat -> {info}")
        return 0 if ok else 1

    for ag in agents:
        aid = ag.get("id")
        if not aid:
            continue
        ok, info = _emit({
            "ts": time.time(),
            "from_agent": aid,
            "to_service": "lineman",
            "type": "heartbeat",
            "status": "ok",
            "hostname": HOSTNAME,
        })
        if ok:
            emitted += 1
        else:
            failed += 1
    print(f"[{time.strftime('%H:%M:%S')}] heartbeat: {emitted} ok, {failed} fail (of {len(agents)})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
