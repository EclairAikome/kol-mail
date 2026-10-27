"""A Date header later than the mail's arrival (sender clock or time zone wrong) is replaced by the receipt time."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import received  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 27, 7, 38, tzinfo=UTC)


def at(hh, mm, ss=0, day=27):
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=UTC)


class FakeServer:
    """list_emails_metadata the way mcp-email-server answers it: newest receipt first, since/before and from_address
    filtering on the receipt time, but only the Date header in the answer."""

    def __init__(self, mails):
        self.mails = mails  # (email_id, sender, Date header, receipt time)
        self.calls = 0

    async def call(self, tool, mailbox, page=1, page_size=10, since=None, before=None, from_address=None, **_):
        assert tool == "list_emails_metadata"
        self.calls += 1
        lo, hi = received.parse(since), received.parse(before)
        hits = [m for m in sorted(self.mails, key=lambda m: m[3], reverse=True)
                if (lo is None or m[3] >= lo) and (hi is None or m[3] < hi)
                and (from_address is None or from_address in m[1])]
        chunk = hits[(page - 1) * page_size:page * page_size]
        return {"emails": [{"email_id": i, "message_id": f"<{i}@x>", "sender": s, "date": received.stamp(d)}
                           for i, s, d, _ in chunk], "total": len(hits)}


GLOOBY = ("929", "Glooby <glooby@example.com>", at(11, 33, 32), at(4, 33, 40))  # header 7 hours late
INBOX = [("931", "Ana <ana@example.com>", at(7, 10), at(7, 10, 5)),
         ("930", "Bo <bo@example.com>", at(5, 0), at(5, 0, 3)),
         GLOOBY,
         ("928", "Glooby <glooby@example.com>", at(8, 10, day=26), at(8, 10, 4, day=26))]


def listing(mails):
    return asyncio.run(FakeServer(mails).call("list_emails_metadata", mailbox="INBOX", page_size=30))["emails"]


def test_only_a_header_later_than_the_mail_above_it_is_suspect():
    found = received.suspects(listing(INBOX), NOW, {})
    assert [(e["email_id"], bound) for e, bound in found] == [("929", at(5, 0))]


def test_a_newest_mail_dated_after_the_fetch_is_suspect():
    found = received.suspects(listing([GLOOBY]), NOW, {})
    assert [(e["email_id"], bound) for e, bound in found] == [("929", NOW)]


def test_ordinary_drift_is_left_alone():
    late = ("932", "Cy <cy@example.com>", at(7, 45), at(7, 37))  # 8 minutes fast
    assert received.suspects(listing([late] + INBOX[:2]), NOW, {}) == []


def test_mails_already_looked_up_are_not_suspects_again_and_tighten_the_bound():
    known = {"<929@x>": received.stamp(at(4, 33))}
    early = ("927", "Di <di@example.com>", at(4, 50), at(4, 20))  # arrived before 929, header after its arrival
    assert [e["email_id"] for e, _ in received.suspects(listing(INBOX + [early]), NOW, known)] == ["927"]


def test_a_header_too_early_makes_only_the_mail_below_it_suspect():
    slow = ("940", "Ed <ed@example.com>", at(5, 0, day=25), at(7, 20))  # clock two days slow
    honest = [(str(941 + i), f"F{i} <f{i}@example.com>", at(7, 15 - i), at(7, 15 - i)) for i in range(5)]
    found = received.suspects(listing([slow] + honest), NOW, {})
    assert [e["email_id"] for e, _ in found] == ["941"]


def test_find_narrows_the_receipt_time_down_to_a_minute():
    server = FakeServer(INBOX)
    when = asyncio.run(received.find(server.call, "INBOX", "929", GLOOBY[1], at(5, 0), NOW))
    assert GLOOBY[3] - received.STEP <= when <= GLOOBY[3]
    assert server.calls <= 14


def test_find_still_finds_the_mail_when_its_bound_is_too_early():
    when = asyncio.run(received.find(FakeServer(INBOX).call, "INBOX", "929", GLOOBY[1], at(3, 0, day=25), NOW))
    assert GLOOBY[3] - received.STEP <= when <= GLOOBY[3]


def test_find_gives_up_when_the_mail_is_gone():
    assert asyncio.run(received.find(FakeServer(INBOX[:2]).call, "INBOX", "929", GLOOBY[1], at(5, 0), NOW)) is None


def test_find_pages_through_a_busy_sender():
    many = [(str(2000 + i), GLOOBY[1], at(4, 0) - timedelta(minutes=i), at(4, 0) - timedelta(minutes=i))
            for i in range(250)]
    when = asyncio.run(received.find(FakeServer(many + [GLOOBY]).call, "INBOX", "929", GLOOBY[1], NOW, NOW))
    assert GLOOBY[3] - received.STEP <= when <= GLOOBY[3]


def test_shown_keeps_the_header_unless_it_is_later_than_the_arrival():
    assert received.shown("2026-10-27T11:33:32+00:00", "2026-10-27T04:33:00+00:00") == "2026-10-27T04:33:00+00:00"
    assert received.shown("2026-10-27T04:40:00+00:00", "2026-10-27T04:33:00+00:00") == "2026-10-27T04:40:00+00:00"
    assert received.shown("2026-10-27T04:20:00+00:00", "2026-10-27T04:33:00+00:00") == "2026-10-27T04:20:00+00:00"
    assert received.shown("2026-10-27T11:33:32+00:00", None) == "2026-10-27T11:33:32+00:00"


@pytest.fixture
def fresh_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "cache", app.Cache(tmp_path / "c.sqlite3"))
    monkeypatch.setattr(app, "received_tried", {})
    monkeypatch.setitem(app.state, "unread_rev", 0)


def stored(mid):
    return app.cache.received_many(app.current_account.get(), [mid])


def test_a_list_fetch_corrects_the_row_and_asks_the_page_to_repaint(fresh_cache, monkeypatch):
    server = FakeServer(INBOX)
    monkeypatch.setattr(app, "bg", SimpleNamespace(call=server.call))
    emails = listing(INBOX)
    asyncio.run(app.check_received("INBOX", emails, NOW.timestamp()))
    rows = {r["email_id"]: r["date"] for r in (app.list_row("INBOX", e, {}) for e in emails)}
    assert received.parse(rows["929"]) == received.parse(stored("<929@x>")["<929@x>"])
    assert GLOOBY[3] - received.STEP <= received.parse(rows["929"]) <= GLOOBY[3]
    assert rows["930"] == emails[1]["date"]
    assert app.state["unread_rev"] == 1
    calls = server.calls
    app.received_tried.clear()
    asyncio.run(app.check_received("INBOX", emails, NOW.timestamp()))
    assert server.calls == calls  # looked up once, then cached
    with app.on_account("someone_else"):
        assert app.list_row("INBOX", emails[2], {})["date"] == emails[2]["date"]  # another mailbox's arrival


def test_a_list_fetched_again_meanwhile_does_not_repeat_the_lookups(tmp_path, monkeypatch):
    monkeypatch.setitem(app.state, "unread_rev", 0)
    emails = listing(INBOX)
    counts = []
    for n, runs in enumerate((1, 2)):
        server = FakeServer(INBOX)
        monkeypatch.setattr(app, "bg", SimpleNamespace(call=server.call))
        monkeypatch.setattr(app, "cache", app.Cache(tmp_path / f"c{n}.sqlite3"))
        monkeypatch.setattr(app, "received_tried", {})

        async def fetches():
            await asyncio.gather(*(app.check_received("INBOX", emails, NOW.timestamp()) for _ in range(runs)))
        asyncio.run(fetches())
        counts.append(server.calls)
    assert counts[0] == counts[1]


def test_a_mail_error_is_retried_later_and_never_reported(fresh_cache, monkeypatch):
    async def broken(*a, **k):
        raise app.MailError("IMAP down")
    monkeypatch.setattr(app, "bg", SimpleNamespace(call=broken))
    reported = []
    monkeypatch.setattr(app.autoreport, "report", lambda *a, **k: reported.append(a))
    asyncio.run(app.check_received("INBOX", listing(INBOX), NOW.timestamp()))
    assert stored("<929@x>") == {} and reported == []
    assert (app.current_account.get(), "<929@x>") in app.received_tried
