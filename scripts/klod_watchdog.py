#!/usr/bin/env python3
"""Runner вотчдога Клода (#4). Гоняет проверки по репо Lineman, пишет отчёт
~/.klod/watchdog.json, алертит Борю (через Lineman /api/tg/send) на НОВЫЕ high-нарушения.

Запуск: python3 scripts/klod_watchdog.py  (по таймеру/крону, напр. каждый час).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from watchdog import (scan_text_for_secrets, check_required_docs, check_paid_route_leak,
                      check_token_caps, build_report, new_violations, Violation)

REPORT = os.path.expanduser(os.environ.get("KLOD_WATCHDOG_REPORT", "~/.klod/watchdog.json"))
LINEMAN = os.environ.get("LINEMAN_BASE", "http://127.0.0.1:9090")
MSK = timezone(timedelta(hours=3))
REQUIRED_DOCS = ["docs/PORTAL_AUTH_STANDARD.md", "docs/FEDERATION_SKILLS.md",
                 "WIKI.md", "TZ_LINEMAN.md"]
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".db", ".pdf", ".ico", ".woff", ".woff2"}
# tests/ содержат фейковые ключи-фикстуры — не утечки. cloudflare-worker secrets деплоятся отдельно.
SKIP_PREFIXES = ("tests/",)
MAX_BYTES = 256 * 1024


def tracked_files(root: str) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True,
                             text=True, timeout=30)
        return [l for l in out.stdout.splitlines() if l]
    except Exception:
        return []


def run() -> dict:
    violations: list[Violation] = []
    files = tracked_files(ROOT)
    present = set(files)

    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        if ext in SKIP_EXT or rel.startswith(SKIP_PREFIXES):
            continue
        ap = os.path.join(ROOT, rel)
        try:
            if os.path.getsize(ap) > MAX_BYTES:
                continue
            with open(ap, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            continue
        violations.extend(scan_text_for_secrets(text, rel))

    violations.extend(check_required_docs(present, REQUIRED_DOCS))

    lq = os.path.join(ROOT, "lazy_queue.py")
    if os.path.exists(lq):
        violations.extend(check_paid_route_leak(open(lq, encoding="utf-8", errors="ignore").read()))

    cfg = os.path.join(ROOT, "config.json")
    if os.path.exists(cfg):
        try:
            violations.extend(check_token_caps(json.load(open(cfg, encoding="utf-8"))))
        except Exception:
            pass

    now_iso = datetime.now(MSK).strftime("%Y-%m-%d %H:%M MSK")
    report = build_report(violations, now_iso)

    prev = []
    if os.path.exists(REPORT):
        try:
            prev = json.load(open(REPORT, encoding="utf-8")).get("violations", [])
        except Exception:
            prev = []
    fresh = new_violations(prev, violations)
    report["new_count"] = len(fresh)

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    tmp = REPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REPORT)

    fresh_high = [v for v in fresh if v.severity == "high"]
    if fresh_high:
        alert(fresh_high)
    return report


def alert(vs: list[Violation]) -> None:
    lines = "\n".join(f"• [{v.severity}] {v.check}: {v.path} — {v.detail}" for v in vs[:10])
    text = f"🛡 Вотчдог Клода: {len(vs)} НОВЫХ high-нарушений\n{lines}"
    try:
        data = json.dumps({"account": "default", "chat_id": "36910539", "text": text}).encode()
        req = urllib.request.Request(f"{LINEMAN}/api/tg/send", data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("alert failed:", e, file=sys.stderr)


if __name__ == "__main__":
    r = run()
    print(json.dumps({"total": r["total"], "new": r.get("new_count", 0),
                      "by_severity": r["by_severity"], "generated": r["generated"]},
                     ensure_ascii=False))
