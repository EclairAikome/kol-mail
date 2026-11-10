"""0.10: a conversation lists every one of its mails. Until then it showed its latest 20 and left the rest out without
a word. The earlier ones now come as stubs (who, when, a cached preview) for the page to fold away, and each is
fetched when it is opened, so a hundred mails cost no more logins to open than twenty did."""

import asyncio
import collections
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

KOL = "kai@example.org"
SUBJECT = "Sponsorship for the March streams"
SENT = "Sent"


def meta(n: int) -> tuple[str, dict]:
    """Mail n of the conversation (1 the oldest): they write the odd ones, we answer with the even ones."""
    ours = n % 2 == 0
    day, hour = 1 + (n - 1) // 20, (n - 1) % 20
    return (SENT if ours else "INBOX", {
        "email_id": str(1000 + n), "message_id": f"<m{n}@example.org>",
        "sender": "Me <me@example.com>" if ours else f"Kai <{KOL}>",
        "recipients": [KOL] if ours else ["me@example.com"],
        "subject": ("Re: " if n > 1 else "") + SUBJECT, "date": f"2026-10-{day:02d}T{hour:02d}:00:00+00:00"})


class Seen(list):
    """The bodies asked for, with what else a test sets and looks at: unread, marked (read), queued (to translate)."""


@pytest.fixture
def thread(monkeypatch):
    """A conversation of `count` mails behind fakes of the mail backend; fetched: which bodies were asked for."""
    def make(count: int):
        members = [meta(n) for n in range(1, count + 1)]
        by_key = {(box, m["email_id"]): m for box, m in members}
        fetched = Seen()

        def full(box, uid):
            return {**by_key[(box, uid)], "body": f"body of {uid}", "attachments": ["kit.pdf"] if uid == "1001" else []}

        async def load_body(bridge, box, uid):
            return full(box, str(uid))

        async def load_body_slot(box, uid):
            fetched.append((box, str(uid)))
            return full(box, str(uid))

        async def thread_bodies(wanted):
            for box, m in wanted:
                fetched.append((box, m["email_id"]))
            return {(box, m["email_id"]): full(box, m["email_id"]) for box, m in wanted}

        async def candidates(msg):
            return list(members)

        async def nothing(*a, **k):
            return None

        monkeypatch.setattr(app, "LOGGED_OUT", False)
        monkeypatch.setattr(app, "bodies", collections.OrderedDict())
        monkeypatch.setattr(app, "load_body", load_body)
        monkeypatch.setattr(app, "load_body_slot", load_body_slot)
        monkeypatch.setattr(app, "thread_bodies", thread_bodies)
        monkeypatch.setattr(app, "thread_candidates", candidates)
        monkeypatch.setattr(app, "folders_known", nothing)
        monkeypatch.setattr(app, "index_boxes", lambda: ("INBOX", SENT))
        monkeypatch.setattr(app, "fresh_indexes", lambda: {"INBOX": [], SENT: []})
        monkeypatch.setattr(app, "sent_box", lambda: SENT)
        unread: set[str] = set()
        fetched.unread, fetched.marked, fetched.queued = unread, [], []  # what the tests set and look at

        async def mark_read_many(box, ids):
            fetched.marked.extend(ids)

        async def mark_read_later(box, uid):
            fetched.marked.append(str(uid))

        def spawn(coro, what):
            if what != "mark read":
                coro.close()
                return
            try:  # the stand-ins above never wait: one step runs them to their end
                coro.send(None)
            except StopIteration:
                pass

        monkeypatch.setattr(app, "is_unread", lambda box, uid: str(uid) in unread)
        monkeypatch.setattr(app, "mark_read_many", mark_read_many)
        monkeypatch.setattr(app, "mark_read_later", mark_read_later)
        monkeypatch.setattr(app, "is_flagged", lambda box, uid: False)
        monkeypatch.setattr(app, "known_channels", lambda address, texts: [])
        monkeypatch.setattr(app, "enqueue", lambda mid, box, uid, **k: fetched.queued.append(str(uid)))
        monkeypatch.setattr(app, "spawn", spawn)
        monkeypatch.setattr(app.cache, "get", lambda mid: None)
        return fetched
    return make


def get(path: str):
    client = TestClient(app.app, base_url=f"http://127.0.0.1:{app.PORT}")
    return client.get(path, headers={"X-KOL-Mail": "1"})


def test_a_hundred_mails_are_all_listed_and_only_the_latest_are_fetched(thread):
    fetched = thread(101)
    d = get("/api/thread?mailbox=INBOX&id=1101").json()
    msgs = d["messages"]
    assert [m["email_id"] for m in msgs] == [str(1000 + n) for n in range(1, 102)]  # every mail, oldest first
    assert d["truncated"] is False and d["partial"] is False
    stubs = [m for m in msgs if m.get("stub")]
    assert len(stubs) == 101 - app.THREAD_EAGER and all(m["body"] == "" for m in stubs)
    assert [m["email_id"] for m in msgs if not m.get("stub")] == [str(1000 + n) for n in range(82, 102)]
    assert len(fetched) == app.THREAD_EAGER  # a hundred mails open with as many logins as twenty
    first = msgs[0]
    assert first["sender"] == f"Kai <{KOL}>" and first["date"].startswith("2026-10-01") and not first["own"]
    assert msgs[1]["own"] and msgs[1]["recipients"] == [KOL]  # enough to answer it and to tell whose it is
    assert msgs[-1]["opened"] and d["reply_target"]["email_id"] == "1101"
    assert d["kol_address"] == KOL


def test_an_early_mail_opened_from_a_search_comes_whole_with_the_latest(thread):
    fetched = thread(60)
    d = get("/api/thread?mailbox=INBOX&id=1003").json()
    whole = [m["email_id"] for m in d["messages"] if not m.get("stub")]
    assert whole == ["1003"] + [str(1000 + n) for n in range(41, 61)]
    assert len(d["messages"]) == 60 and len(fetched) == app.THREAD_EAGER + 1
    opened = next(m for m in d["messages"] if m["opened"])
    assert opened["email_id"] == "1003" and opened["body"] == "body of 1003"


def test_a_folded_mail_is_fetched_when_it_is_opened(thread):
    fetched = thread(40)
    d = get("/api/thread/message?mailbox=INBOX&id=1001").json()["message"]
    assert d["body"] == "body of 1001" and d["attachments"] == ["kit.pdf"] and not d.get("stub")
    assert d["opened"] is False and d["own"] is False and fetched == [("INBOX", "1001")]
    assert get("/api/thread/message?mailbox=INBOX&id=abc").status_code == 400
    assert get("/api/thread/message?id=1001").status_code == 400


def test_an_earlier_mail_comes_whole_only_when_that_costs_neither_a_login_nor_an_ai_call(thread, monkeypatch):
    fetched = thread(30)
    for n in (1, 2, 3):  # bodies the background prefetch happened to cache: theirs, ours, theirs
        box, m = meta(n)
        app.bodies[(box, m["email_id"])] = {**m, "body": f"cached words {n}"}
    monkeypatch.setattr(app.cache, "get", lambda mid: {"status": "done", "body_zh": "译文", "body_hash": None}
                        if mid == "<m3@example.org>" else None)
    d = get("/api/thread?mailbox=INBOX&id=1029").json()
    first, second, third = d["messages"][:3]
    assert first["stub"]                                            # theirs, never translated: an AI call if shown
    assert second["body"] == "cached words 2" and not second.get("stub")  # ours: nothing to translate
    assert third["body"] == "cached words 3" and not third.get("stub")    # theirs, translated already
    assert not {("INBOX", "1001"), ("Sent", "1002"), ("INBOX", "1003")} & set(fetched)
    assert "1001" not in fetched.queued and "1003" not in fetched.queued
    assert sorted(set(fetched.queued), key=int) == [str(1000 + n) for n in range(11, 31, 2)]  # the latest twenty's


def test_only_the_mails_that_came_whole_are_marked_read(thread):
    """Opening a conversation used to mark every unread mail of it read. With the earlier ones folded away unseen,
    that would have hidden mail the owner never looked at."""
    fetched = thread(60)
    fetched.unread.update({"1003", "1005", "1051", "1057", "1059"})
    d = get("/api/thread?mailbox=INBOX&id=1059").json()
    by_id = {m["email_id"]: m for m in d["messages"]}
    assert [i for i, m in by_id.items() if m["unread"]] == ["1003", "1005", "1051", "1057", "1059"]
    assert by_id["1003"]["stub"] and not by_id["1051"].get("stub")
    assert sorted(fetched.marked) == ["1051", "1057", "1059"]  # the opened one and the two that came whole
    assert not any(m["unread"] for m in d["messages"] if m["own"])
    fetched.marked.clear()
    get("/api/thread/message?mailbox=INBOX&id=1003")           # the folded one is read when it is opened
    assert fetched.marked == ["1003"]


def test_a_stub_previews_the_cached_chinese(thread, monkeypatch):
    thread(30)
    monkeypatch.setattr(app.cache, "get", lambda mid: {"status": "done", "body_zh": "你好，\n\n我的报价是 400 美元。",
                                                       "body_hash": "x"} if mid == "<m1@example.org>" else None)
    d = get("/api/thread?mailbox=INBOX&id=1029").json()
    assert d["messages"][0]["stub"] and d["messages"][0]["preview"] == "你好， 我的报价是 400 美元。"
    assert d["messages"][2]["stub"] and d["messages"][2]["preview"] == ""


def test_a_short_conversation_is_as_before(thread):
    fetched = thread(5)
    d = get("/api/thread?mailbox=INBOX&id=1005").json()
    assert len(d["messages"]) == 5 and not any(m.get("stub") for m in d["messages"]) and len(fetched) == 5


def test_a_conversation_beyond_the_limit_says_so(thread, monkeypatch):
    thread(30)
    monkeypatch.setattr(app, "THREAD_LIMIT", 12)
    d = get("/api/thread?mailbox=INBOX&id=1029").json()
    assert d["truncated"] is True and len(d["messages"]) == 12
    assert d["messages"][-1]["email_id"] == "1030" and d["messages"][0]["email_id"] == "1019"  # the latest are kept
    monkeypatch.setattr(app, "THREAD_LIMIT", 30)  # exactly as many as the conversation has: nothing was cut
    d = get("/api/thread?mailbox=INBOX&id=1029").json()
    assert d["truncated"] is False and len(d["messages"]) == 30


def test_warming_a_conversation_fetches_what_it_opens_with_not_its_history(thread, monkeypatch):
    fetched = thread(80)
    ran = []
    monkeypatch.setattr(app, "spawn", lambda coro, what: ran.append(asyncio.run(coro)))
    box, m = meta(79)
    app.warm_thread(box, m["email_id"], m)
    assert len(fetched) == app.THREAD_EAGER


def test_eager_members_keeps_order_and_never_doubles_the_opened_one():
    members = [meta(n) for n in range(1, 31)]
    eager = app.eager_members(members, "INBOX", "1029")
    assert [m["email_id"] for _, m in eager] == [str(1000 + n) for n in range(11, 31)]
    eager = app.eager_members(members, "INBOX", "1001")
    assert [m["email_id"] for _, m in eager] == ["1001"] + [str(1000 + n) for n in range(11, 31)]
