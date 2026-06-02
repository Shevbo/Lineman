#!/usr/bin/env python3
"""lazy_worker — long-running PM2 процесс, обрабатывает Lazy Queue.

Алгоритм:
1. `claim_next()` атомарно берёт следующий job (status=queued, priority asc, id asc).
2. Если нет — sleep IDLE_SLEEP_S и далее.
3. Иначе: route_for_kind(job.kind) → пройти fallback chain.
4. На успехе `complete_job`. На полном фейле `fail_job` (retries++, max 3 → status=failed).

Запуск как PM2:
    npx pm2 start /home/shectory/workspaces/infra/lineman/scripts/lazy_worker.py \
        --name lazy-worker --interpreter /home/shectory/workspaces/infra/lineman/.venv/bin/python3
"""
from __future__ import annotations

import os
import sys
import time

THIS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(THIS))
import lazy_queue as lq  # noqa: E402

IDLE_SLEEP_S = float(os.environ.get("LAZY_IDLE_SLEEP_S", "5"))


def process(job: dict) -> None:
    chain = lq.route_for_kind(job["kind"])
    last_err = ""
    for backend, model in chain:
        try:
            content, tin, tout, latency = lq.call_backend(
                backend=backend, model=model,
                system=job.get("system_prompt") or "",
                user=job["user_prompt"],
                max_tokens=int(job.get("max_tokens") or 600),
                temperature=float(job.get("temperature") or 0.3),
                agent=f"lazy-worker[{job['kind']}]",
            )
            if not content.strip():
                last_err = f"{backend}/{model}: empty content"
                continue
            lq.complete_job(
                job["id"], output=content, model=model, backend=backend,
                tokens_in=tin, tokens_out=tout, latency_ms=latency,
            )
            print(f"[lazy] id={job['id']} kind={job['kind']} → {backend}/{model} "
                  f"in={tin} out={tout} lat={latency}ms")
            return
        except Exception as e:
            last_err = f"{backend}/{model}: {type(e).__name__}: {str(e)[:120]}"
            continue
    print(f"[lazy] id={job['id']} kind={job['kind']} FAILED — {last_err}", file=sys.stderr)
    lq.fail_job(job["id"], last_err, int(job.get("retries") or 0))


def main() -> int:
    print(f"[lazy-worker] starting, idle-sleep={IDLE_SLEEP_S}s")
    while True:
        try:
            job = lq.claim_next()
        except Exception as e:
            print(f"[lazy-worker] claim error: {e}", file=sys.stderr)
            time.sleep(IDLE_SLEEP_S)
            continue
        if not job:
            time.sleep(IDLE_SLEEP_S)
            continue
        # дедлайн просрочен — отметить и пропустить
        if job.get("deadline_ts") and time.time() > float(job["deadline_ts"]):
            lq.complete_job(
                job["id"], output="[expired before worker reached it]",
                model="-", backend="-", tokens_in=0, tokens_out=0, latency_ms=0,
            )
            print(f"[lazy] id={job['id']} expired")
            continue
        try:
            process(job)
        except Exception as e:
            lq.fail_job(job["id"], f"worker exc: {type(e).__name__}: {e}", int(job.get("retries") or 0))


if __name__ == "__main__":
    sys.exit(main())
