"""Pull-модель reply-доставки klod-access: агент тянет свои ответы по to + курсору."""
import tempfile
from pathlib import Path

import klod_inbox


def _isolate(tmp):
    klod_inbox.INBOX_DIR = Path(tmp)
    klod_inbox.OUTBOX_FILE = Path(tmp) / "outbox.jsonl"
    klod_inbox.INBOX_FILE = Path(tmp) / "inbox.jsonl"
    klod_inbox.COUNTER_FILE = Path(tmp) / "counter.txt"


def test_outbox_to_filter_and_cursor():
    tmp = tempfile.mkdtemp()
    _isolate(tmp)
    r1 = klod_inbox.write_outbox("eshkola", "fix done", in_reply_to=26)
    klod_inbox.write_outbox("career-bot", "oauth standard", in_reply_to=23)
    klod_inbox.write_outbox("eshkola", "more", in_reply_to=26)

    assert len(klod_inbox.read_outbox(to="eshkola")) == 2
    assert len(klod_inbox.read_outbox(to="career-bot")) == 1
    assert len(klod_inbox.read_outbox()) == 3
    # курсор: только ответы eshkola ПОСЛЕ первого
    after = klod_inbox.read_outbox(since=r1["id"], to="eshkola")
    assert len(after) == 1 and after[0]["message"] == "more"
