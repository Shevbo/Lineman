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
    # Kind может иметь суффикс ':terse' — применяется caveman-overlay для
    # экономии output. Routing берётся от базового kind.
    raw_kind = job["kind"]
    terse = raw_kind.endswith(":terse")
    base_kind = raw_kind[:-len(":terse")] if terse else raw_kind

    # task-split: спец-обработка. Worker зовёт LLM для генерации списка
    # подзадач, парсит JSON, кладёт каждую в очередь как отдельный job.
    if base_kind == "task-split":
        process_split(job)
        return

    chain = lq.route_for_kind(base_kind)
    system = job.get("system_prompt") or ""
    if terse:
        system = lq.with_terse_overlay(system)
    last_err = ""
    for backend, model in chain:
        try:
            content, tin, tout, latency = lq.call_backend(
                backend=backend, model=model,
                system=system,
                user=job["user_prompt"],
                max_tokens=int(job.get("max_tokens") or 600),
                temperature=float(job.get("temperature") or 0.3),
                agent=f"lazy-worker[{raw_kind}]",
            )
            if not content.strip():
                last_err = f"{backend}/{model}: empty content"
                continue
            lq.complete_job(
                job["id"], output=content, model=model, backend=backend,
                tokens_in=tin, tokens_out=tout, latency_ms=latency,
                kind=job.get("kind", ""),
            )
            saved = lq.compute_saved_usd(backend, job.get("kind", ""), tin, tout)
            saved_str = f" saved=${saved:.4f}" if saved > 0 else ""
            print(f"[lazy] id={job['id']} kind={job['kind']} → {backend}/{model} "
                  f"in={tin} out={tout} lat={latency}ms{saved_str}")
            return
        except Exception as e:
            last_err = f"{backend}/{model}: {type(e).__name__}: {str(e)[:120]}"
            continue
    print(f"[lazy] id={job['id']} kind={job['kind']} FAILED — {last_err}", file=sys.stderr)
    lq.fail_job(job["id"], last_err, int(job.get("retries") or 0))


def process_split(job: dict) -> None:
    """Разбивает большую задачу на N мелких через splitter LLM, кидает
    каждую в очередь с pri=job.priority+1 (немного ниже)."""
    chain = lq.route_for_kind("task-split")
    last_err = ""
    for backend, model in chain:
        try:
            content, tin, tout, latency = lq.call_backend(
                backend=backend, model=model,
                system=lq.SPLIT_SYSTEM,
                user=job["user_prompt"],
                max_tokens=int(job.get("max_tokens") or 800),
                temperature=0.2,
                agent="lazy-worker[task-split]",
            )
            subs = lq.parse_split_response(content)
            if not subs:
                last_err = f"{backend}/{model}: no subtasks parsed"
                continue
            child_ids = []
            child_pri = int(job.get("priority") or 3) + 1
            for st in subs:
                cid = lq.submit_job(
                    from_agent=job.get("from_agent") or "task-split",
                    from_node=job.get("from_node") or "smain",
                    kind=st["kind"],
                    user_prompt=st["prompt"],
                    system_prompt="",
                    max_tokens=st.get("max_tokens", 400),
                    priority=child_pri,
                    deadline_hint_minutes=60,
                )
                child_ids.append(cid)
            output_summary = json.dumps({
                "children": child_ids, "count": len(child_ids),
                "subtasks_preview": [s["kind"] + ":" + s["prompt"][:60] for s in subs],
            }, ensure_ascii=False)
            lq.complete_job(
                job["id"], output=output_summary, model=model, backend=backend,
                tokens_in=tin, tokens_out=tout, latency_ms=latency,
                kind="task-split",
            )
            saved = lq.compute_saved_usd(backend, "task-split", tin, tout)
            print(f"[lazy] id={job['id']} task-split → {len(subs)} children "
                  f"{child_ids}  in={tin} out={tout} lat={latency}ms"
                  + (f" saved=${saved:.4f}" if saved > 0 else ""))
            return
        except Exception as e:
            last_err = f"{backend}/{model}: {type(e).__name__}: {str(e)[:120]}"
            continue
    print(f"[lazy] id={job['id']} task-split FAILED — {last_err}", file=sys.stderr)
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
