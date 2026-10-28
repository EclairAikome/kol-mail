"""The folder menu (create, rename, move, delete), moving mails between folders and the server's flag (已标记)."""

import asyncio
import json
import sys
import time
import types
from pathlib import Path

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import folders  # noqa: E402
import mailserver  # noqa: E402

NAMES = ["Outbox", "Deleted Items", "Sent Items", "Drafts", "Junk E-mail", "Trash", "纯固薪", "CPS+固薪", "纯CPS",
         "Partners", "Partners/Asia", "INBOX"]
ROLES = {"sent": "Sent Items", "junk": "Junk E-mail", "drafts": "Drafts", "trash": "Deleted Items"}


def post(handler, body: dict):
    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""}, receive)
    return asyncio.run(handler(request))


def answer(resp) -> tuple[int, dict]:
    return resp.status_code, json.loads(resp.body)


@pytest.fixture
def mailbox(monkeypatch):
    """Fresh per-account caches, the WorkMail folder list, and a fake server that records every call."""
    for name in ("lists", "folder_info", "bodies", "marked_read", "unread", "unread_tried", "flagged", "flag_tried",
                 "flag_marks", "folder_index", "known_index", "index_resynced", "inflight", "box_gen"):
        monkeypatch.setattr(app, name, app.PerAccount(getattr(app, name)._factory))
    app.folder_info["names"], app.folder_info["roles"] = list(NAMES), dict(ROLES)
    app.folder_info["special"], app.folder_info["delimiter"] = ["Deleted Items", "Sent Items", "Drafts", "Junk E-mail"], "/"
    monkeypatch.setattr(app, "spawn", lambda coro, what: coro.close())
    monkeypatch.setattr(app, "INDEX_DIR", Path(app.DATA) / "index-test")
    monkeypatch.setattr(app, "HTML_MAIL", True)  # the patched mail server, which brings the rename and delete tools
    calls = []
    replies = {"list_mailboxes": {"result": [{"name": n, "flags": [], "delimiter": "/"} for n in NAMES]}}

    async def call(tool, **args):
        calls.append((tool, args))
        reply = replies.get(tool)
        if isinstance(reply, Exception):
            raise reply
        return reply(args) if callable(reply) else (reply if reply is not None else {})
    # bg picks its session per mailbox (SplitBridge): its two sessions are patched, never the router itself
    for bridge in (app.mail, app.bg.primary, app.bg.others, app.finder):
        monkeypatch.setattr(bridge, "call", call)
    return types.SimpleNamespace(calls=calls, replies=replies)


# ---------------------------------------------------------------- folders.py

def test_names_and_places():
    assert folders.leaf("Partners/Asia", "/") == "Asia" and folders.parent("Partners/Asia", "/") == "Partners"
    assert folders.parent("Partners", "/") == "" and folders.join("", "X", "/") == "X"
    assert folders.rename_target("Partners/Asia", "Japan", "/") == "Partners/Japan"
    assert folders.move_target("Partners/Asia", "", "/") == "Asia"
    assert folders.move_target("Asia", "Partners", "/") == "Partners/Asia"
    assert folders.renamed("Partners/Asia", "Partners", "KOL", "/") == "KOL/Asia"
    assert folders.renamed("PartnersX", "Partners", "KOL", "/") == "PartnersX"  # only real subfolders follow
    assert folders.inside("Partners/Asia", "Partners", "/") and not folders.inside("PartnersX", "Partners", "/")


def test_typed_names_that_cannot_be_used():
    assert folders.leaf_problem("", "/") and folders.leaf_problem("a/b", "/") and folders.leaf_problem("inbox", "/")
    assert folders.leaf_problem("50%", "/") and folders.leaf_problem("x" * 101, "/")
    assert folders.leaf_problem("待跟进 KOL", "/") is None


def test_a_folder_never_goes_into_itself():
    names = ["Partners", "Partners/Asia", "Partners/Asia/JP", "Other"]
    assert folders.move_problem("Partners", "Partners/Asia", "/", names)
    assert folders.move_problem("Partners/Asia", "Partners", "/", names)  # already there
    assert folders.move_problem("Partners/Asia", "Gone", "/", names)
    assert folders.move_problem("Partners/Asia", "Other", "", names)  # a flat mailbox has no levels
    assert folders.move_problem("Partners/Asia", "Other", "/", names) is None
    assert folders.move_problem("Partners/Asia", "", "/", names) is None


def test_locked_and_custom_folders(mailbox):
    locked = app.locked_boxes()
    assert {"纯固薪", "CPS+固薪", "纯CPS", "Sent Items", "Deleted Items", "Outbox", "Trash"} <= locked
    assert app.custom_boxes() == ("Partners", "Partners/Asia")
    assert app.thread_boxes() == ("INBOX", "纯固薪", "CPS+固薪", "纯CPS", "Partners", "Partners/Asia")
    assert "Sent Items" in app.flag_boxes() and "Junk E-mail" not in app.flag_boxes()


def test_another_mailbox_keeps_inbox_and_deal_folders_only(mailbox, monkeypatch):
    monkeypatch.setattr(app, "is_primary", lambda: False)
    assert app.custom_boxes() == ()


# ---------------------------------------------------------------- the folder menu

def test_create_rename_and_move_go_to_the_server(mailbox):
    mailbox.replies.update(create_mailbox={"status": "created"}, kolmail_rename_mailbox={"status": "renamed"},
                           list_mailboxes={"result": [{"name": n, "flags": [], "delimiter": "/"} for n in NAMES]})
    assert answer(post(app.api_folder_create, {"parent": "Partners", "name": "Europe"})) == \
        (200, {"ok": True, "folder": "Partners/Europe"})
    assert answer(post(app.api_folder_rename, {"mailbox": "Partners/Asia", "name": "APAC"}))[1]["folder"] == "Partners/APAC"
    assert answer(post(app.api_folder_move, {"mailbox": "Partners/Asia", "parent": ""}))[1]["folder"] == "Asia"
    sent = [(t, a) for t, a in mailbox.calls if t != "list_mailboxes"]
    assert sent == [("create_mailbox", {"mailbox": "Partners/Europe"}),
                    ("kolmail_rename_mailbox", {"mailbox": "Partners/Asia", "new_name": "Partners/APAC"}),
                    ("kolmail_rename_mailbox", {"mailbox": "Partners/Asia", "new_name": "Asia"})]


def test_locked_folders_are_refused_before_the_server_is_asked(mailbox):
    for handler, body in ((app.api_folder_rename, {"mailbox": "纯固薪", "name": "x"}),
                          (app.api_folder_move, {"mailbox": "Sent Items", "parent": "Partners"}),
                          (app.api_folder_delete, {"mailbox": "INBOX"}),
                          (app.api_folder_delete, {"mailbox": "Junk E-mail"})):
        status, data = answer(post(handler, body))
        assert status == 400 and data["error"], body
    assert "分拣" in answer(post(app.api_folder_rename, {"mailbox": "纯固薪", "name": "x"}))[1]["error"]
    assert answer(post(app.api_folder_create, {"parent": "", "name": "Partners"}))[1]["error"] == "已经有同名的文件夹了"
    assert {t for t, _ in mailbox.calls} <= {"list_mailboxes"}  # read the folder list at most, changed nothing


def test_without_the_patched_server_folders_can_only_be_created(mailbox, monkeypatch):
    monkeypatch.setattr(app, "HTML_MAIL", False)
    for handler, body in ((app.api_folder_rename, {"mailbox": "Partners", "name": "x"}),
                          (app.api_folder_move, {"mailbox": "Partners/Asia", "parent": ""}),
                          (app.api_folder_delete, {"mailbox": "Partners/Asia"})):
        assert "重新运行安装命令" in answer(post(handler, body))[1]["error"]
    assert mailbox.calls == []


def test_a_server_that_says_the_name_is_taken(mailbox):
    mailbox.replies["kolmail_rename_mailbox"] = {"status": "exists"}
    assert answer(post(app.api_folder_rename, {"mailbox": "Partners", "name": "Taken"})) == \
        (400, {"error": "已经有同名的文件夹了"})


def test_delete_moves_the_mails_to_deleted_items_first(mailbox):
    pages = [[{"email_id": str(i)} for i in range(1, 101)], [{"email_id": "101"}], []]
    mailbox.replies.update(list_emails_metadata=lambda args: {"emails": pages.pop(0)},
                           move_emails={"result": "Successfully moved"}, kolmail_delete_mailbox={"status": "deleted"})
    mailbox.replies["list_mailboxes"] = {"result": [{"name": n, "flags": [], "delimiter": "/"}
                                                    for n in NAMES if n != "Partners/Asia"]}  # what the server lists
    assert answer(post(app.api_folder_delete, {"mailbox": "Partners"})) == \
        (200, {"ok": True, "moved": 101, "trash": "Deleted Items"})
    moves = [a for t, a in mailbox.calls if t == "move_emails"]
    assert [len(m["email_ids"]) for m in moves] == [100, 1]
    assert {(m["source_mailbox"], m["destination_mailbox"]) for m in moves} == {("Partners", "Deleted Items")}
    assert [t for t, _ in mailbox.calls][-2:] == ["kolmail_delete_mailbox", "list_mailboxes"]
    lists = [a for t, a in mailbox.calls if t == "list_emails_metadata"]
    assert all(a.get("since") for a in lists)  # straight from IMAP, never the server's lagging projection


def test_delete_refuses_a_folder_with_subfolders(mailbox):
    assert "子文件夹" in answer(post(app.api_folder_delete, {"mailbox": "Partners"}))[1]["error"]
    assert {t for t, _ in mailbox.calls} == {"list_mailboxes"}  # a fresh folder list, nothing moved


def test_a_failed_move_keeps_the_folder(mailbox):
    mailbox.replies.update(list_emails_metadata={"emails": [{"email_id": "5"}]},
                           move_emails={"result": "Move result [failed: 5]"})
    status, data = answer(post(app.api_folder_delete, {"mailbox": "Partners/Asia"}))
    assert status == 502 and "文件夹先留着" in data["error"]
    assert "kolmail_delete_mailbox" not in [t for t, _ in mailbox.calls]


# ---------------------------------------------------------------- moving mails

def test_moved_mails_leave_the_caches_of_their_folder(mailbox):
    meta = {"email_id": "7", "message_id": "<m7@x>", "subject": "Hi", "sender": "kol@x.com"}
    app.lists[("INBOX", 1)] = (time.time(), {"emails": [meta], "total": 1})
    app.unread["INBOX"] = (time.time(), {"7": meta, "8": {}}, 2)
    app.flagged["INBOX"] = (time.time(), {"7": meta}, 1)
    app.bodies[("INBOX", "7")] = {"body": "x"}
    mailbox.replies["move_emails"] = {"result": "Successfully moved 1 email(s) to Partners"}
    assert answer(post(app.api_move, {"mailbox": "INBOX", "email_ids": ["7"], "to": "Partners"})) == \
        (200, {"ok": True, "moved": 1, "ids": ["7"]})
    assert ("INBOX", 1) not in app.lists and ("INBOX", "7") not in app.bodies
    assert set(app.unread["INBOX"][1]) == {"8"} and app.unread["INBOX"][2] == 1 and app.flagged["INBOX"][1] == {}
    assert mailbox.calls == [("move_emails", {"email_ids": ["7"], "source_mailbox": "INBOX",
                                              "destination_mailbox": "Partners"})]


def test_a_dragged_row_takes_its_whole_conversation(mailbox):
    def meta(uid, subject, who="kol@x.com"):
        return {"email_id": uid, "message_id": f"<{uid}@x>", "subject": subject, "sender": who,
                "recipients": ["me@example.com"]}
    # page 1 holds the newest mail; the folder index knows the older ones of the same conversation
    app.lists[("INBOX", 1)] = (time.time(), {"emails": [meta("9", "Re: Collab")], "total": 3})
    app.folder_index["INBOX"] = (time.time(), [meta("9", "Re: Collab"), meta("4", "Collab"),
                                               meta("2", "Other thing"), meta("1", "Collab", "else@y.com")])
    mailbox.replies["move_emails"] = {"result": "Successfully moved"}
    status, data = answer(post(app.api_move, {"mailbox": "INBOX", "email_ids": ["9"], "to": "Partners",
                                              "whole_thread": True}))
    assert status == 200 and data["ids"] == ["9", "4"] and data["moved"] == 2


def test_a_move_to_junk_starts_no_search_of_junk(mailbox, monkeypatch):
    searched = []

    async def refresh(bridge, box):
        searched.append(box)
    for name in ("refresh_index", "refresh_unread", "refresh_flagged"):
        monkeypatch.setattr(app, name, refresh)
    asyncio.run(app.prefetch_after_folder_change("Junk E-mail"))
    asyncio.run(app.prefetch_after_folder_change("Deleted Items"))
    assert searched == []
    asyncio.run(app.prefetch_after_folder_change("Partners"))
    assert searched == ["Partners"] * 3


def test_a_search_that_overlapped_a_move_stores_nothing(mailbox):
    def slow_search(args):
        app.forget_moved("INBOX", ["7"])  # the move lands while the search is out
        return {"emails": [{"email_id": "7"}], "total": 1}
    mailbox.replies["list_emails_metadata"] = slow_search
    asyncio.run(app.refresh_flagged(app.bg, "INBOX"))
    asyncio.run(app.refresh_unread(app.bg, "INBOX"))
    assert "INBOX" not in app.flagged and "INBOX" not in app.unread


def test_moves_that_make_no_sense_are_refused(mailbox):
    for body in ({"mailbox": "INBOX", "email_ids": ["7"], "to": "INBOX"},
                 {"mailbox": "INBOX", "email_ids": ["7"], "to": "Gone"},
                 {"mailbox": "INBOX", "email_ids": ["7"], "to": "Outbox"},
                 {"mailbox": "INBOX", "email_ids": [str(i) for i in range(501)], "to": "Partners"},
                 {"mailbox": "INBOX", "email_ids": [], "to": "Partners"}):
        assert answer(post(app.api_move, body))[0] == 400, body
    assert mailbox.calls == []


# ---------------------------------------------------------------- flags

def test_a_flag_shows_at_once_and_stays_once_confirmed(mailbox):
    meta = {"email_id": "7", "message_id": "<m7@x>", "subject": "Hi", "sender": "kol@x.com", "date": "2026-10-27T10:00:00Z"}
    app.lists[("INBOX", 1)] = (time.time(), {"emails": [meta], "total": 1})
    mailbox.replies["set_email_flags"] = {"result": "Successfully added \\Flagged to 1 email(s)"}
    assert answer(post(app.api_flag, {"mailbox": "INBOX", "email_ids": ["7"], "on": True}))[1]["flagged_count"] == 1
    assert mailbox.calls[-1] == ("set_email_flags", {"mailbox": "INBOX", "email_ids": ["7"], "flags": ["\\Flagged"],
                                                    "operation": "add"})
    assert app.is_flagged("INBOX", "7") and [m["email_id"] for _, m in app.flagged_mails()] == ["7"]
    # a search that started before the server confirmed it does not undo the mark; one that started after decides
    app.flag_marks[("INBOX", "7")] = (True, time.time() + 60)
    mailbox.replies["list_emails_metadata"] = {"emails": [], "total": 0}
    asyncio.run(app.refresh_flagged(app.bg, "INBOX"))
    assert app.is_flagged("INBOX", "7")
    app.flag_marks[("INBOX", "7")] = (True, time.time() - 60)
    asyncio.run(app.refresh_flagged(app.bg, "INBOX"))
    assert not app.is_flagged("INBOX", "7") and ("INBOX", "7") not in app.flag_marks


def test_no_flags_where_they_would_never_be_checked_again(mailbox):
    for box in ("Junk E-mail", "Drafts", "Deleted Items"):
        assert answer(post(app.api_flag, {"mailbox": box, "email_ids": ["7"], "on": True}))[0] == 400
    assert mailbox.calls == [] and not app.flag_marks


def test_a_flag_the_server_refused_is_taken_back(mailbox):
    mailbox.replies["set_email_flags"] = {"result": "Set-flags result [failed: 7]"}
    with pytest.raises(app.MailError):
        post(app.api_flag, {"mailbox": "INBOX", "email_ids": ["7"], "on": True})
    assert ("INBOX", "7") not in app.flag_marks and not app.is_flagged("INBOX", "7")


def test_flagged_view_lists_every_folder_newest_first(mailbox, monkeypatch):
    monkeypatch.setattr(app.cache, "get_many", lambda ids: {})
    older = {"email_id": "3", "message_id": "<a@x>", "subject": "Older", "sender": "a@x.com", "date": "2026-10-20T10:00:00Z"}
    newer = {"email_id": "9", "message_id": "<b@x>", "subject": "Newer", "sender": "me@example.com",
             "recipients": ["b@x.com"], "date": "2026-10-27T10:00:00Z"}

    def search(args):
        assert args["flagged"] is True
        found = {"INBOX": [older], "Sent Items": [newer]}.get(args["mailbox"], [])
        return {"emails": found, "total": len(found)}
    mailbox.replies["list_emails_metadata"] = search
    request = Request({"type": "http", "method": "GET", "path": "/api/flagged", "headers": [], "query_string": b"mode=live"})
    data = json.loads(asyncio.run(app.api_flagged(request)).body)
    assert [(e["mailbox"], e["subject"]) for e in data["emails"]] == [("Sent Items", "Newer"), ("INBOX", "Older")]
    assert data["total"] == 2 and data["failed"] == [] and all(e["flagged"] for e in data["emails"])


# ---------------------------------------------------------------- mailserver.py: rename and delete on IMAP

class FakeImap:
    def __init__(self, folders, messages=0, answer="OK"):
        self.folders, self.messages, self.answer, self.sent = folders, messages, answer, []

    async def list(self, ref, pattern):
        return ("OK", self.folders)

    async def status(self, name, items):
        return ("OK", [f'"{name}" (MESSAGES {self.messages})'.encode()])

    async def rename(self, old, new):
        self.sent.append(("RENAME", old, new))
        return (self.answer, [])

    async def delete(self, name):
        self.sent.append(("DELETE", name))
        return (self.answer, [])


def fake_server(imap):
    info = lambda name, flags=(): types.SimpleNamespace(name=name, delimiter="/", flags=list(flags))  # noqa: E731

    async def nothing(*a, **k):
        return None
    classic = types.SimpleNamespace(
        _imap_login=nothing, _send_imap_id=nothing, _best_effort_imap_logout=nothing,
        _raise_for_imap_error=lambda r, op: None, _parse_list_responses=lambda items: [info(*i) for i in items],
        _quote_mailbox=lambda m: f'"{m}"', _imap_status=lambda r: r[0],
        _imap_effect_status=lambda r: {"OK": "succeeded", "NO": "failed"}.get(r[0], "unknown"))
    checked = []

    async def connect():
        return imap
    client = types.SimpleNamespace(_connect_imap=connect, _check_mutation_authority=lambda: checked.append(1),
                                   email_server=types.SimpleNamespace(user_name="u", password=types.SimpleNamespace(
                                       get_secret_value=lambda: "p")))
    return classic, client, checked


def test_server_rename_statuses():
    imap = FakeImap([("INBOX",), ("A",), ("B",)])
    classic, client, checked = fake_server(imap)
    assert asyncio.run(mailserver.rename_mailbox(classic, client, "Gone", "C")) == "missing"
    assert asyncio.run(mailserver.rename_mailbox(classic, client, "A", "B")) == "exists"
    assert imap.sent == [] and checked == []
    assert asyncio.run(mailserver.rename_mailbox(classic, client, "A", "B/A")) == "renamed"
    assert imap.sent == [("RENAME", '"A"', '"B/A"')] and checked == [1]


def test_server_delete_only_takes_an_empty_folder_without_subfolders():
    folders_ = [("INBOX",), ("A",), ("A/B",), ("C",)]
    classic, client, _ = fake_server(FakeImap(folders_))
    assert asyncio.run(mailserver.delete_mailbox(classic, client, "A")) == "has_children"
    classic, client, _ = fake_server(FakeImap(folders_, messages=3))
    assert asyncio.run(mailserver.delete_mailbox(classic, client, "C")) == "not_empty"
    imap = FakeImap(folders_)
    classic, client, _ = fake_server(imap)
    assert asyncio.run(mailserver.delete_mailbox(classic, client, "C")) == "deleted" and imap.sent == [("DELETE", '"C"')]
    classic, client, _ = fake_server(FakeImap([("INBOX",), ("[Gmail]", ["\\Noselect"])]))
    assert asyncio.run(mailserver.delete_mailbox(classic, client, "[Gmail]")) == "missing"
    # the count comes from the attribute list, never from the folder name the STATUS line repeats
    imap = FakeImap([("INBOX",), ("x (MESSAGES 0)",)], messages=5)
    classic, client, _ = fake_server(imap)
    assert asyncio.run(mailserver.delete_mailbox(classic, client, "x (MESSAGES 0)")) == "not_empty" and imap.sent == []
