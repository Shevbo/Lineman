"""Трекер бэклога Клода (#7). Боря складывает задачи (через миниаппу), отдельные
пункты промоутятся в очередь Билдера. Хранилище — JSON ~/.klod/backlog.json.

Статусы: new (лежит) → sent (отправлено Билдеру, привязан ticket_id) → done.
"""
from __future__ import annotations
import json
import os
import time

DEFAULT_PATH = "~/.klod/backlog.json"
_VALID_STATUS = {"new", "sent", "done"}


class BacklogStore:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = os.path.expanduser(path)

    def _load(self) -> list:
        try:
            return json.loads(open(self.path, encoding="utf-8").read()) \
                if os.path.exists(self.path) else []
        except Exception:
            return []

    def _save(self, items: list) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(items, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)

    def add(self, title: str, note: str = "", repo: str = "",
            priority: str = "normal", now: float | None = None) -> dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("title required")
        ts = int(now if now is not None else time.time() * 1000)
        items = self._load()
        bid = f"b{ts}"
        while any(it.get("id") == bid for it in items):
            ts += 1
            bid = f"b{ts}"
        item = {"id": bid, "title": title, "note": (note or "").strip(),
                "repo": (repo or "").strip(), "priority": priority,
                "status": "new", "ticket_id": "", "created": ts}
        items.append(item)
        self._save(items)
        return item

    def list(self) -> list:
        return self._load()

    def get(self, bid: str) -> dict | None:
        for it in self._load():
            if it.get("id") == bid:
                return it
        return None

    def set_status(self, bid: str, status: str,
                   ticket_id: str | None = None) -> dict | None:
        if status not in _VALID_STATUS:
            raise ValueError(f"bad status {status}")
        items = self._load()
        found = None
        for it in items:
            if it.get("id") == bid:
                it["status"] = status
                if ticket_id is not None:
                    it["ticket_id"] = ticket_id
                found = it
        if found:
            self._save(items)
        return found

    def remove(self, bid: str) -> bool:
        items = self._load()
        kept = [it for it in items if it.get("id") != bid]
        if len(kept) != len(items):
            self._save(kept)
            return True
        return False

    def summary(self) -> dict:
        s: dict = {}
        for it in self._load():
            st = it.get("status", "new")
            s[st] = s.get(st, 0) + 1
        return s


def enqueue_builder_ticket(queue_path: str, repo: str, task: str,
                           frm: str = "klod-backlog", now: float | None = None) -> str:
    """Аппенд тикета в очередь Билдера (тот же формат что POST /api/build)."""
    qpath = os.path.expanduser(queue_path)
    os.makedirs(os.path.dirname(qpath), exist_ok=True)
    try:
        items = json.loads(open(qpath, encoding="utf-8").read()) \
            if os.path.exists(qpath) else []
    except Exception:
        items = []
    tid = f"t{int(now if now is not None else time.time())}"
    items.append({"id": tid, "repo_path": repo, "task": task, "kind": "normal",
                  "status": "queued", "branch": "", "pr_url": "",
                  "created_at": "", "evidence": {"from": frm}})
    with open(qpath, "w", encoding="utf-8") as f:
        f.write(json.dumps(items, ensure_ascii=False, indent=1))
    return tid
