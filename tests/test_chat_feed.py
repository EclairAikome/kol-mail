"""0.10: every Discord DM reaches 私信 without being opened (/ext/discord/feed, read from Discord's own data by the
extension's page script), the two readings of one message never overwrite each other, a long chat is shown a page at
a time and never cut off, and the chat list is one query."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import chats  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

CH, OTHER, THIRD = "1100000000000000001", "1100000000000000002", "1100000000000000003"
KEY = f"dc:{CH}"
ME, PEER = "1200000000000000002", "1300000000000000003"


def at(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = chats.Store(tmp_path / "chats.sqlite3")
    monkeypatch.setattr(app, "CHATS", s)
    monkeypatch.setattr(app, "chat_locks", {})
    monkeypatch.setattr(app, "state", {**app.state, "ext_seen": None, "ext_feed": None, "chat_tg_error": None})
    return s


@pytest.fixture
def client(store, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    monkeypatch.setattr(app, "EXT_TOKEN_FILE", tmp_path / "ext_token")
    monkeypatch.setattr(app, "ext_token_cache", [])
    monkeypatch.setattr(app, "chat_rows_cache", {"at": 9e18, "rows": [], "error": None})
    monkeypatch.setattr(app, "spawn", lambda coro, what: coro.close())
    return TestClient(app.app, base_url=f"http://127.0.0.1:{app.PORT}")


def local(**extra):
    return {"X-KOL-Mail": "1", "Origin": f"http://127.0.0.1:{app.PORT}", **extra}


def ext(**extra):
    return {"X-KOL-Ext": app.ext_token(), "Origin": "chrome-extension://abcdefghijklmnop", **extra}


def message(n, minutes_ago, out=False, text="hi", **extra):
    return {"id": str(1400000000000000000 + n), "at": at(minutes_ago), "author_id": ME if out else PEER,
            "author": "Mira" if out else "PixelFox_", "out": out, "text": text, "attachments": [], "reply_to": None,
            **extra}


def conv(channel=CH, messages=(), last=None, name="PixelFox_", loaded=False):
    return {"channel_id": channel, "peer": {"id": PEER, "name": name, "username": name.lower()},
            "last": last, "messages": list(messages), "loaded": loaded}


def feed(client, convs=(), channels=()):
    return client.post("/ext/discord/feed", headers=ext(),
                       json={"me": {"id": ME, "name": "Mira"}, "convs": list(convs), "channels": list(channels)})


# ---------------------------------------------------------------- the feed

def test_a_dm_nobody_opened_reaches_the_chats(client, store):
    r = feed(client, [conv(CH, [message(1, 9, text="Hello!"), message(2, 8, out=True, text="Hi PixelFox")]),
                      conv(OTHER, [message(3, 7, text="Hey")], name="RiverOwl")])
    assert r.json() == {"ok": True, "added": 3, "auto": False, "want": []}
    assert [(m["text"], m["out"]) for m in store.messages(KEY)] == [("Hello!", False), ("Hi PixelFox", True)]
    assert store.conv(KEY)["peer_name"] == "PixelFox_" and store.conv(KEY)["peer_id"] == PEER
    assert store.conv(f"dc:{OTHER}")["peer_name"] == "RiverOwl"
    assert chats.unread(store.conv(f"dc:{OTHER}"))  # they wrote, nobody looked: it shows as unread in 私信
    assert feed(client, [conv(CH, [message(1, 9, text="Hello!")])]).json()["added"] == 0  # told twice, kept once
    assert app.state["ext_feed"]


def test_the_feed_needs_the_extensions_key_and_takes_only_what_has_the_right_shape(client, store):
    body = {"me": {"id": ME, "name": "Mira"}, "convs": [conv(CH, [message(1, 5)])], "channels": []}
    assert client.post("/ext/discord/feed", json=body).status_code == 403
    assert client.post("/ext/discord/feed", json=body, headers={"X-KOL-Ext": app.ext_token(),
                                                                 "Origin": "https://discord.com"}).status_code == 403
    assert client.post("/ext/discord/feed", headers=ext(), content=b"[1]").status_code == 400
    r = feed(client, [conv("abc", [message(1, 5)]), "x", conv(THIRD, [{"id": "nope", "at": at(1)}, {"id": "5"}]),
                      conv(OTHER, [])])
    assert r.json()["added"] == 0
    assert store.convs() == []  # a DM with nothing readable never becomes an empty row in 私信
    assert feed(client, [conv(CH, [])] * (app.EXT_FEED_CONVS + 1)).status_code == 400
    assert feed(client, [], [conv(CH)] * (app.EXT_FEED_CHANNELS + 1)).status_code == 400
    many = [message(i, 300 - i) for i in range(app.EXT_FEED_MESSAGES + 30)]
    assert feed(client, [conv(CH, many)]).json()["added"] == app.EXT_FEED_MESSAGES  # the newest hundred
    assert store.messages(KEY, 500)[-1]["id"] == many[-1]["id"]


def test_unopened_dms_are_asked_for_only_when_the_owner_switched_that_on(client, store):
    feed(client, [conv(CH, [message(5, 5)], last=str(1400000000000000005), loaded=True)])  # Discord loaded this DM
    snapshot = [conv(CH, last=str(1400000000000000005)),          # we hold its newest
                conv(OTHER, last=str(1400000000000000009)),        # never seen here
                conv(THIRD, last="not-an-id")]
    assert feed(client, [], snapshot).json() == {"ok": True, "added": 0, "auto": False, "want": []}
    assert client.get("/api/chat/ext", headers=local()).json()["auto"] is False
    token = app.ext_token()
    d = client.post("/api/chat/ext", headers=local(), json={"auto": True}).json()
    assert d["auto"] is True and d["token"] == token  # the switch never makes a new key
    assert feed(client, [], snapshot).json()["want"] == [OTHER]
    newer = [conv(CH, last=str(1400000000000000007))]              # they wrote again, the page did not tell us
    assert feed(client, [], newer).json()["want"] == [CH]
    store.ignore(KEY, True)                                        # "不是 KOL，忽略": never loaded for KOL Mail
    assert feed(client, [], newer).json()["want"] == []
    assert client.post("/api/chat/ext", headers=local(), json={"auto": False}).json()["auto"] is False
    assert feed(client, [], snapshot).json()["want"] == []


def test_what_is_read_off_the_screen_never_replaces_discords_own_data(client, store):
    """The page shows **bold** as bold and a mention as a name; Discord's data keeps the marks. Both readings reach
    KOL Mail for the DM that is open: without this the text, and with it the translation, changed on every sync."""
    raw = "**Rates**: $120 per video, <@1200000000000000002>"
    feed(client, [conv(CH, [message(1, 5, text=raw)])])
    store.set_zh(KEY, str(1400000000000000001), "报价：每条视频 120 美元")
    drawn = {"id": str(1400000000000000001), "at": at(5), "author_id": PEER, "author": "PixelFox_", "out": False,
             "text": "Rates: $120 per video, @Mira"}
    new = {"id": str(1400000000000000002), "at": at(4), "author_id": PEER, "author": "PixelFox_", "out": False,
           "text": "only on screen so far"}
    r = client.post("/ext/discord/sync", headers=ext(), json={
        "channel_id": CH, "peer": {"id": PEER, "name": "PixelFox_", "username": "pixelfox_"},
        "me": {"id": ME, "name": "Mira"}, "messages": [drawn, new]})
    assert r.json()["conv"]["added"] == 1
    first, second = store.messages(KEY)
    assert first["text"] == raw and first["zh"] == "报价：每条视频 120 美元"
    assert second["text"] == "only on screen so far"
    # the other way round: read off the screen first, then Discord's data arrives; the same words keep their Chinese
    store.set_zh(KEY, second["id"], "目前只在屏幕上")
    feed(client, [conv(CH, [message(2, 4, text="only on **screen** so far")])])
    second = store.messages(KEY)[1]
    assert second["text"] == "only on **screen** so far" and second["zh"] == "目前只在屏幕上"
    assert second["extra"]["src"] == "page"
    # a real edit is other words: its old translation goes
    feed(client, [conv(CH, [message(2, 4, text="only on screen so far, $150 then")])])
    assert store.messages(KEY)[1]["zh"] is None


def test_same_words_sets_only_markdown_and_spacing_aside():
    assert chats.same_words("**Rates**: $120", "Rates: $120")
    assert chats.same_words("Ｈｅｌｌｏ  world\n~~old~~ `code`", "hello world old code")
    assert chats.same_words("- one\n- two\n> quoted", "• one\n• two\nquoted")
    assert chats.same_words("PixelFox_ said __hi__", "PixelFox said hi")
    # an edit, however small, is other words: its translation must not stay
    assert not chats.same_words("$120 per video", "$150 per video")
    assert not chats.same_words("1,000 dollars", "1.000 dollars")
    assert not chats.same_words("3.5k views", "35k views")
    assert not chats.same_words("5-10 videos", "510 videos")
    assert not chats.same_words("We agree.", "We agree?")
    assert not chats.same_words("", "x") and chats.same_words("", "") and not chats.same_words("!!", "??")
    assert chats.same_words("👍", "👍") and not chats.same_words("**", "__")


def test_a_dm_is_asked_for_until_discord_loaded_it_and_not_again_for_a_message_we_never_keep(client, store):
    client.post("/api/chat/ext", headers=local(), json={"auto": True})
    live, call = str(1400000000000000005), str(1400000000000000009)
    feed(client, [conv(CH, [message(5, 5)], last=live)])           # one message arrived while the DM was never opened
    assert feed(client, [], [conv(CH, last=live)]).json()["want"] == [CH]  # its history is still missing
    feed(client, [conv(CH, [message(1, 9), message(5, 5)], last=live, loaded=True)])
    assert feed(client, [], [conv(CH, last=live)]).json()["want"] == []
    # then a call: Discord's newest message of the DM is one we never keep
    assert feed(client, [], [conv(CH, last=call)]).json()["want"] == [CH]
    feed(client, [conv(CH, [], last=call, loaded=True)])           # loaded again: nothing new to keep
    assert feed(client, [], [conv(CH, last=call)]).json()["want"] == []  # and it is not asked for on every page load
    assert store.count(KEY) == 2
    # a DM made of calls alone never becomes an empty row in 私信, and is not asked for twice either
    feed(client, [conv(OTHER, [], last=call, loaded=True)])
    assert store.conv(f"dc:{OTHER}") is None
    assert feed(client, [], [conv(OTHER, last=call)]).json()["want"] == []


# ---------------------------------------------------------------- a long chat, a page at a time

def test_a_long_chat_opens_on_its_newest_messages_and_reaches_its_first(client, store):
    store.upsert_conv(KEY, peer_id=PEER, peer_name="PixelFox_")
    store.add_messages(KEY, [{"id": str(1400000000000000000 + i), "at": at(1000 - i), "out": i % 3 == 0,
                              "author": "x", "text": f"m{i}", "extra": {}} for i in range(1, 251)])
    d = client.get(f"/api/chat/conv?key={KEY}", headers=local()).json()
    assert d["total"] == 250 and len(d["messages"]) == app.CHAT_PAGE
    assert [m["text"] for m in d["messages"]][-1] == "m250" and d["messages"][0]["text"] == f"m{251 - app.CHAT_PAGE}"
    seen = [m["text"] for m in d["messages"]]
    first = d["messages"][0]["id"]
    for _ in range(10):
        older = client.get(f"/api/chat/conv?key={KEY}&before={first}&limit=100", headers=local()).json()
        assert older["total"] == 250 and "conv" not in older
        if not older["messages"]:
            break
        seen = [m["text"] for m in older["messages"]] + seen
        first = older["messages"][0]["id"]
    assert seen == [f"m{i}" for i in range(1, 251)]  # every message once, in order: nothing is lost
    assert client.get(f"/api/chat/conv?key={KEY}&before=nope", headers=local()).json()["messages"] == []
    assert client.get(f"/api/chat/conv?key={KEY}&limit=x", headers=local()).status_code == 400
    assert len(client.get(f"/api/chat/conv?key={KEY}&limit=99999", headers=local()).json()["messages"]) == 250


def test_looking_at_earlier_messages_does_not_mark_the_chat_read(client, store):
    store.upsert_conv(KEY, peer_id=PEER)
    store.add_messages(KEY, [{"id": "1", "at": at(9), "out": False, "author": "x", "text": "a", "extra": {}},
                             {"id": "2", "at": at(8), "out": False, "author": "x", "text": "b", "extra": {}}])
    client.get(f"/api/chat/conv?key={KEY}&before=2", headers=local())
    assert chats.unread(store.conv(KEY))
    client.get(f"/api/chat/conv?key={KEY}", headers=local())
    assert not chats.unread(store.conv(KEY))


def test_same_second_messages_page_without_a_gap_or_a_repeat(store):
    store.upsert_conv(KEY)
    stamp = at(5)
    store.add_messages(KEY, [{"id": str(1400000000000000000 + i), "at": stamp, "out": False, "author": "x",
                              "text": f"m{i}", "extra": {}} for i in range(7)])
    page = store.messages(KEY, 3)
    older = store.messages(KEY, 3, before=page[0]["id"])
    oldest = store.messages(KEY, 3, before=older[0]["id"])
    assert [m["text"] for m in oldest + older + page] == [f"m{i}" for i in range(7)]


# ---------------------------------------------------------------- the list

def test_the_chat_list_shows_each_chats_newest_message(store):
    for n, key in enumerate(("dc:1100000000000000001", "tg:42", "dc:1100000000000000009")):
        store.upsert_conv(key, peer_id=str(n + 1), peer_name=f"P{n}")
    store.add_messages("dc:1100000000000000001", [
        {"id": "1", "at": at(30), "out": False, "author": "x", "text": "old", "extra": {}},
        {"id": "2", "at": at(3), "out": True, "author": "me", "text": "newest here", "extra": {}}])
    store.add_messages("tg:42", [{"id": "7", "at": at(1), "out": False, "author": "x", "text": "",
                                  "extra": {"media": "photo"}}])
    store.set_zh("dc:1100000000000000001", "2", "这里最新")
    rows = {r["key"]: r for r in store.convs()}
    # the latest chat first (one without messages counts from when it was made, here just now)
    assert [r["key"] for r in store.convs()] == ["dc:1100000000000000009", "tg:42", "dc:1100000000000000001"]
    assert rows["dc:1100000000000000001"]["preview"] == "这里最新" and rows["dc:1100000000000000001"]["preview_out"]
    assert rows["tg:42"]["preview"] == "[photo]" and not rows["tg:42"]["preview_out"] and rows["tg:42"]["unread"]
    assert rows["dc:1100000000000000009"]["preview"] == "" and not rows["dc:1100000000000000009"]["preview_out"]
    assert not any(k.startswith("p_") for r in rows.values() for k in r)
