"""Мониторинг тикетов Билдера на дашборде — шейпинг данных для /api/builder/tickets."""
from proxy_server import build_builder_status


def test_builder_status_summary_and_shape():
    tickets = [
        {"id": "t1", "repo_path": "/x/garden-manager", "task": "a" * 80,
         "kind": "normal", "status": "pr_open", "branch": "builder/t1",
         "pr_url": "http://pr/1", "evidence": {"tests": "77 passed"}},
        {"id": "t2", "repo_path": "/home/u/lineman", "task": "fix bug",
         "kind": "critical", "status": "failed", "branch": "builder/t2",
         "pr_url": "", "evidence": {"claude": "limit"}},
        {"id": "t3", "repo_path": "/x/g", "task": "q", "kind": "normal",
         "status": "queued", "branch": "", "pr_url": "", "evidence": {}},
    ]
    audit = [{"event": "built", "ticket": "t1", "status": "pr_open", "ts": "2026-06-07T09:22:13"}]
    s = build_builder_status(tickets, audit)

    assert s["total"] == 3
    assert s["summary"]["pr_open"] == 1
    assert s["summary"]["failed"] == 1
    assert s["summary"]["queued"] == 1

    t1 = next(t for t in s["tickets"] if t["id"] == "t1")
    assert t1["repo"] == "garden-manager"          # basename
    assert len(t1["task"]) <= 120                   # обрезка длинной задачи
    assert t1["status"] == "pr_open" and t1["pr_url"] == "http://pr/1"
    assert "77 passed" in t1["tests"]

    assert s["audit"][-1]["ticket"] == "t1"


def test_builder_status_empty():
    s = build_builder_status([], [])
    assert s["total"] == 0
    assert s["tickets"] == []
    assert s["summary"] == {}
    assert s["audit"] == []


def test_builder_status_newest_tickets_first():
    tickets = [{"id": "old", "repo_path": "/x/a", "task": "t", "status": "merged",
                "kind": "normal", "branch": "", "pr_url": "", "evidence": {},
                "created_at": "2026-06-01T00:00:00"},
               {"id": "new", "repo_path": "/x/b", "task": "t", "status": "queued",
                "kind": "normal", "branch": "", "pr_url": "", "evidence": {},
                "created_at": "2026-06-07T00:00:00"}]
    s = build_builder_status(tickets, [])
    assert s["tickets"][0]["id"] == "new"           # новые сверху
