"""私信 (chats.py and app.py's /api/chat and /ext/discord): Telegram and Discord chats stored once per message, matched
to KOL总表 rows, written to 沟通记录 only when linked, and replies that only go out verified and unchanged."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import chats  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

CH = "1100000000000000001"
KEY = f"dc:{CH}"
ME, PEER = "1200000000000000002", "1300000000000000003"


def at(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def msg(mid, minutes_ago, out=False, text="hi"):
    return {"id": str(mid), "at": at(minutes_ago), "out": out, "author": "Mira" if out else "PixelFox_",
            "text": text, "extra": {}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = chats.Store(tmp_path / "chats.sqlite3")
    monkeypatch.setattr(app, "CHATS", s)
    # module state the code under test writes: each test starts from empty copies
    monkeypatch.setattr(app, "chat_locks", {})
    monkeypatch.setattr(app, "chat_send_locks", {})
    monkeypatch.setattr(app, "chat_sent", {})
    monkeypatch.setattr(app, "chat_contexts", {})
    monkeypatch.setattr(app, "state", {**app.state, "ext_seen": None, "chat_tg_error": None})
    return s


@pytest.fixture
def client(store, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    monkeypatch.setattr(app, "EXT_TOKEN_FILE", tmp_path / "ext_token")
    monkeypatch.setattr(app, "ext_token_cache", [])
    monkeypatch.setattr(app, "chat_rows_cache", {"at": 0.0, "rows": [], "error": None})
    monkeypatch.setattr(app, "spawn", lambda coro, what: coro.close())  # background work is tested on its own
    return TestClient(app.app, base_url=f"http://127.0.0.1:{app.PORT}")


def local(**extra):
    return {"X-KOL-Mail": "1", "Origin": f"http://127.0.0.1:{app.PORT}", **extra}


def ext(**extra):
    return {"X-KOL-Ext": app.ext_token(), "Origin": "chrome-extension://abcdefghijklmnop", **extra}


# ---------------------------------------------------------------- chats.py


def test_messages_are_stored_once_and_an_edit_replaces_the_words(store):
    store.upsert_conv(KEY, peer_id=PEER, peer_name="PixelFox_")
    assert [m["id"] for m in store.add_messages(KEY, [msg(1, 10), msg(2, 9, out=True)])] == ["1", "2"]
    assert store.add_messages(KEY, [msg(1, 10), msg(2, 9, out=True)]) == []
    store.set_zh(KEY, "1", "你好")
    store.add_messages(KEY, [msg(1, 10, text="hello there")])
    m = store.messages(KEY)[0]
    assert m["text"] == "hello there" and m["zh"] is None
    conv = store.conv(KEY)
    assert conv["last_in_at"] and conv["last_out_at"] and not chats.unread(conv)  # we answered


def test_unread_means_they_wrote_after_we_did_and_after_it_was_opened(store):
    store.upsert_conv(KEY, peer_id=PEER)
    store.add_messages(KEY, [msg(1, 10, out=True), msg(2, 5)])
    assert chats.unread(store.conv(KEY))
    store.seen(KEY)
    assert not chats.unread(store.conv(KEY))


def test_an_empty_value_never_wipes_what_is_known_of_the_peer(store):
    store.upsert_conv(KEY, peer_id=PEER, peer_name="PixelFox_", username="pixelfox_")
    store.upsert_conv(KEY, peer_id=None, peer_name="", username=None)
    c = store.conv(KEY)
    assert (c["peer_id"], c["peer_name"], c["username"]) == (PEER, "PixelFox_", "pixelfox_")
    with pytest.raises(ValueError):
        store.upsert_conv("dc:abc")


def test_peer_tokens_are_what_the_sheet_column_holds():
    assert chats.peer_token({"key": KEY, "peer_id": PEER, "peer_name": "PixelFox_ (FR)"}) == f"dc:{PEER}(PixelFox_ FR)"
    assert chats.peer_token({"key": "tg:5550001234", "peer_id": "5550001234", "username": "tea_cat",
                             "peer_name": "Котик"}) == "tg:5550001234(@tea_cat)"
    assert chats.peer_token({"key": KEY, "peer_id": None}) == ""


def test_suggestions_find_the_row_behind_a_chat_name():
    rows = [{"id": "K0103", "kol": "PixelFox"}, {"id": "K0260", "kol": "RiverOwlTTV"},
            {"id": "K0052", "kol": "MoonBakerYT"}, {"id": "K0001", "kol": "Shi"},
            {"id": "K0188", "kol": "Star Girl", "chat": f"tg:42(@sg); dc:{PEER}(x)"}]
    assert [r["id"] for r in chats.suggest({"key": KEY, "peer_name": "PixelFox_"}, rows)] == ["K0103"]
    assert [r["id"] for r in chats.suggest({"key": "dc:1", "peer_name": "RiverOwl"}, rows)] == ["K0260"]
    assert [r["id"] for r in chats.suggest({"key": "dc:2", "peer_name": "MoonBaker"}, rows)] == ["K0052"]
    hit = chats.suggest({"key": KEY, "peer_id": PEER, "peer_name": "nobody"}, rows)
    assert [r["id"] for r in hit] == ["K0188"]  # the 聊天账号 column wins over any name


def test_runs_group_one_sides_messages_close_together():
    ms = [msg(1, 100), msg(2, 99), msg(3, 50, out=True), msg(4, 49, out=True), msg(5, 5), msg(6, 1)]
    ms[5]["at"] = at(-30)  # 35 minutes after the one before: a new row
    assert [[m["id"] for m in r] for r in chats.runs(ms)] == [["1", "2"], ["3", "4"], ["5"], ["6"]]


def test_the_transcript_is_one_table_per_local_day_with_pipes_made_safe(tmp_path):
    conv = {"key": KEY, "kol_id": "K0103", "kol_name": "PixelFox", "peer_name": "PixelFox_"}
    tz = timezone(timedelta(hours=8))
    chats.append_transcript(tmp_path, conv, [msg(1, 10, text="a|b\nc")], tz)
    chats.append_transcript(tmp_path, conv, [msg(2, 5, out=True, text="ok")], tz)
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.count("| 时间 |") == 1 and "| Discord | K0103 | PixelFox | 收 | a｜b / c |" in text and "| 发 | ok |" in text


def test_a_new_discord_draft_replaces_what_was_not_sent(store):
    store.upsert_conv(KEY)
    store.queue(KEY, [{"text": "one"}, {"text": "two"}])
    first = store.pending(KEY)
    assert store.update_item(KEY, first[0]["id"], "sent")
    store.queue(KEY, [{"text": "new", "zh": "新"}])
    assert [(i["text"], i["n"], i["of_n"]) for i in store.pending(KEY)] == [("new", 1, 1)]
    assert not store.update_item(KEY, first[1]["id"], "sent")  # dropped by the new draft
    new = store.pending(KEY)[0]["id"]
    assert store.update_item(KEY, new, "filled") and not store.update_item(KEY, new, "filled")  # one tab claims it
    assert store.update_item(KEY, new, "sent")


# ---------------------------------------------------------------- /ext/discord


def sync_body(messages):
    return {"channel_id": CH, "peer": {"id": PEER, "name": "PixelFox_", "username": "pixelfox_"},
            "me": {"id": ME, "name": "Mira"}, "messages": messages}


def test_the_extension_needs_its_key_and_an_extension_origin(client):
    assert client.post("/ext/discord/hello", json={}).status_code == 403
    assert client.post("/ext/discord/hello", json={}, headers={"X-KOL-Ext": "0" * 32}).status_code == 403
    page = {"X-KOL-Ext": app.ext_token(), "Origin": "https://discord.com"}
    assert client.post("/ext/discord/hello", json={}, headers=page).status_code == 403
    assert client.post("/ext/discord/hello", json={}, headers=ext()).json()["ok"]
    old = app.ext_token()
    assert client.post("/api/chat/ext", headers=local()).json()["token"] != old  # 重新生成
    assert client.post("/ext/discord/hello", json={}, headers={"X-KOL-Ext": old}).status_code == 403


def test_a_synced_dm_is_stored_and_suggests_its_row(client, store):
    app.chat_rows_cache.update(at=9e18, rows=[{"id": "K0103", "kol": "PixelFox", "chat": ""}])
    r = client.post("/ext/discord/sync", headers=ext(), json=sync_body([
        {"id": "1400000000000000001", "at": at(5), "author_id": ME, "author": "Mira", "out": True, "text": "Bonjour"},
        {"id": "1400000000000000002", "at": at(4), "author_id": PEER, "author": "PixelFox_", "out": False,
         "text": "Salut", "attachments": [{"name": "stats.png", "url": "https://cdn.discordapp.com/a/stats.png"}]},
        {"id": "not-a-number", "at": at(3), "text": "dropped"}]))
    d = r.json()
    assert d["conv"]["key"] == KEY and d["conv"]["added"] == 2 and d["conv"]["linked"] is None
    assert d["conv"]["suggest"] == [{"id": "K0103", "kol": "PixelFox"}]
    assert [m["text"] for m in store.messages(KEY)] == ["Bonjour", "Salut"]
    assert store.messages(KEY)[1]["extra"]["attachments"][0]["name"] == "stats.png"
    assert client.post("/ext/discord/sync", headers=ext(), json={"channel_id": "abc"}).status_code == 400


def test_queue_items_go_to_the_extension_and_come_back_sent(client, store):
    store.upsert_conv(KEY)
    store.queue(KEY, [{"text": "one", "zh": "一"}, {"text": "two", "zh": "二"}])
    items = client.post("/ext/discord/queue", headers=ext(), json={"channel_id": CH}).json()["items"]
    assert [(i["n"], i["of"], i["text"], i["zh"]) for i in items] == [(1, 2, "one", "一"), (2, 2, "two", "二")]
    r = client.post("/ext/discord/queue/update", headers=ext(),
                    json={"channel_id": CH, "item_id": items[0]["id"], "status": "sent"})
    assert r.json()["ok"]
    assert [i["text"] for i in store.pending(KEY)] == ["two"]
    assert client.post("/ext/discord/queue/update", headers=ext(),
                       json={"channel_id": CH, "item_id": items[1]["id"], "status": "bogus"}).status_code == 400


# ---------------------------------------------------------------- linking and 沟通记录


def test_a_linked_chat_writes_its_runs_to_the_sheet_and_the_transcript(store, tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr(app, "sheet_post", posted.append)
    monkeypatch.setattr(app, "CHAT_RECORD", tmp_path / "私信记录")

    async def fake_claude(system, user, schema, model, timeout, files=None):
        return {"zh": ["你好，价格多少？"]}
    monkeypatch.setattr(app, "run_claude", fake_claude)
    store.upsert_conv(KEY, peer_id=PEER, peer_name="PixelFox_")
    store.add_messages(KEY, [msg(1, 60 * 24 * 10, text="ancient"), msg(2, 30, text="Hello, what is the price?"),
                             msg(3, 20, out=True, text="你好，我们按条付费")])
    asyncio.run(app.chat_record(KEY))
    assert posted == []  # not linked yet: nothing reaches the sheet
    store.link(KEY, "K0103", "PixelFox", "pixelfox@example.com")
    asyncio.run(app.chat_record(KEY))
    assert [(p["dir"], p["summary"], p["id"], p["channel"]) for p in posted] == [
        ("in", "对方消息：你好，价格多少？", "K0103", "Discord"), ("out", "我方消息：你好，我们按条付费", "K0103", "Discord")]
    assert posted[0]["chat"] == f"dc:{PEER}(PixelFox_)" and posted[0]["email"] == "pixelfox@example.com"
    assert posted[0]["message_id"] == f"{KEY}:2"
    assert all(m["logged"] for m in store.messages(KEY))  # the 10-day-old one was skipped, not logged
    assert "ancient" not in "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "私信记录").glob("*.md"))
    posted.clear()
    asyncio.run(app.chat_record(KEY))
    assert posted == []  # each message once


def test_linking_checks_the_row_and_teaches_the_sheet_the_account(client, store, monkeypatch):
    monkeypatch.setattr(app, "sheet_enabled", lambda: True)
    monkeypatch.setattr(app, "SHEET_MODE", "legacy")
    monkeypatch.setattr(app, "sheet_ops", app.collections.deque())
    monkeypatch.setattr(app, "sheet_wake", app.asyncio.Event())

    async def rows(force=False):
        return [{"id": "K0103", "kol": "PixelFox", "email": "pixelfox@example.com", "chat": ""}]
    monkeypatch.setattr(app, "chat_rows", rows)
    store.upsert_conv(KEY, peer_id=PEER, peer_name="PixelFox_")
    assert client.post("/api/chat/link", headers=local(), json={"key": KEY, "id": "K9999"}).status_code == 400
    r = client.post("/api/chat/link", headers=local(), json={"key": KEY, "id": "K0103"}).json()
    assert r["conv"]["linked"] == {"id": "K0103", "kol": "PixelFox", "email": "pixelfox@example.com"}
    assert list(app.sheet_ops) == [{"op": "upsert", "id": "K0103", "chat": f"dc:{PEER}(PixelFox_)", "create": False,
                                    "source": "KOL Mail 私信关联"}]
    assert client.post("/api/chat/unlink", headers=local(), json={"key": KEY}).json()["conv"]["linked"] is None


# ---------------------------------------------------------------- sending


def test_a_reply_goes_out_only_verified_and_only_if_they_wrote_nothing_new(client, store, monkeypatch):
    store.upsert_conv(KEY, peer_id=PEER)
    store.add_messages(KEY, [msg(1, 10)])
    text = "Salut !\n\nOn peut faire 2 vidéos."
    body = {"key": KEY, "text": text, "zh": ["你好！", "我们可以做 2 条视频。"], "newest": "1"}
    assert client.post("/api/chat/send", headers=local(), json=body).status_code == 400  # no verified Chinese yet
    monkeypatch.setitem(app.checked_texts, app.text_hash(text), 0.0)
    assert client.post("/api/chat/send", headers=local(), json={**body, "newest": ""}).status_code == 400  # no snapshot
    store.add_messages(KEY, [msg(2, 1, text="wait")])
    r = client.post("/api/chat/send", headers=local(), json=body)
    assert r.status_code == 409 and "对方" in r.json()["error"]
    store.add_messages(KEY, [msg(3, 0.5, out=True, text="typed in Discord")])
    r = client.post("/api/chat/send", headers=local(), json={**body, "newest": "2"})
    assert r.status_code == 409 and "你刚" in r.json()["error"]  # the owner answered by hand meanwhile
    r = client.post("/api/chat/send", headers=local(), json={**body, "newest": "3"}).json()
    assert r == {"ok": True, "queued": 2}
    assert [(i["text"], i["zh"]) for i in store.pending(KEY)] == [("Salut !", "你好！"), ("On peut faire 2 vidéos.",
                                                                                         "我们可以做 2 条视频。")]
    assert client.post("/api/chat/send", headers=local(), json={**body, "newest": "3"}).status_code == 400  # twice: no


def test_a_discord_draft_goes_stale_when_they_write_and_is_done_when_our_words_appear(store):
    store.upsert_conv(KEY, peer_id=PEER)
    store.add_messages(KEY, [msg(1, 10)])
    store.queue(KEY, [{"text": "One"}, {"text": "Two  words"}])
    store.add_messages(KEY, [msg(2, -0.1, out=True, text="one")])  # sent by the owner, case aside
    assert [i["text"] for i in store.pending(KEY)] == ["Two  words"]
    store.add_messages(KEY, [msg(3, -0.2, text="actually, wait")])  # they wrote after the draft was made
    assert store.pending(KEY) == []


def test_a_linked_chat_catches_up_from_the_sheets_newest_row(store, tmp_path, monkeypatch):
    """Rows a Claude session wrote cover what came before them; what came after goes in, once."""
    posted = []
    monkeypatch.setattr(app, "sheet_post", posted.append)
    monkeypatch.setattr(app, "CHAT_RECORD", tmp_path / "私信记录")
    monkeypatch.setattr(app, "SHEET_MODE", "legacy")
    monkeypatch.setattr(app, "sheet_enabled", lambda: True)
    local_time = (datetime.now(app.autoreply.TZ) - timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M")

    async def get(kol_id, timeout=60):
        return {"recent_logs": [{"时间": local_time, "渠道": "Discord", "摘要": "会话里记的"},
                                {"时间": "2099-01-01 00:00", "渠道": "邮件", "摘要": "a mail: another channel"}]}
    monkeypatch.setattr(app, "sheet_get", get)

    async def no_claude(*a, **k):
        raise app.ClaudeError("offline")
    monkeypatch.setattr(app, "run_claude", no_claude)
    store.upsert_conv(KEY, peer_id=PEER)
    store.add_messages(KEY, [msg(1, 180, text="covered by the session"), msg(2, 90, text="after the session's row"),
                             msg(3, 10, out=True, text="ours, recent")])
    store.link(KEY, "K0103", "PixelFox", "")
    store.skip_old(KEY, datetime.now(timezone.utc) - timedelta(hours=1))  # what chat_autolink does at once
    asyncio.run(app.chat_catch_up(KEY))
    assert [p["summary"] for p in posted] == ["对方消息：after the session's row", "我方消息：ours, recent"]


def test_unlinking_by_hand_keeps_the_sheet_account_from_linking_it_back(store, monkeypatch):
    monkeypatch.setattr(app, "chat_rows_cache", {"at": 9e18, "error": None, "rows": [
        {"id": "K0103", "kol": "PixelFox", "email": "", "chat": f"dc:{PEER}(PixelFox_)"}]})
    monkeypatch.setattr(app, "spawn", lambda coro, what: coro.close())  # the catch-up is tested on its own
    store.upsert_conv(KEY, peer_id=PEER, peer_name="PixelFox_")
    app.chat_autolink(KEY)
    assert store.conv(KEY)["kol_id"] == "K0103"
    store.unlink(KEY)
    app.chat_autolink(KEY)
    assert store.conv(KEY)["kol_id"] is None
    store.link(KEY, "K0103", "PixelFox", "")  # by hand again: allowed, and autolink is back on
    assert store.conv(KEY)["no_autolink"] == 0


def test_a_display_name_naming_another_account_never_matches_it():
    rows = [{"id": "K1", "kol": "x", "chat": "dc:99(tg:12345)"}]
    assert chats.token_row({"key": "tg:12345", "peer_id": "12345"}, rows) is None


def test_extra_arriving_later_is_kept_and_a_long_one_stays_valid(store):
    store.upsert_conv(KEY)
    store.add_messages(KEY, [{**msg(1, 5, text=""), "extra": {"media": "voice"}}])
    store.add_messages(KEY, [{**msg(1, 5, text=""), "extra": {"media": "voice", "transcript": "x" * 9000}}])
    extra = store.messages(KEY)[0]["extra"]
    assert extra["media"] == "voice" and len(extra["transcript"]) == chats.EXTRA_FIELD_MAX


class FakeTelegram:
    def __init__(self, history):
        self.history, self.sent = history, []
        self.ready = asyncio.Event()
        self.error = None

    async def call(self, tool, timeout=180, raw=False, **args):
        if tool == "get_history":
            return {"results": self.history}
        if tool == "send_message":
            self.sent.append(args["message"])
            return {"result": f"Message sent successfully to chat {args['chat_id']}"}
        raise AssertionError(tool)


def test_a_telegram_reply_is_sent_part_by_part(client, store, monkeypatch):
    fake = FakeTelegram([{"id": 7, "date": at(3), "text": "Сколько стоит?", "sender": "Котик", "username": "tea_cat"}])
    monkeypatch.setattr(app, "tg", fake)
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app, "CHAT_SEND_GAP", (0, 0))
    closed = []
    monkeypatch.setattr(app, "close_cases_for", lambda channel, peer: closed.append((channel, peer)))
    text = "Привет!\n\nДве интеграции по 80$."
    monkeypatch.setitem(app.checked_texts, app.text_hash(text), 0.0)
    assert asyncio.run(app.tg_sync_chat(5550001234)) == 1  # what the listener or the sync loop did before
    r = client.post("/api/chat/send", headers=local(), json={"key": "tg:5550001234", "text": text, "newest": "7"})
    assert r.json() == {"ok": True, "sent": 2} and fake.sent == ["Привет!", "Две интеграции по 80$."]
    assert closed == [("telegram", "5550001234")]
    conv = store.conv("tg:5550001234")
    assert conv["peer_name"] == "Котик" and conv["username"] == "tea_cat"


def test_a_send_that_broke_off_is_never_repeated(client, store, monkeypatch):
    fake = FakeTelegram([{"id": 7, "date": at(3), "text": "?", "sender": "Котик"}])

    async def flaky(tool, timeout=180, raw=False, **args):
        if tool == "send_message" and fake.sent:
            raise app.MailError("connection reset")  # the second part: may or may not have gone out
        return await FakeTelegram.call(fake, tool, timeout, raw, **args)
    monkeypatch.setattr(fake, "call", flaky)
    monkeypatch.setattr(app, "tg", fake)
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app, "CHAT_SEND_GAP", (0, 0))
    text = "Один.\n\nДва."
    monkeypatch.setitem(app.checked_texts, app.text_hash(text), 0.0)
    asyncio.run(app.tg_sync_chat(5550001234))
    body = {"key": "tg:5550001234", "text": text, "newest": "7"}
    r = client.post("/api/chat/send", headers=local(), json=body)
    assert r.status_code == 502 and r.json()["uncertain"] and r.json()["sent"] == 1
    assert client.post("/api/chat/send", headers=local(), json=body).status_code == 400
    assert fake.sent == ["Один."]


def test_a_part_too_long_for_one_chat_message_is_refused(client, store, monkeypatch):
    store.upsert_conv(KEY, peer_id=PEER)
    store.add_messages(KEY, [msg(1, 10)])
    text = "x" * (chats.PART_MAX["discord"] + 1)
    monkeypatch.setitem(app.checked_texts, app.text_hash(text), 0.0)
    r = client.post("/api/chat/send", headers=local(), json={"key": KEY, "text": text, "newest": "1"})
    assert r.status_code == 400 and "拆成几段" in r.json()["error"]


def test_a_chat_whose_account_the_sheet_lists_links_itself_without_replaying_history(store, monkeypatch):
    monkeypatch.setattr(app, "chat_rows_cache", {"at": 9e18, "error": None, "rows": [
        {"id": "K0337", "kol": "Ветер Тест", "email": "", "chat": "tg:5550001234(@tea_cat)"},
        {"id": "K0001", "kol": "other", "email": "", "chat": "tg:55500012340(@not_him)"}]})
    fake = FakeTelegram([{"id": 1, "date": at(60 * 30), "text": "old news", "sender": "Котик"},
                         {"id": 2, "date": at(5), "text": "just now", "sender": "Котик"}])
    monkeypatch.setattr(app, "tg", fake)
    monkeypatch.setattr(app, "spawn", lambda coro, what: coro.close())
    asyncio.run(app.tg_sync_chat(5550001234))
    conv = store.conv("tg:5550001234")
    assert conv["kol_id"] == "K0337"
    assert [m["id"] for m in store.unlogged("tg:5550001234")] == ["2"]  # the last hour only
    assert chats.token_row({"key": "tg:1", "peer_id": "1"}, app.chat_rows_cache["rows"]) is None


@pytest.mark.parametrize("work_time, ran_min_ago, runs", [(True, None, 1), (True, 150, 1), (True, 30, 0), (False, None, 0)])
def test_the_popo_loop_runs_popo_sync_in_working_hours_every_two_hours(store, tmp_path, monkeypatch, work_time,
                                                                       ran_min_ago, runs):
    marker = tmp_path / "ran.txt"
    script = tmp_path / "popo_sync.py"  # stands in for kol-sheet\popo_sync.py: records the call, answers like it
    script.write_text("import json, sys\nopen(sys.argv[0] + '.ran', 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                      "print(json.dumps({'status': 'ok', 'added': [{'kol': 'x'}], 'proposals': []}))\n",
                      encoding="utf-8")
    state_file = tmp_path / "popo_sync_state.json"
    if ran_min_ago is not None:
        state_file.write_text("{}", encoding="utf-8")
        t = (datetime.now(timezone.utc) - timedelta(minutes=ran_min_ago)).timestamp()
        import os
        os.utime(state_file, (t, t))
    monkeypatch.setattr(app, "POPO_SYNC", script)
    monkeypatch.setattr(app, "POPO_SYNC_STATE", state_file)
    monkeypatch.setattr(app, "SHEET_PYTHON", sys.executable)
    monkeypatch.setattr(app, "SHEET_MODE", "legacy")
    monkeypatch.setattr(app, "SHEET_ENV", None)

    class Schedule:
        def is_work_time(self, moment):
            return work_time

    class Config:
        schedule = Schedule()
    monkeypatch.setattr(app, "load_autoreply_config", lambda: Config())
    ticks = []
    real_sleep = asyncio.sleep

    async def tick(seconds, *a, **k):  # the loop's 10-minute wait: once through, then stop
        ticks.append(seconds)
        if len(ticks) > 1:
            raise asyncio.CancelledError
        await real_sleep(0)
    monkeypatch.setattr(app.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.popo_sync_loop())
    ran = Path(str(script) + ".ran")
    assert (ran.read_text(encoding="utf-8").split("\n").count("run") if ran.exists() else 0) == runs
    assert bool(store.get_kv("popo_tried_at")) == bool(runs)
    assert app.state.get("popo_error") is None


def test_popo_sync_counts_any_recent_run_and_its_own_tries(store, tmp_path, monkeypatch):
    state_file = tmp_path / "popo_sync_state.json"
    monkeypatch.setattr(app, "POPO_SYNC_STATE", state_file)
    assert app.popo_last_run() is None
    state_file.write_text("{}", encoding="utf-8")  # the triage task (or anyone) just ran popo_sync
    assert datetime.now(timezone.utc) - app.popo_last_run() < timedelta(minutes=1)
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).timestamp()
    import os
    os.utime(state_file, (old, old))
    store.set_kv("popo_tried_at", datetime.now(app.autoreply.TZ).isoformat())  # a try of ours that failed
    assert datetime.now(timezone.utc) - app.popo_last_run() < timedelta(minutes=1)


def test_only_one_to_one_chats_with_people_count():
    assert app.tg_private(5550001234, "tea_cat")
    assert not app.tg_private(-100123, "")
    assert not app.tg_private(777000, "")
    assert not app.tg_private(55, "KolMailBot")
    assert not app.tg_private(True, "")
