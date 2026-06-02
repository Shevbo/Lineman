"""Client helper для агентов федерации — Lazy Queue.

Использование:
    from lazy_client import submit, wait, submit_and_wait

    job_id = submit(kind="tune", prompt="...", system="ты редактор...",
                    from_agent="eshkola@sdev", priority=3,
                    deadline_hint_minutes=30)

    # poll
    result = wait(job_id, timeout=600)
    # или sync:
    text = submit_and_wait(kind="lint", prompt="...", from_agent="eshkola@sdev",
                          timeout=300)

LINEMAN env override: переменная `LINEMAN_URL` (default http://10.66.0.1:9090).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

LINEMAN_URL = os.environ.get("LINEMAN_URL", "http://10.66.0.1:9090")
_NOPROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LazyError(RuntimeError):
    pass


def _post(path: str, body: dict, timeout: float = 8.0) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{LINEMAN_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _NOPROXY_OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}


def _get(path: str, timeout: float = 8.0) -> dict[str, Any]:
    req = urllib.request.Request(f"{LINEMAN_URL}{path}")
    try:
        with _NOPROXY_OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}


def submit(*, kind: str, prompt: str, from_agent: str,
           system: str = "", max_tokens: int = 600, temperature: float = 0.3,
           priority: int = 3, deadline_hint_minutes: int = 60,
           from_node: str = "smain") -> int:
    """Поставить отложенную задачу. Возвращает job_id."""
    r = _post("/api/queue/lazy", {
        "kind": kind, "prompt": prompt, "system": system,
        "from_agent": from_agent, "from_node": from_node,
        "max_tokens": max_tokens, "temperature": temperature,
        "priority": priority, "deadline_hint_minutes": deadline_hint_minutes,
    })
    if "job_id" not in r:
        raise LazyError(f"submit failed: {r}")
    return int(r["job_id"])


def status(job_id: int) -> dict:
    r = _get(f"/api/queue/lazy/{job_id}")
    if "error" in r:
        raise LazyError(f"status {job_id}: {r['error']}")
    return r


def wait(job_id: int, *, timeout: float = 600.0, poll_interval: float = 3.0) -> dict:
    """Ждать пока job не станет done или failed. Возвращает финальный job dict."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = status(job_id)
        if j.get("status") in ("done", "failed"):
            return j
        time.sleep(poll_interval)
    raise LazyError(f"timeout waiting job {job_id}")


def submit_and_wait(*, kind: str, prompt: str, from_agent: str,
                    system: str = "", max_tokens: int = 600,
                    timeout: float = 300.0, **kw) -> str:
    job_id = submit(kind=kind, prompt=prompt, from_agent=from_agent,
                    system=system, max_tokens=max_tokens, **kw)
    j = wait(job_id, timeout=timeout)
    if j.get("status") == "failed":
        raise LazyError(f"job {job_id} failed: {j.get('error')}")
    return j.get("output") or ""


def list_my(from_agent: str, status_filter: str | None = None) -> list[dict]:
    qs = urllib.parse.urlencode({"from_agent": from_agent, **({"status": status_filter} if status_filter else {})})
    r = _get(f"/api/queue/lazy?{qs}")
    return r.get("jobs", [])


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("usage: lazy_client.py <kind> <agent@node> <prompt> [system]")
        sys.exit(2)
    kind, agent, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
    sysprompt = sys.argv[4] if len(sys.argv) > 4 else ""
    job_id = submit(kind=kind, prompt=prompt, from_agent=agent, system=sysprompt)
    print(f"submitted job_id={job_id}, waiting...")
    j = wait(job_id, timeout=300)
    print(f"status={j['status']} backend={j.get('backend_used')} model={j.get('model_used')} "
          f"in={j.get('tokens_in')} out={j.get('tokens_out')} lat={j.get('latency_ms')}ms")
    print("--- output ---")
    print(j.get("output") or "[empty]")
