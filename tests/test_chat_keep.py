"""What is kept of a private chat is kept against the other side changing its mind and against this computer
failing: a message read again with other words keeps the earlier ones (msg_edit) and 私信记录 says so in a new row,
a linked chat's whole history is in 私信记录 (msg.noted, apart from the sheet's msg.logged), attachments are copied
next to it for good (msg_file), and once a day the store and the transcript are copied with a ledger of SHA-256
sums (私信备份)."""

import asyncio
import hashlib
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import chats  # noqa: E402

CH = "1100000000000000001"
KEY = f"dc:{CH}"
TG = "tg:5550001234"
PEER = "1300000000000000003"
TZ = app.autoreply.TZ
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


def at(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def msg(mid, minutes_ago, out=False, text="hi", **extra):
    return {"id": str(mid), "at": at(minutes_ago), "out": out, "author": "Mira" if out else "PixelFox_",
            "text": text, "extra": extra}


@pytest.fixture
def record(tmp_path, monkeypatch):
    folder = tmp_path / "私信记录"
    monkeypatch.setattr(app, "CHAT_RECORD", folder)
    return folder


@pytest.fixture
def store(tmp_path, record, monkeypatch):
    s = chats.Store(tmp_path / "chats.sqlite3")
    monkeypatch.setattr(app, "CHATS", s)
    monkeypatch.setattr(app, "chat_locks", {})
    monkeypatch.setattr(app, "chat_file_locks", {})
    monkeypatch.setattr(app, "state", {**app.state, "ext_seen": None, "ext_feed": None, "chat_tg_error": None})
    monkeypatch.setattr(app, "chat_rows_cache", {"at": 9e18, "rows": [], "error": None})
    monkeypatch.setattr(app, "mark_from_internet", lambda path: True)  # as on Windows, whatever runs the test

    async def no_claude(*a, **k):  # 沟通记录 rows go in untranslated: the model is never asked from a test
        raise app.ClaudeError("offline")
    monkeypatch.setattr(app, "run_claude", no_claude)
    return s


@pytest.fixture
def posted(monkeypatch):
    rows = []
    monkeypatch.setattr(app, "sheet_post", rows.append)
    return rows


@pytest.fixture
def spawned(monkeypatch):
    """What the code under test hands to the background, run by the test when it wants it run."""
    waiting = []
    monkeypatch.setattr(app, "spawn", lambda coro, what: waiting.append((what, coro)))

    def run():
        done = [what for what, _ in waiting]
        while waiting:
            asyncio.run(waiting.pop(0)[1])
        return done
    yield run
    for _, coro in waiting:
        coro.close()


def rows_of(record: Path) -> list[str]:
    return [line for p in sorted(record.glob("*.md")) for line in p.read_text(encoding="utf-8").splitlines()
            if chats.row_cells(line)]


def said(record: Path) -> list[str]:
    return [chats.row_cells(r)[4] for r in rows_of(record)]


def linked(store, key=KEY, kol="K0103"):
    store.upsert_conv(key, peer_id=PEER if key == KEY else key.split(":")[1], peer_name="PixelFox_")
    store.link(key, kol, "PixelFox", "pixelfox@example.com")


# ---------------------------------------------------------------- A: an edit keeps the earlier words


def test_an_edit_keeps_the_old_words_and_the_transcript_says_so_in_a_new_row(store, record, posted):
    linked(store)
    store.add_messages(KEY, [msg(1, 20, text="Deal: 500 USD for one video")])
    asyncio.run(app.chat_record(KEY))
    first = rows_of(record)
    assert len(first) == 1 and first[0].endswith("| 收 | Deal: 500 USD for one video |")
    assert store.add_messages(KEY, [msg(1, 20, text="Deal: 800 USD for one video")]) == []  # not a new message
    kept = store.edits(KEY)
    assert [(e["id"], e["old_text"], e["new_text"], e["real"], e["noted"]) for e in kept] == [
        ("1", "Deal: 500 USD for one video", "Deal: 800 USD for one video", 1, 0)]
    assert chats.parse_time(kept[0]["found_at"]) and store.waiting(KEY)
    assert store.messages(KEY)[0]["text"] == "Deal: 800 USD for one video"
    asyncio.run(app.chat_record(KEY))
    rows = rows_of(record)
    assert rows[0] == first[0] and len(rows) == 2  # the row written when the message came is as it was
    assert "【已编辑】" in rows[1] and "消息ID 1【旧文本】Deal: 500 USD for one video【新文本】Deal: 800 USD for one video" in rows[1]
    assert "| Discord | K0103 | PixelFox | 收 |" in rows[1]
    assert not store.waiting(KEY) and store.edits(KEY)[0]["noted"] == 1
    asyncio.run(app.chat_record(KEY))
    assert len(rows_of(record)) == 2  # told once
    assert len(posted) == 1  # an edit is no new 沟通记录 row


def test_every_version_of_a_message_edited_twice_stays(store):
    store.upsert_conv(KEY)
    for text in ("one", "two", "three"):
        store.add_messages(KEY, [msg(7, 5, text=text)])
    assert [(e["old_text"], e["new_text"]) for e in store.edits(KEY, mid="7")] == [("one", "two"), ("two", "three")]
    assert store.edits(KEY, mid="8") == []


def test_the_same_words_drawn_another_way_are_kept_but_are_no_edit(store, record, posted):
    linked(store)
    store.add_messages(KEY, [msg(1, 20, text="rate is 500 per video")])
    asyncio.run(app.chat_record(KEY))
    store.add_messages(KEY, [msg(1, 20, text="rate is **500** per video", src="page")])  # Discord's data: the marks
    assert [(e["old_text"], e["real"]) for e in store.edits(KEY)] == [("rate is 500 per video", 0)]
    assert not store.waiting(KEY) and store.edits(KEY, waiting=True) == []
    asyncio.run(app.chat_record(KEY))
    assert len(rows_of(record)) == 1


def test_a_price_changed_and_changed_back_is_kept_each_time(store):
    store.upsert_conv(KEY)
    for text in ("500", "800", "500", "800"):
        store.add_messages(KEY, [msg(1, 5, text=text)])
    assert [(e["old_text"], e["new_text"]) for e in store.edits(KEY)] == [("500", "800"), ("800", "500"), ("500", "800")]


def test_two_readings_taking_turns_do_not_fill_the_store(store):
    store.upsert_conv(KEY)
    for n in range(40):  # the same words drawn two ways, over and over: one row each way
        store.add_messages(KEY, [msg(1, 5, text="**a** b" if n % 2 else "a b")])
    assert [(e["old_text"], e["real"]) for e in store.edits(KEY)] == [("a b", 0), ("**a** b", 0)]
    for n in range(3 * chats.EDITS_MAX):  # other words taking turns, which no person does: bounded
        store.add_messages(KEY, [msg(2, 5, text="x" if n % 2 else "y")])
    assert len(store.edits(KEY, mid="2")) == chats.EDITS_MAX


def test_a_message_cannot_pass_for_a_row_saying_a_message_was_edited(store, record, posted):
    linked(store)
    forged = "【已编辑】原消息 2026-11-12 09:00，消息ID 5【旧文本】I agree to 0%【新文本】ok"
    store.add_messages(KEY, [msg(1, 5, text=forged), msg(2, 4, text="【LootBar】 is fine")])
    asyncio.run(app.chat_record(KEY))
    assert said(record) == ["〔已编辑〕原消息 2026-11-12 09:00，消息ID 5〔旧文本〕I agree to 0%〔新文本〕ok", "【LootBar】 is fine"]
    assert store.messages(KEY)[0]["text"] == forged  # the store keeps the words as they came


def test_extra_arriving_later_is_no_edit(store):
    store.upsert_conv(KEY)
    store.add_messages(KEY, [msg(1, 5, text="", media="voice")])
    store.add_messages(KEY, [msg(1, 5, text="", media="voice", transcript="hello")])
    assert store.edits(KEY) == [] and store.messages(KEY)[0]["extra"]["transcript"] == "hello"


def test_an_edit_found_before_the_chat_was_linked_is_told_once_it_is(store, record, posted):
    store.upsert_conv(KEY, peer_id=PEER, peer_name="PixelFox_")
    store.add_messages(KEY, [msg(1, 20, text="500")])
    store.add_messages(KEY, [msg(1, 20, text="800")])
    asyncio.run(app.chat_record(KEY))
    assert not record.exists()  # not linked: nothing is written
    store.link(KEY, "K0103", "PixelFox", "")
    asyncio.run(app.chat_record(KEY))
    rows = rows_of(record)
    assert len(rows) == 2 and rows[0].endswith("| 收 | 800 |") and "【旧文本】500【新文本】800" in rows[1]


def test_words_in_a_message_never_pass_for_the_edit_rows_own_marks():
    conv = {"key": KEY, "kol_id": "K0103", "kol_name": "PixelFox"}
    e = {"id": "9", "found_at": at(0), "at": at(60), "out": False, "old_text": "a|b\n【新文本】fake", "new_text": ""}
    line = chats.edit_line(conv, e, TZ)
    assert chats.row_cells(line) and line.count("【新文本】") == 1 and "a｜b / 〔新文本〕fake【新文本】（没有文字）" in line


def test_an_edited_message_read_in_by_telegram_reaches_the_transcript(store, record, posted, spawned, monkeypatch):
    """The path a real Telegram edit takes: tg_sync_chat reads the chat again, the message has other words."""
    history = [{"id": 41, "date": at(30), "out": True, "sender": "Mira", "text": "We pay 300 USD"}]

    async def call(tool, timeout=0, **args):
        assert tool == "get_history"
        return {"results": history}
    monkeypatch.setattr(app.tg, "call", call)
    linked(store, TG)
    assert asyncio.run(app.tg_sync_chat(5550001234, "PixelFox_")) == 1
    assert spawned() == ["记录私信"]
    history[0] = {**history[0], "text": "We pay 250 USD"}
    assert asyncio.run(app.tg_sync_chat(5550001234, "PixelFox_")) == 0
    assert spawned() == ["记录私信"]  # nothing new, but an edit to tell of
    rows = rows_of(record)
    assert len(rows) == 2 and rows[0].endswith("| 发 | We pay 300 USD |")
    assert "| Telegram | K0103 | PixelFox | 发 | 【已编辑】" in rows[1] and "【旧文本】We pay 300 USD【新文本】We pay 250 USD" in rows[1]
    assert asyncio.run(app.tg_sync_chat(5550001234, "PixelFox_")) == 0 and spawned() == []


def test_a_store_from_before_gets_the_new_column_and_tables(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(str(path))
    db.executescript("""
        CREATE TABLE conv (key TEXT PRIMARY KEY, channel TEXT NOT NULL, peer_id TEXT, peer_name TEXT, username TEXT,
          kol_id TEXT, kol_name TEXT, kol_email TEXT, linked_at TEXT, ignored INTEGER NOT NULL DEFAULT 0, seen_at TEXT,
          last_at TEXT, last_in_at TEXT, last_out_at TEXT, created_at TEXT NOT NULL, no_autolink INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE msg (conv TEXT NOT NULL, id TEXT NOT NULL, at TEXT NOT NULL, out INTEGER NOT NULL, author TEXT,
          text TEXT, extra TEXT, zh TEXT, logged INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (conv, id));
        INSERT INTO conv (key, channel, created_at) VALUES ('dc:1100000000000000001', 'discord', '2026-11-01T00:00:00+00:00');
        INSERT INTO msg (conv, id, at, out, author, text, extra, logged)
          VALUES ('dc:1100000000000000001', '1', '2026-11-01T00:00:00+00:00', 0, 'x', 'old words', '{}', 1);
    """)
    db.commit()
    db.close()
    s = chats.Store(path)
    try:
        m = s.messages(KEY)[0]
        assert (m["text"], m["logged"], m["noted"]) == ("old words", 1, 0)
        s.add_messages(KEY, [{"id": "1", "at": "2026-11-01T00:00:00+00:00", "out": False, "text": "new words"}])
        assert s.edits(KEY)[0]["old_text"] == "old words" and s.files(KEY) == []
    finally:
        s.db.close()
    chats.Store(path).db.close()  # opening it again changes nothing


# ---------------------------------------------------------------- B: the transcript holds a linked chat's history


def test_reconcile_marks_what_the_files_hold_so_only_the_rest_is_written(store, record, posted):
    linked(store)
    old = [msg(1, 60 * 24 * 9, text="first|contact"), msg(2, 60 * 24 * 8, out=True, text="our offer"),
           msg(3, 60 * 24 * 8 - 1, text="ok")]
    store.add_messages(KEY, old + [msg(4, 60 * 24 * 7, text="never written: before the link's window")])
    conv = store.conv(KEY)
    chats.append_transcript(record, conv, store.messages(KEY)[:3], TZ)  # as the version before wrote them
    store.mark_logged(KEY, ["1", "2", "3", "4"])  # ... and marked them; the fourth by skip_old, without a row
    before = rows_of(record)
    assert store.reconcile(record, TZ) == (3, 1)
    assert [m["noted"] for m in store.messages(KEY)] == [1, 1, 1, 0]
    assert store.reconcile(record, TZ) is None  # once
    asyncio.run(app.chat_record(KEY))
    rows = rows_of(record)
    assert len(rows) == 4 and sorted(rows) == sorted(before + [r for r in rows if "never written" in r])
    assert posted == []  # 沟通记录 is not touched: all four are older than the backfill window


def test_reconcile_finds_a_row_written_under_another_kol_id_and_counts_equal_rows(store, record):
    linked(store)
    store.add_messages(KEY, [msg(1, 600, text="ok"), msg(2, 600, text="ok"), msg(3, 600, text="ok")])
    then = {**store.conv(KEY), "kol_id": "K0001", "kol_name": "someone else"}
    chats.append_transcript(record, then, store.messages(KEY)[:2], TZ)
    store.mark_logged(KEY, ["1", "2", "3"])
    # a chat never linked said "ok" in that minute too: never logged, so no row can be its own
    store.upsert_conv(TG, peer_id="5550001234")
    store.add_messages(TG, [{**msg(1, 600, text="ok"), "at": store.messages(KEY)[0]["at"]}])
    assert store.reconcile(record, TZ) == (2, 2)  # two rows say "ok" at that minute: two messages are in
    assert [m["noted"] for m in store.messages(KEY)] == [1, 1, 0] and store.messages(TG)[0]["noted"] == 0


def test_reconcile_waits_while_the_files_cannot_be_read_and_the_sheet_is_written_meanwhile(store, record, posted,
                                                                                         tmp_path, monkeypatch):
    away = tmp_path / "drive that is away" / "私信记录"
    monkeypatch.setattr(app, "CHAT_RECORD", away)
    linked(store)
    store.add_messages(KEY, [msg(1, 5, text="written before")])
    asyncio.run(app.chat_record(KEY))
    assert [p["summary"] for p in posted] == ["对方消息：written before"]  # 沟通记录 is not held up
    assert store.get_kv(chats.RECONCILED) is None and store.messages(KEY)[0]["noted"] == 0 and not away.exists()
    assert "私信记录读不到" in app.state["sheet_error"]
    record.mkdir()
    (record / "2026-11-01.md").write_bytes(b"\xff\xfe not text")
    with pytest.raises(OSError):
        store.reconcile(record, TZ)
    assert store.get_kv(chats.RECONCILED) is None
    (record / "2026-11-01.md").unlink()
    monkeypatch.setattr(app, "CHAT_RECORD", record)
    asyncio.run(app.chat_record(KEY))
    assert said(record) == ["written before"] and len(posted) == 1 and store.get_kv(chats.RECONCILED) == "1"


def test_a_day_that_cannot_be_written_never_has_the_other_days_written_twice(store, record, posted, monkeypatch):
    linked(store)
    store.add_messages(KEY, [msg(1, 60 * 24 * 3, text="three days ago"), msg(2, 1, text="today")])
    real, today = chats.append_transcript, chats.local_day(at(1), TZ)

    def locked_today(folder, conv, messages, tz, **k):
        if any(chats.local_day(m["at"], tz) == today for m in messages):
            raise OSError("locked by a sync client")
        return real(folder, conv, messages, tz, **k)
    monkeypatch.setattr(chats, "append_transcript", locked_today)
    asyncio.run(app.chat_record(KEY))
    assert said(record) == ["three days ago"] and [m["noted"] for m in store.messages(KEY)] == [1, 0]
    monkeypatch.setattr(chats, "append_transcript", real)
    asyncio.run(app.chat_record(KEY))
    assert said(record) == ["three days ago", "today"]


def test_chat_record_reconciles_first_on_a_store_that_never_was(store, record, posted):
    linked(store)
    store.add_messages(KEY, [msg(1, 30, text="already written")])
    chats.append_transcript(record, store.conv(KEY), store.messages(KEY), TZ)
    store.mark_logged(KEY, ["1"])  # what the version before left: logged, in the file, noted unknown
    store.add_messages(KEY, [msg(2, 1, text="new")])
    asyncio.run(app.chat_record(KEY))
    assert said(record) == ["already written", "new"]
    assert store.get_kv(chats.RECONCILED) == "1"


def test_a_chat_linked_by_its_account_has_its_history_in_the_transcript_and_the_last_hour_in_the_sheet(
        store, record, posted, spawned, monkeypatch):
    monkeypatch.setattr(app, "chat_rows_cache", {"at": 9e18, "error": None, "rows": [
        {"id": "K0103", "kol": "PixelFox", "email": "", "chat": f"dc:{PEER}(PixelFox_)"}]})
    store.upsert_conv(KEY, peer_id=PEER, peer_name="PixelFox_")
    store.add_messages(KEY, [msg(1, 60 * 24 * 40, text="agreed: 1200 USD"), msg(2, 180, out=True, text="contract sent"),
                             msg(3, 5, text="signed")])
    app.chat_autolink(KEY)
    asyncio.run(app.chat_record(KEY))
    assert [p["summary"] for p in posted] == ["对方消息：signed"]
    rows = rows_of(record)
    assert len(rows) == 3 and rows[0].endswith("| 收 | agreed: 1200 USD |") and len(list(record.glob("*.md"))) >= 2
    posted.clear()
    asyncio.run(app.chat_record(KEY))
    assert posted == [] and len(rows_of(record)) == 3


def test_a_message_sent_to_the_sheet_again_is_not_written_to_the_transcript_again(store, record, posted):
    linked(store)
    store.add_messages(KEY, [msg(1, 50, text="one"), msg(2, 40, text="two")])
    asyncio.run(app.chat_record(KEY))
    store.unskip_after(KEY, datetime.now(timezone.utc) - timedelta(minutes=45))  # chat_catch_up: the sheet lacks "two"
    posted.clear()
    asyncio.run(app.chat_record(KEY))
    assert [p["summary"] for p in posted] == ["对方消息：two"] and len(rows_of(record)) == 2


def test_a_transcript_that_cannot_be_written_is_written_the_next_time(store, record, posted, monkeypatch):
    linked(store)
    store.add_messages(KEY, [msg(1, 5, text="one")])
    real = chats.append_transcript

    def full_disk(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(chats, "append_transcript", full_disk)
    asyncio.run(app.chat_record(KEY))
    assert len(posted) == 1 and store.waiting(KEY) and "私信记录写不进去" in app.state["sheet_error"]
    monkeypatch.setattr(chats, "append_transcript", real)
    asyncio.run(app.chat_record(KEY))
    assert len(posted) == 1 and len(rows_of(record)) == 1 and not store.waiting(KEY)


def test_the_backfill_after_a_start_writes_the_chats_nobody_wrote_in(store, record, posted, monkeypatch):
    monkeypatch.setattr(app.tg, "ready", asyncio.Event())
    app.tg.ready.set()
    linked(store)
    store.upsert_conv(TG, peer_id="5550001234", peer_name="stranger")  # not linked: stays out
    store.add_messages(KEY, [msg(1, 60 * 24 * 20, text="old talk")])
    store.add_messages(TG, [msg(1, 60 * 24 * 20, text="private")])
    asyncio.run(app.chat_backfill())
    assert said(record) == ["old talk"] and posted == []


# ---------------------------------------------------------------- D: attachments are copied for good


def attachment(name="rates.pdf", url=None):
    return {"name": name, "url": url or f"https://cdn.discordapp.com/attachments/{CH}/1/{name}?ex=1&is=2&hm=3"}


def test_a_discord_attachment_is_copied_next_to_the_transcript_and_the_row_names_it(store, record, posted, monkeypatch):
    asked = []

    async def fetch(url):
        asked.append(url)
        return b"%PDF-1.7 rates"
    monkeypatch.setattr(app, "discord_file", fetch)
    linked(store)
    store.add_messages(KEY, [msg(1400000000000000001, 5, text="here are my rates", attachments=[attachment()])])
    asyncio.run(app.chat_record(KEY))
    saved = record / "附件" / f"dc-{CH}" / "1400000000000000001" / "rates.pdf"
    assert saved.read_bytes() == b"%PDF-1.7 rates" and len(asked) == 1
    assert app.ATTACH_DIR not in saved.parents  # not where the weekly sweep deletes
    f = store.files(KEY, "1400000000000000001")[0]
    assert (f["name"], f["path"], f["size"], f["error"]) == ("rates.pdf", str(saved), 14, None)
    assert f["sha256"] == hashlib.sha256(b"%PDF-1.7 rates").hexdigest()
    assert rows_of(record)[0].endswith(
        f"| 收 | here are my rates [附件 rates.pdf，已存 附件/dc-{CH}/1400000000000000001/rates.pdf] |")
    asyncio.run(app.chat_keep_files(store.conv(KEY), store.messages(KEY)))
    assert len(asked) == 1  # copied once


def test_only_discords_own_file_servers_are_asked(monkeypatch):
    async def never(*a, **k):
        raise AssertionError("no request may be made")
    monkeypatch.setattr(app.materials, "get", never)
    for url in ("https://evil.example/attachments/1/2/a.png", "http://cdn.discordapp.com/attachments/1/2/a.png",
                "https://cdn.discordapp.com.evil.example/a.png",
                "https://x" "@cdn.discordapp.com/a.png",  # in two parts: whole, the publish scan reads an address

                "https://cdn.discordapp.com:8443/a.png", "https://cdn.discordapp.com:x/a.png", "file:///c:/x", "",
                "https://127.0.0.1/a.png"):
        with pytest.raises(app.materials.Unreadable):
            asyncio.run(app.discord_file(url))


def test_a_copy_that_fails_is_said_so_and_tried_again_with_the_next_reading(store, record, posted, spawned, monkeypatch):
    answers = [app.materials.Unreadable("Discord 的附件链接已过期"), b"\x89PNG data"]

    async def fetch(url):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(app, "discord_file", fetch)
    linked(store)
    m = msg(1400000000000000002, 5, text="", attachments=[attachment("proof.png")])
    store.add_messages(KEY, [m])
    asyncio.run(app.chat_record(KEY))
    assert rows_of(record)[0].endswith("| 收 | [附件 proof.png，未存] |")
    f = store.files(KEY, m["id"])[0]
    assert f["path"] is None and "过期" in f["error"] and f["tries"] == 1
    app.chat_after_store(KEY, [m], False)  # the DM is opened again in Discord: the same message, a fresh address
    assert spawned() == ["存私信附件"]
    f = store.files(KEY, m["id"])[0]
    assert Path(f["path"]).read_bytes() == b"\x89PNG data" and f["error"] is None and f["tries"] == 2
    app.chat_after_store(KEY, [m], False)
    assert spawned() == []  # nothing left to do
    rows = said(record)  # the first row stays; a new one says where the copy is now
    assert len(rows) == 2 and rows[0] == "[附件 proof.png，未存]"
    assert rows[1].startswith("【附件补存】原消息 ") and rows[1].endswith(
        f"，消息ID 1400000000000000002，已存 附件/dc-{CH}/1400000000000000002/proof.png")


def test_a_copy_is_given_up_after_a_few_tries_and_a_failure_never_hides_a_copy(store, record, monkeypatch):
    calls = []

    async def fetch(url):
        calls.append(url)
        raise app.materials.Unreadable("Discord 返回 500")
    monkeypatch.setattr(app, "discord_file", fetch)
    linked(store)
    m = msg(5, 5, attachments=[attachment("a.png")])
    store.add_messages(KEY, [m])
    for _ in range(app.CHAT_FILE_TRIES + 2):
        asyncio.run(app.chat_keep_files(store.conv(KEY), [m]))
    assert len(calls) == app.CHAT_FILE_TRIES and app.chat_files_todo(KEY, [m]) == []
    again = msg(5, 5, attachments=[attachment("a.png", f"https://cdn.discordapp.com/attachments/{CH}/1/a.png?ex=9&hm=8")])
    assert len(app.chat_files_todo(KEY, [again])) == 1  # Discord signed the address anew: a new chance
    asyncio.run(app.chat_keep_files(store.conv(KEY), [again]))
    assert len(calls) == app.CHAT_FILE_TRIES + 1 and app.chat_files_todo(KEY, [again]) == []
    store.file_saved(KEY, "6", 0, "b.png", "x/b.png", "00", 1)
    store.file_failed(KEY, "6", 0, "b.png", "later failure")
    assert store.files(KEY, "6")[0]["path"] == "x/b.png" and store.files(KEY, "6")[0]["error"] is None


def test_an_unlinked_chats_attachments_are_not_fetched(store, spawned, monkeypatch):
    store.upsert_conv(KEY, peer_id=PEER)
    m = msg(5, 5, attachments=[attachment("a.png")])
    store.add_messages(KEY, [m])
    app.chat_after_store(KEY, [m], True)
    assert spawned() == []


def test_names_from_a_stranger_are_made_safe_to_write():
    taken = set()
    name = app.chat_file_name
    assert name("rates.pdf", 0, taken) == "rates.pdf"
    assert name("rates.PDF", 1, taken) == "2-rates.PDF"  # the same name twice in one message (Windows: case aside)
    assert name("..\\..\\evil.exe", 0, set()) == "_.._evil.exe.unchecked"  # no path; nothing a double click runs
    assert name("a/b:c*?.png", 0, set()) == "a_b_c__.png"
    assert name("CON.txt", 0, set()) == "file-1.txt" and name("", 2, set()) == "file-3.unchecked"
    assert name("invoice.pdf\u202egnp.exe", 0, set()).endswith(".unchecked")  # a name that reads as .png is an .exe
    assert len(name("x" * 300 + ".png", 0, set())) <= 110
    assert app.chat_files_dir("tg:-100123", "../7").parts[-2:] == ("tg-100123", "___7")
    taken = set()
    # the number put in front never makes the name of another file of the message
    assert [name(x, n, taken) for n, x in enumerate(["3-a.pdf", "a.pdf", "a.pdf"])] == ["3-a.pdf", "a.pdf", "4-a.pdf"]


def test_a_copy_is_never_written_over_and_what_cannot_be_marked_opens_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "mark_from_internet", lambda path: True)
    first = app.chat_file_write(tmp_path, "rates.pdf", b"one")
    second = app.chat_file_write(tmp_path, "RATES.pdf", b"two")  # the same name to Windows
    assert (first.name, second.name) == ("rates.pdf", "2-RATES.pdf") and first.read_bytes() == b"one"
    monkeypatch.setattr(app, "mark_from_internet", lambda path: False)  # a Mac, or a disk without such marks
    assert app.chat_file_write(tmp_path, "contract.docx", b"x").name == "contract.docx.unchecked"
    assert app.chat_file_write(tmp_path, "shot.png", PNG).name == "shot.png"  # a picture runs nothing
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2-RATES.pdf", "contract.docx.unchecked", "rates.pdf", "shot.png"]

    def full(self, data):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "write_bytes", full)
    with pytest.raises(OSError):
        app.chat_file_write(tmp_path, "late.png", PNG)
    assert not any(p.name.startswith("part-") or p.name == "late.png" for p in tmp_path.iterdir())


def test_one_file_that_cannot_be_had_never_keeps_the_next_from_being_copied(store, record, monkeypatch):
    async def fetch(url):
        if "bad" in url:
            raise RuntimeError("something no one thought of")
        return PNG
    monkeypatch.setattr(app, "discord_file", fetch)
    linked(store)
    m = msg(5, 5, attachments=[attachment("bad.png"), attachment("good.png")])
    store.add_messages(KEY, [m])
    asyncio.run(app.chat_keep_files(store.conv(KEY), [m]))
    files = store.files(KEY, "5")
    assert [(f["name"], bool(f["path"]), f["tries"]) for f in files] == [("bad.png", False, 1), ("good.png", True, 1)]
    assert "something no one thought of" in files[0]["error"]


def test_a_telegram_photo_is_copied_and_other_media_is_only_named(store, record, posted, monkeypatch):
    asked = []

    async def photo(chat_id, message_id, kind):
        asked.append((chat_id, message_id, kind))
        return PNG
    monkeypatch.setattr(app, "telegram_file", photo)
    linked(store, TG)
    store.add_messages(TG, [msg(11, 9, text="paid, see screenshot", media="photo"), msg(12, 8, text="", media="video"),
                            msg(13, 7, text="contract", media="document: contract.pdf"),
                            msg(14, 6, out=True, text="", media="document: banner.PNG")])
    asyncio.run(app.chat_record(TG))
    assert asked == [(5550001234, 11, "photo"), (5550001234, 14, "document")]
    assert (record / "附件" / "tg-5550001234" / "11" / "photo.png").read_bytes() == PNG  # its real type, sniffed
    cells = said(record)
    assert cells == ["paid, see screenshot [附件 photo，已存 附件/tg-5550001234/11/photo.png]", "[video]",
                     "contract [document: contract.pdf]",
                     "[附件 document: banner.PNG，已存 附件/tg-5550001234/14/banner.PNG]"]


def test_what_telegram_hands_over_must_be_an_image_and_no_login_is_no_try(store, record, monkeypatch):
    answers = [b"<html>not a picture", app.NotReady("Telegram 后端没连上")]

    async def photo(chat_id, message_id, kind):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(app, "telegram_file", photo)
    linked(store, TG)
    ms = [msg(11, 9, media="photo"), msg(12, 8, media="photo")]
    store.add_messages(TG, ms)
    asyncio.run(app.chat_keep_files(store.conv(TG), ms))
    files = store.files(TG)
    assert [(f["id"], f["path"], f["tries"]) for f in files] == [("11", None, 1)] and "不是图片" in files[0]["error"]
    assert not (record / "附件").exists()


def test_a_photo_missed_while_telegram_was_away_is_copied_at_the_next_look(store, record, posted, monkeypatch):
    up = []

    async def photo(chat_id, message_id, kind):
        if not up:
            raise app.NotReady("Telegram 后端没连上")
        return PNG
    monkeypatch.setattr(app, "telegram_file", photo)
    linked(store, TG)
    store.add_messages(TG, [msg(11, 9, out=True, text="payment sent", media="photo")])
    asyncio.run(app.chat_record(TG))
    assert said(record) == ["payment sent [附件 photo，未存]"] and store.files(TG) == []
    asyncio.run(app.chat_backfill())  # still away: nothing changes, and no try is used up
    assert store.files(TG) == [] and len(rows_of(record)) == 1
    up.append(True)
    asyncio.run(app.chat_backfill())
    assert (record / "附件" / "tg-5550001234" / "11" / "photo.png").read_bytes() == PNG
    rows = said(record)
    assert len(rows) == 2 and rows[1].endswith("，消息ID 11，已存 附件/tg-5550001234/11/photo.png") and "| 发 |" in rows_of(record)[1]
    asyncio.run(app.chat_backfill())
    assert len(rows_of(record)) == 2


# ---------------------------------------------------------------- E: the daily copy and its ledger


def ledger_lines(record: Path) -> list[list[str]]:
    text = (record.parent / "私信备份" / app.CHAT_LEDGER).read_text(encoding="utf-8")
    return [line.split("\t") for line in text.splitlines() if not line.startswith("#")]


def test_the_daily_copy_holds_the_store_and_the_transcript_and_the_ledger_their_sums(store, record, posted):
    linked(store)
    store.add_messages(KEY, [msg(1, 5, text="agreed: 500 USD")])
    store.add_messages(KEY, [msg(1, 5, text="agreed: 300 USD")])
    asyncio.run(app.chat_record(KEY))
    day1 = datetime(2026, 11, 12, 18, 5, tzinfo=TZ)
    done = app.chat_backup(day1)
    folder = record.parent / "私信备份" / "2026-11-12"
    assert Path(done["folder"]) == folder and done["changed"] == []
    copy = sqlite3.connect(str(folder / "chats.sqlite3"))
    try:
        assert copy.execute("SELECT text FROM msg").fetchall() == [("agreed: 300 USD",)]
        assert copy.execute("SELECT old_text FROM msg_edit").fetchall() == [("agreed: 500 USD",)]
    finally:
        copy.close()
    md = sorted(record.glob("*.md"))[0]
    with zipfile.ZipFile(folder / "私信记录.zip") as z:
        assert z.namelist() == [md.name] and z.read(md.name) == md.read_bytes()
    lines = ledger_lines(record)
    assert [(c[1], c[4]) for c in lines] == [
        (f"私信记录/{md.name}", "新文件"), ("私信备份/2026-11-12/chats.sqlite3", "当天备份"),
        ("私信备份/2026-11-12/私信记录.zip", "当天备份"), ("=", "汇总值")]
    assert lines[0][2:4] == [str(md.stat().st_size), hashlib.sha256(md.read_bytes()).hexdigest()]
    assert lines[0][0] == "2026-11-12T18:05:00+08:00"
    first_digest = lines[-1][3]
    assert first_digest == app.ledger_digest("", ["\t".join(c) for c in lines[:-1]]) == done["digest"]

    size_then, sum_then = int(lines[0][2]), lines[0][3]
    store.add_messages(KEY, [msg(2, 1, out=True, text="ok")])
    asyncio.run(app.chat_record(KEY))
    app.chat_backup(day1 + timedelta(days=1))
    lines = ledger_lines(record)[4:]
    assert [(c[1], c[4]) for c in lines][0] == (f"私信记录/{md.name}", "末尾有追加")
    assert app.sha256_file(md, size_then) == sum_then  # the first sum still proves the file's beginning
    assert lines[-1][3] == app.ledger_digest(first_digest, ["\t".join(c) for c in lines[:-1]])  # chained to the run before

    app.chat_backup(day1 + timedelta(days=2))  # nothing changed: only the day's copies are listed
    assert [c[1] for c in ledger_lines(record)[8:]] == [
        "私信备份/2026-11-14/chats.sqlite3", "私信备份/2026-11-14/私信记录.zip", "="]

    md.write_text(md.read_text(encoding="utf-8").replace("300 USD", "900 USD"), encoding="utf-8", newline="\n")
    done = app.chat_backup(day1 + timedelta(days=3))
    assert done["changed"] == [f"私信记录/{md.name}"]
    assert ledger_lines(record)[11][4] == "已记过的内容有变化"


def test_a_second_copy_the_same_day_leaves_the_first_and_a_cut_off_ledger_line_is_closed(store, record):
    morning = datetime(2026, 11, 12, 9, 30, tzinfo=TZ)
    app.chat_backup(morning)
    root = record.parent / "私信备份"
    first = (root / "2026-11-12" / "chats.sqlite3").read_bytes()
    ledger = root / app.CHAT_LEDGER
    with ledger.open("a", encoding="utf-8", newline="\n") as f:
        f.write("2026-11-12T12:00:00+08:00\t私信记录/cut")  # the computer went off in the middle of a line
    store.upsert_conv(KEY)
    store.add_messages(KEY, [msg(1, 5)])
    app.chat_backup(morning.replace(hour=18, minute=0, second=7))
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["2026-11-12", "2026-11-12-180007"]
    assert (root / "2026-11-12" / "chats.sqlite3").read_bytes() == first  # what the ledger's sum is of
    lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if not ln.startswith("#")]
    assert lines[3] == "2026-11-12T12:00:00+08:00\t私信记录/cut"
    assert [ln.split("\t")[1] for ln in lines[4:]] == [
        "私信备份/2026-11-12-180007/chats.sqlite3", "私信备份/2026-11-12-180007/私信记录.zip", "="]
    assert lines[2].split("\t")[1] == "=" and lines[-1].split("\t")[3] == app.ledger_digest(lines[2].split("\t")[3], lines[4:6])


def test_a_copy_is_due_each_evening_and_a_missed_one_at_the_next_start():
    def t(day, hour):
        return datetime(2026, 11, day, hour, 0, tzinfo=TZ)
    due = app.chat_backup_due
    assert due(t(12, 9), None)
    assert not due(t(12, 9), t(11, 18)) and not due(t(12, 17), t(11, 23))  # last evening's is there: wait for tonight
    assert due(t(12, 18), t(11, 18)) and not due(t(12, 19), t(12, 18)) and not due(t(13, 9), t(12, 18))
    assert due(t(13, 9), t(11, 18))  # the computer was off on the 12th's evening
    assert due(t(12, 18), t(12, 10))  # a copy made in the morning (after a missed evening) is not tonight's
    assert not due(t(12, 19), t(12, 18).astimezone(timezone.utc))  # the time is stored with a zone: any zone compares


def test_old_copies_go_but_the_last_thirty_and_each_months_first_stay(store, record):
    root = record.parent / "私信备份"
    for day in range(1, 31):
        (root / f"2026-09-{day:02d}").mkdir(parents=True)
    for day in range(3, 32):
        (root / f"2026-10-{day:02d}").mkdir(parents=True)
    (root / "2026-10-05" / "chats.sqlite3").write_bytes(b"x")
    (root / "notes").mkdir()  # not ours: never touched
    app.chat_backup(datetime(2026, 11, 1, 18, 0, tzinfo=TZ))
    left = sorted(p.name for p in root.iterdir() if p.is_dir())
    # the last thirty (October's 29 and today's), and September's first
    assert left == ["2026-09-01"] + [f"2026-10-{d:02d}" for d in range(3, 32)] + ["2026-11-01", "notes"]
    assert (root / "2026-10-05" / "chats.sqlite3").exists()


def test_a_ledger_that_cannot_be_read_stops_the_copy_but_not_the_clearing(store, record):
    """Review of 1.0.1: the ledger was read after the copy, so a damaged one left a full copy at every look (48 a
    day) and the old copies were never cleared. Now nothing is copied, and the clearing runs all the same."""
    root = record.parent / "私信备份"
    for day in range(1, 31):
        (root / f"2026-09-{day:02d}").mkdir(parents=True)
    (root / app.CHAT_LEDGER).write_bytes(b"\xff\xfe broken")
    with pytest.raises(ValueError):
        app.chat_backup(datetime(2026, 11, 1, 18, 0, tzinfo=TZ))
    left = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert left == [f"2026-09-{d:02d}" for d in range(1, 31)]  # thirty: none too many, and no copy of today


def test_a_copy_the_ledger_never_got_is_made_again_in_its_place(store, record, monkeypatch):
    """The ledger could not be written after the copy (locked, disk full): the next look makes that copy again in
    the same folder instead of adding one more, and only a copy the ledger lists makes the next one a new folder."""
    root = record.parent / "私信备份"
    real, fail = app.ledger_digest, [True]

    def digest(before, lines):
        if fail:
            raise OSError("the ledger is locked")
        return real(before, lines)
    monkeypatch.setattr(app, "ledger_digest", digest)
    evening = datetime(2026, 11, 12, 18, 0, tzinfo=TZ)
    with pytest.raises(OSError):
        app.chat_backup(evening)
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["2026-11-12"]
    fail.clear()
    app.chat_backup(evening + timedelta(minutes=30))
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["2026-11-12"]
    assert [c[1] for c in ledger_lines(record)] == ["私信备份/2026-11-12/chats.sqlite3", "私信备份/2026-11-12/私信记录.zip", "="]
    app.chat_backup(evening + timedelta(hours=1))  # listed now: a second copy the same day is a folder of its own
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["2026-11-12", "2026-11-12-190000"]


def test_a_failed_copy_is_said_where_the_owner_looks(store, record, monkeypatch):
    def broken(now):
        raise OSError("disk full")
    monkeypatch.setattr(app, "chat_backup", broken)
    monkeypatch.setattr(app, "chat_notes_ready", lambda: False)
    monkeypatch.setitem(app.state, "sheet_error", None)

    class Telegram:
        ready = asyncio.Event()
    Telegram.ready.set()
    monkeypatch.setattr(app, "tg", Telegram)
    sleeps = []

    async def sleep(seconds):  # the wait after a start passes, the one after the first look ends the loop
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(app.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.chat_upkeep_loop())
    assert app.state["sheet_error"].startswith("私信备份没做成")


def test_the_store_is_copied_whole_while_it_is_in_use(store, tmp_path):
    store.upsert_conv(KEY)
    store.add_messages(KEY, [msg(n, 5, text=f"m{n}") for n in range(50)])
    dest = tmp_path / "copy.sqlite3"
    store.backup(dest)
    store.add_messages(KEY, [msg(99, 1, text="after the copy")])
    copy = chats.Store(dest)
    try:
        assert copy.count(KEY) == 50 and store.count(KEY) == 51 and not dest.with_name("copy.sqlite3.part").exists()
    finally:
        copy.db.close()


# ---------------------------------------------------------------- E: the digest is mailed to one's own address


@pytest.fixture
def mailbox(monkeypatch):
    """The mail backend as the hash mail meets it: what was sent, and the outcome a test wants the next send to
    have (an exception is raised, a string is the server's answer). The mail is switched on, as the owner did."""
    class Bridge:
        def __init__(self):
            self.sent, self.outcome = [], None

        async def send(self, **kwargs):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            self.sent.append(kwargs)
            return self.outcome or f"Email sent successfully to {', '.join(kwargs['recipients'])}"
    bridge = Bridge()
    monkeypatch.setattr(app, "bg", bridge)
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    monkeypatch.setitem(app.CFG["chats"], "hash_mail", True)
    return bridge


def transcript(record, day="2026-11-12", text="| 10:00 | Discord | K0103 | 对方 | agreed: 300 USD |\n"):
    record.mkdir(exist_ok=True)
    md = record / f"{day}.md"
    md.write_text(text, encoding="utf-8", newline="\n")
    return md


DAY = datetime(2026, 11, 12, 18, 5, tzinfo=TZ)


def test_the_digest_is_mailed_to_ones_own_address_once_and_only_where_switched_on(store, record, mailbox, monkeypatch):
    transcript(record)
    done = app.chat_backup(DAY)
    assert config_default_is_off()
    for name, value in (("hash_mail", False), ("hash_mail", "yes")):  # off, or not a plain true: nothing is sent
        monkeypatch.setitem(app.CFG["chats"], name, value)
        assert asyncio.run(app.chat_hash_send()) == "" and mailbox.sent == []
    monkeypatch.setitem(app.CFG["chats"], "hash_mail", True)
    monkeypatch.setattr(app, "DEV", True)  # a dev copy next to the real service never sends
    assert asyncio.run(app.chat_hash_send()) == "" and mailbox.sent == []
    monkeypatch.setattr(app, "DEV", False)
    assert asyncio.run(app.chat_hash_send()) == "sent"
    [mail] = mailbox.sent
    assert set(mail) == {"recipients", "subject", "body"}  # plain text: no signature, no banner, nothing attached
    assert mail["recipients"] == ["me@example.com"] and mail["subject"] == "KOL Mail 私信留存哈希 2026-11-12"
    lines = ["\t".join(c) for c in ledger_lines(record)]
    assert len(lines) == 4 and all(line in mail["body"].splitlines() for line in lines)
    assert done["digest"] in mail["body"].splitlines() and "已记过的内容有变化" not in mail["body"]
    assert store.get_kv(app.CHAT_HASH_MAILED) == done["digest"]
    assert asyncio.run(app.chat_hash_send()) == "" and len(mailbox.sent) == 1  # the same run is never mailed twice


def config_default_is_off() -> bool:
    return app.config.DEFAULTS["chats"]["hash_mail"] is False


def test_runs_not_mailed_yet_go_in_one_mail_and_a_long_list_is_cut_at_its_start(store, record, mailbox, monkeypatch):
    md = transcript(record)
    app.chat_backup(DAY)
    assert asyncio.run(app.chat_hash_send()) == "sent"
    app.chat_backup(DAY + timedelta(days=1))  # the computer was off, or the mail did not get through that day
    md.write_text(md.read_text(encoding="utf-8").replace("300 USD", "900 USD"), encoding="utf-8", newline="\n")
    last = app.chat_backup(DAY + timedelta(days=2))
    assert asyncio.run(app.chat_hash_send()) == "sent"
    mail = mailbox.sent[-1]
    assert mail["subject"].endswith("2026-11-14") and last["digest"] in mail["body"].splitlines()
    listed = [line.split("\t")[1] for line in mail["body"].splitlines() if line.count("\t") == 4]
    assert listed == ["私信备份/2026-11-13/chats.sqlite3", "私信备份/2026-11-13/私信记录.zip", "=",
                      "私信记录/2026-11-12.md", "私信备份/2026-11-14/chats.sqlite3", "私信备份/2026-11-14/私信记录.zip", "="]
    assert "注意，这次有文件里已经记过的内容和上次不一样" in mail["body"]  # the changed file is pointed at

    monkeypatch.setattr(app, "CHAT_HASH_LINES", 2)
    app.chat_backup(DAY + timedelta(days=3))
    assert asyncio.run(app.chat_hash_send()) == "sent"
    body = mailbox.sent[-1]["body"]
    assert "（前面还有 1 行没有列出，见台账）" in body and "2026-11-15/chats.sqlite3" not in body
    assert "私信备份/2026-11-15/私信记录.zip" in body and "注意" not in body


def test_a_mail_that_never_left_costs_no_try_and_an_unknown_outcome_is_tried_three_times(store, record, mailbox):
    transcript(record)
    first = app.chat_backup(DAY)
    soon, send = DAY + timedelta(hours=1), lambda now: asyncio.run(app.chat_hash_send(now))
    mailbox.outcome = app.NotReady("reconnecting")
    assert [send(soon) for _ in range(5)] == ["waiting"] * 5
    assert store.get_kv(app.CHAT_HASH_TRIED) == f"{first['digest']} 0" and not app.state.get("sheet_error")
    assert send(DAY + timedelta(hours=27)) == "waiting"  # a day on it is said: silence would pass for "mailed"
    assert "邮箱一直没连上" in app.state["sheet_error"] and "2026-11-12 18:05" in app.state["sheet_error"]
    app.state["sheet_error"] = None
    mailbox.outcome = app.MailError("timed out")  # the server may have taken it
    assert [send(soon) for _ in range(3)] == ["unknown"] * 3
    assert "发送结果不确定" in app.state["sheet_error"] and store.get_kv(app.CHAT_HASH_MAILED) is None
    app.state["sheet_error"] = None  # a sheet write that went well empties the field: the next look says it again
    assert [send(soon) for _ in range(2)] == ["", ""] and "3 次都没确认发出" in app.state["sheet_error"]

    second = app.chat_backup(DAY + timedelta(days=1))  # a new run starts its own count
    mailbox.outcome = "Email delivery [failed: me@example.com]"
    assert send(DAY + timedelta(days=1)) == "unknown" and len(mailbox.sent) == 1
    mailbox.outcome = None
    assert send(DAY + timedelta(days=1)) == "sent"
    body = mailbox.sent[-1]["body"]
    assert "私信备份/2026-11-12/chats.sqlite3" in body and "私信备份/2026-11-13/chats.sqlite3" in body  # both runs
    assert store.get_kv(app.CHAT_HASH_MAILED) == second["digest"]
    assert send(DAY + timedelta(days=1)) == ""


def test_a_mail_that_left_but_could_not_be_noted_is_not_mailed_at_every_look_for_ever(store, record, mailbox, monkeypatch):
    transcript(record)
    app.chat_backup(DAY)
    set_kv = store.set_kv

    def full_disk(key, value):
        if key == app.CHAT_HASH_MAILED:
            raise sqlite3.OperationalError("database or disk is full")
        set_kv(key, value)
    monkeypatch.setattr(store, "set_kv", full_disk)
    for _ in range(6):
        try:
            asyncio.run(app.chat_hash_send(DAY))
        except sqlite3.OperationalError:
            pass
    assert len(mailbox.sent) == app.CHAT_HASH_TRIES


def test_a_ledger_line_cut_off_in_its_sum_is_no_run_and_hides_none(store, record, mailbox):
    transcript(record)
    done = app.chat_backup(DAY)
    ledger = record.parent / "私信备份" / app.CHAT_LEDGER
    with ledger.open("a", encoding="utf-8", newline="\n") as f:
        f.write("2026-11-13T18:00:00+08:00\t=\t3\t")  # the power went while the next run's sum was being written
    assert [d for d, _ in app.ledger_runs(ledger)] == [done["digest"]]
    assert asyncio.run(app.chat_hash_send(DAY)) == "sent" and done["digest"] in mailbox.sent[0]["body"]


def test_a_copy_that_fails_never_keeps_the_digest_from_being_mailed(store, record, mailbox, monkeypatch):
    transcript(record)
    app.chat_backup(datetime.now(TZ) - timedelta(days=2))  # made, never mailed
    store.set_kv(app.CHAT_BACKUP_AT, "")

    def broken(now):
        raise OSError("disk full")
    monkeypatch.setattr(app, "chat_backup", broken)
    monkeypatch.setattr(app, "chat_notes_ready", lambda: False)
    monkeypatch.setattr(app, "tg", type("Tg", (), {"ready": type("Ready", (), {"wait": staticmethod(no_wait)})()})())
    monkeypatch.setattr(app.asyncio, "sleep", naps(2))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.chat_upkeep_loop())
    assert len(mailbox.sent) == 1


async def no_wait():
    return True


def naps(last: int):
    """asyncio.sleep for a loop under test: returns at once, and cancels the loop at its `last` call."""
    calls = []

    async def nap(seconds):
        calls.append(seconds)
        if len(calls) >= last:
            raise asyncio.CancelledError
    return nap


def test_the_upkeep_loop_mails_the_copy_it_just_made(store, record, mailbox, monkeypatch):
    transcript(record)
    monkeypatch.setattr(app, "chat_notes_ready", lambda: False)
    monkeypatch.setattr(app, "tg", type("Tg", (), {"ready": type("Ready", (), {"wait": staticmethod(no_wait)})()})())
    monkeypatch.setattr(app.asyncio, "sleep", naps(2))  # the first is the wait after a start, the second ends the look
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.chat_upkeep_loop())
    assert len(mailbox.sent) == 1 and store.get_kv(app.CHAT_BACKUP_AT)
    assert store.get_kv(app.CHAT_HASH_MAILED) in mailbox.sent[0]["body"]
