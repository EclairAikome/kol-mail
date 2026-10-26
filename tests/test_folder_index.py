"""Folder indexes hold every message of a folder: mass outreach must not push older replies out of a conversation."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

BOX = "Sent Items"


@pytest.fixture(autouse=True)
def keep_app_state():
    """The tests swap the bg session and the index globals: put them back for the other test files."""
    saved = app.bg, app.INDEX_DIR, dict(app.known_index), dict(app.folder_index), dict(app.index_resynced)
    yield
    app.bg, app.INDEX_DIR = saved[0], saved[1]
    for target, old in zip((app.known_index, app.folder_index, app.index_resynced), saved[2:]):
        target.clear()
        target.update(old)


class FakeFolder:
    """list_emails_metadata over one folder, newest first, with the server's page and total fields."""
    name = "fake"

    def __init__(self, uids):
        self.uids = list(uids)
        self.calls = []

    def meta(self, uid):
        who = "kol@example.com" if uid == 5 else f"lead{uid}@example.com"
        return {"email_id": str(uid), "message_id": f"<{uid}@example.com>", "subject": "Re: Collab",
                "sender": "Me <me@example.com>", "recipients": [who], "date": "2026-10-20T10:00:00Z"}

    async def call(self, tool, mailbox, page=1, page_size=10, **_):
        self.calls.append(page)
        newest = sorted(self.uids, reverse=True)
        chunk = newest[(page - 1) * page_size:page * page_size]
        return {"emails": [self.meta(u) for u in chunk], "total": len(newest)}


def reset(tmp_path):
    app.INDEX_DIR = tmp_path / "index"
    app.known_index.clear()
    app.folder_index.clear()
    app.index_resynced.clear()


async def refresh_and_settle(folder):
    got = await app.refresh_index(folder, BOX)
    while app.background or app.inflight:
        await asyncio.sleep(0.01)
    return got


def ids(box=BOX):
    return [int(e["email_id"]) for e in app.folder_index[box][1]]


def test_whole_folder_after_first_start(tmp_path):
    reset(tmp_path)
    folder = FakeFolder(range(1, 251))
    app.bg = folder

    async def go():
        first = await refresh_and_settle(folder)
        assert len(first) == 100  # the click gets the newest page at once
        assert ids() == list(range(250, 0, -1))  # the bg re-read filled in the rest
    asyncio.run(go())
    assert folder.calls == [1, 1, 2, 3]


def test_restart_reads_one_page(tmp_path):
    reset(tmp_path)
    folder = FakeFolder(range(1, 251))
    app.bg = folder
    asyncio.run(refresh_and_settle(folder))

    app.known_index.clear()  # a restart: only the copy on disk is left
    app.folder_index.clear()
    app.index_resynced.clear()
    folder.uids += [251, 252, 253]
    folder.calls.clear()
    asyncio.run(refresh_and_settle(folder))
    assert folder.calls == [1]
    assert ids() == list(range(253, 0, -1))


def test_old_reply_stays_in_the_conversation(tmp_path):
    reset(tmp_path)
    folder = FakeFolder(range(1, 251))
    app.bg = folder
    asyncio.run(refresh_and_settle(folder))
    for box in app.index_boxes():
        if box != BOX:
            app.folder_index[box] = (app.time.time(), [])
    members = asyncio.run(app.thread_members("kol@example.com"))
    assert [(box, m["email_id"]) for box, m in members] == [(BOX, "5")]


def test_another_mailbox_keeps_its_newest_page(tmp_path):
    """Another mailbox is read sparingly: no copy on disk, no full re-read, and the primary's copy never answers."""
    reset(tmp_path)
    folder = FakeFolder(range(1, 251))
    app.bg = folder
    asyncio.run(refresh_and_settle(folder))
    with app.on_account("second_mailbox"):
        assert app.saved_index(BOX) == []
        other = FakeFolder(range(1, 251))
        app.bg = other
        asyncio.run(refresh_and_settle(other))
        assert ids() == list(range(250, 150, -1)) and other.calls == [1]
        assert not app.index_path(BOX).exists()
    assert ids() == list(range(250, 0, -1))


def test_the_index_belongs_to_the_mailbox_not_to_the_primary_role(tmp_path, monkeypatch):
    """After another mailbox becomes the primary one, it never reads the old primary's copy."""
    reset(tmp_path)
    folder = FakeFolder(range(1, 251))
    app.bg = folder
    asyncio.run(refresh_and_settle(folder))
    old = app.index_path(BOX)
    assert old.exists() and old.parent != app.INDEX_DIR
    monkeypatch.setattr(app, "ACCOUNT", "second_mailbox")  # what a restart after the handover looks like
    with app.on_account("second_mailbox"):
        assert app.saved_index(BOX) == [] and app.index_path(BOX) != old
    app.forget_account("me")  # logging out of the old primary removes its copy
    assert not old.exists()


def test_the_one_mailbox_copy_moves_into_the_primarys_folder(tmp_path):
    reset(tmp_path)
    legacy = app.INDEX_DIR / app.index_path(BOX).name
    legacy.parent.mkdir(parents=True)
    legacy.write_text(app.json.dumps({"box": BOX, "emails": [FakeFolder([]).meta(7)]}), encoding="utf-8")
    app.adopt_legacy_indexes()
    assert not legacy.exists() and app.index_path(BOX).exists()
    assert [e["email_id"] for e in app.saved_index(BOX)] == ["7"]


def test_moved_mail_leaves_the_index(tmp_path):
    reset(tmp_path)
    folder = FakeFolder(range(1, 251))
    app.bg = folder
    asyncio.run(refresh_and_settle(folder))
    folder.uids.remove(10)
    app.index_resynced.clear()
    asyncio.run(refresh_and_settle(folder))
    assert 10 not in ids() and len(ids()) == 249


def test_the_newest_mail_moved_out_leaves_the_index(tmp_path):
    """Triage files the mail that just came in into a deal folder: the next re-read must not keep it."""
    reset(tmp_path)
    folder = FakeFolder(range(1, 301))
    app.bg = folder
    asyncio.run(refresh_and_settle(folder))
    folder.uids.remove(300)
    app.index_resynced.clear()
    asyncio.run(refresh_and_settle(folder))
    assert ids() == list(range(299, 0, -1))
    folder.calls.clear()
    asyncio.run(refresh_and_settle(folder))
    assert folder.calls == [1]  # the totals agree now: no further full re-read


def test_a_one_mailbox_copy_never_replaces_the_primarys_own(tmp_path):
    reset(tmp_path)
    own = app.index_path(BOX)
    own.parent.mkdir(parents=True)
    own.write_text(app.json.dumps({"box": BOX, "emails": [FakeFolder([]).meta(9)]}), encoding="utf-8")
    legacy = app.INDEX_DIR / own.name
    legacy.write_text(app.json.dumps({"box": BOX, "emails": [FakeFolder([]).meta(7)]}), encoding="utf-8")
    app.adopt_legacy_indexes()
    assert not legacy.exists() and [e["email_id"] for e in app.saved_index(BOX)] == ["9"]


def test_small_folder_is_its_newest_page(tmp_path):
    reset(tmp_path)
    folder = FakeFolder(range(1, 40))
    app.bg = folder
    asyncio.run(refresh_and_settle(folder))
    folder.uids.remove(3)
    asyncio.run(refresh_and_settle(folder))
    assert folder.calls == [1, 1]
    assert ids() == [u for u in range(39, 0, -1) if u != 3]
