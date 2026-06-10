"""Тесты трекера бэклога Клода (#7) и промоушена в очередь Билдера."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from backlog import BacklogStore, enqueue_builder_ticket


def _store(tmp_path):
    return BacklogStore(path=str(tmp_path / "backlog.json"))


def test_add_and_list(tmp_path):
    s = _store(tmp_path)
    it = s.add("Починить дашборд", note="кнопка не жмётся", repo="infra/lineman", now=1000)
    assert it["id"] and it["status"] == "new" and it["title"] == "Починить дашборд"
    assert it["repo"] == "infra/lineman"
    assert len(s.list()) == 1


def test_add_empty_title_rejected(tmp_path):
    s = _store(tmp_path)
    try:
        s.add("   ")
        assert False, "should raise"
    except ValueError:
        pass


def test_set_status_and_ticket(tmp_path):
    s = _store(tmp_path)
    it = s.add("X", now=1000)
    upd = s.set_status(it["id"], "sent", ticket_id="t123")
    assert upd["status"] == "sent" and upd["ticket_id"] == "t123"
    assert s.get(it["id"])["status"] == "sent"


def test_remove(tmp_path):
    s = _store(tmp_path)
    it = s.add("X", now=1000)
    assert s.remove(it["id"]) is True
    assert s.list() == []
    assert s.remove("nope") is False


def test_summary(tmp_path):
    s = _store(tmp_path)
    a = s.add("a", now=1000); s.add("b", now=1001)
    s.set_status(a["id"], "done")
    assert s.summary() == {"new": 1, "done": 1}


def test_ids_unique(tmp_path):
    s = _store(tmp_path)
    ids = {s.add(f"t{i}")["id"] for i in range(5)}
    assert len(ids) == 5


def test_enqueue_builder_ticket(tmp_path):
    q = str(tmp_path / "queue.json")
    tid = enqueue_builder_ticket(q, repo="infra/lineman", task="fix X", frm="klod-backlog", now=2000)
    items = json.loads(open(q).read())
    assert items[0]["id"] == tid and items[0]["task"] == "fix X"
    assert items[0]["repo_path"] == "infra/lineman"
    assert items[0]["status"] == "queued"
    assert items[0]["evidence"]["from"] == "klod-backlog"
