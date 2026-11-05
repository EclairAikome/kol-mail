"""The company mailbox's move from Amazon WorkMail to Alibaba Mail (mailmove.py and its part in app.py)."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import accounts  # noqa: E402
import app  # noqa: E402
import mailmove  # noqa: E402

PRIMARY = accounts.Account("me", "me@example.com", "", True)
OTHER = accounts.Account("me_163", "me@example.org", "个人", False)
OLD = {"revision": 7, "incoming": {"host": "imap.mail.us-east-1.awsapps.com", "port": 993, "start_ssl": False},
       "outgoing": {"host": "smtp.mail.us-east-1.awsapps.com", "port": 465, "start_ssl": False}}
ELSEWHERE = {"revision": 2, "incoming": {"host": "imap.163.com", "port": 993}, "outgoing": {"host": "smtp.163.com", "port": 465}}


# ---------------------------------------------------------------- mailmove.py

def test_only_workmail_servers_move():
    assert mailmove.on_old_servers(OLD) and not mailmove.on_old_servers(ELSEWHERE)
    assert mailmove.on_old_servers({"incoming": {"host": "IMAP.mail.us-east-1.awsapps.com "}})
    preset = {"domain": "example.com", "imap_host": "imap.mail.us-east-1.awsapps.com",
              "smtp_host": "smtp.mail.us-east-1.awsapps.com"}
    assert mailmove.moved_preset(preset) == {"domain": "example.com", **mailmove.NEW_SERVERS}
    assert mailmove.moved_preset({"domain": "example.com", **mailmove.NEW_SERVERS}) is None
    assert mailmove.moved_preset({}) is None and mailmove.moved_preset(None) is None


def test_only_clearly_older_mail_stays_quiet_and_only_for_a_while():
    at = mailmove.CUTOVER
    quiet = lambda sent, when=at + timedelta(days=1): mailmove.quiet_old_mail(sent, when)  # noqa: E731
    assert quiet(at - timedelta(days=30))
    assert not quiet(at - timedelta(days=2))   # the weekend before the cutover: may never have been seen
    assert not quiet(at + timedelta(hours=1))
    # a computer that got this version a week late still hears about the mail of that week
    assert not quiet(at + timedelta(days=3), at + timedelta(days=7))
    assert not quiet(at - timedelta(days=30), at + timedelta(days=31))  # long after the cutover: back to normal
    assert not quiet(None)
    assert mailmove.is_old_host("imap.mail.eu-west-1.awsapps.com") and not mailmove.is_old_host("awsapps.com.evil.net")


TREE = ["Trash", "Draft", "eclair@example.com", "eclair@example.com/CPS+固薪", "eclair@example.com/Deleted Items",
        "eclair@example.com/Drafts", "eclair@example.com/INBOX", "eclair@example.com/INBOX/Partners",
        "eclair@example.com/Junk E-mail", "eclair@example.com/Outbox", "eclair@example.com/Sent Items",
        "eclair@example.com/Trash", "eclair@example.com/纯CPS", "eclair@example.com/纯固薪", "INBOX", "Sent", "Spam"]
ROLES = {"sent": "Sent", "drafts": "Draft", "trash": "Trash", "junk": "Spam"}


def test_the_moved_tree_goes_back_where_kol_mail_looks():
    plan = dict(mailmove.fold_plan(TREE, "Eclair@Example.com", "/", ROLES))
    assert plan == {"eclair@example.com/INBOX": "INBOX", "eclair@example.com/Sent Items": "Sent",
                    "eclair@example.com/Deleted Items": "Trash",
                    "eclair@example.com/Trash": "Trash", "eclair@example.com/Junk E-mail": "Spam",
                    "eclair@example.com/纯固薪": "纯固薪", "eclair@example.com/CPS+固薪": "CPS+固薪",
                    "eclair@example.com/纯CPS": "纯CPS", "eclair@example.com/INBOX/Partners": "INBOX/Partners"}
    order = [old for old, _ in mailmove.fold_plan(TREE, "eclair@example.com", "/", ROLES)]
    assert order.index("eclair@example.com/INBOX") < order.index("eclair@example.com/INBOX/Partners")
    # the deal folders, then Sent, then INBOX, junk and trash last: a big INBOX never keeps the rest away
    first = {"eclair@example.com/纯固薪", "eclair@example.com/CPS+固薪", "eclair@example.com/纯CPS"}
    assert set(order[:3]) == first and order[3] == "eclair@example.com/Sent Items"
    assert order[4] == "eclair@example.com/INBOX"
    assert set(order[-3:]) == {"eclair@example.com/Junk E-mail", "eclair@example.com/Deleted Items",
                               "eclair@example.com/Trash"}
    assert mailmove.fold_plan(["INBOX", "Sent"], "eclair@example.com", "/", ROLES) == []  # nothing moved over
    assert mailmove.fold_plan(TREE, "eclair@example.com", "", ROLES) == []  # a flat mailbox has no such tree
    no_junk = dict(mailmove.fold_plan(TREE, "eclair@example.com", "/", {"sent": "Sent"}))
    assert "eclair@example.com/Junk E-mail" not in no_junk  # nowhere to put it: left where it is


# ---------------------------------------------------------------- the mail component's command line

def test_the_command_line_for_new_servers_and_a_new_password(monkeypatch):
    cli, calls = accounts.Cli("mcp-email-server"), []

    async def fake_json(*args, secrets=()):
        calls.append((args, secrets))
        return {}
    monkeypatch.setattr(cli, "json", fake_json)
    asyncio.run(cli.update_servers("me", 7, mailmove.NEW_SERVERS))
    args, secrets = calls[0]
    assert args[:5] == ("account", "update", "me", "--expected-revision", "7") and not secrets
    assert "imap.us.alibabacloud.com" in args and "smtp.us.alibabacloud.com" in args and "--imap-ssl" in args
    asyncio.run(cli.set_password("me", "s3cret"))
    assert [c[0] for c in calls[1:]] == [("account", "set-secret", "me", role, "--password-stdin")
                                         for role in ("incoming", "outgoing")]
    assert all(c[1] == ("s3cret",) and "s3cret" not in c[0] for c in calls[1:])  # stdin only, never argv


# ---------------------------------------------------------------- the move at start

class FakeCli:
    def __init__(self, shows):
        self.shows, self.calls, self.receiving, self.sending = dict(shows), [], None, None

    async def show(self, name):
        if name not in self.shows:
            raise accounts.AccountError("no such account")
        return self.shows[name]

    async def update_servers(self, name, revision, servers):
        self.calls.append(("update", name, revision, servers["imap_host"]))
        self.shows[name] = {**self.shows[name], "incoming": {"host": servers["imap_host"]},
                            "outgoing": {"host": servers["smtp_host"]}}

    async def set_password(self, name, password):
        self.calls.append(("password", name, password))

    async def test(self, name, role):
        return self.receiving if role == "incoming" else self.sending


class FakeCases:
    def __init__(self):
        self.open = [{"id": 1, "channel": "email"}, {"id": 2, "channel": "telegram"}]
        self.moved = []

    def all_open(self):
        return list(self.open)

    def move(self, case_id, old, new):
        self.moved.append((case_id, new))


@pytest.fixture
def moving(monkeypatch, tmp_path):
    saved, spawned = {}, []
    monkeypatch.setattr(app, "ACCOUNTS", [PRIMARY, OTHER])
    monkeypatch.setattr(app, "ACCOUNT", "me")
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app, "restart_pending", False)
    monkeypatch.setattr(app, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setitem(app.CFG["mail"], "server_move", {})
    monkeypatch.setitem(app.CFG["mail"], "company_servers", {
        "domain": "example.com", "imap_host": "imap.mail.us-east-1.awsapps.com", "smtp_host": "smtp.mail.us-east-1.awsapps.com"})
    monkeypatch.setattr(app.config, "update", lambda path, value: saved.__setitem__(tuple(path), value))
    monkeypatch.setattr(app, "cases", FakeCases())
    monkeypatch.setattr(app, "spawn", lambda coro, what: (spawned.append(what), coro.close()))

    async def no_reload():
        return None
    monkeypatch.setattr(app, "reload_mail_sessions", no_reload)
    monkeypatch.setattr(app, "password_rejected", {})
    monkeypatch.setattr(app, "password_checked", {})
    cli = FakeCli({"me": OLD, "me_163": ELSEWHERE})
    monkeypatch.setattr(app, "mail_cli", cli)
    return cli, saved, spawned


def test_a_workmail_mailbox_moves_once_and_keeps_its_password(moving):
    cli, saved, _ = moving
    index = app.index_dir("me")
    index.mkdir(parents=True)
    (index / "inbox.json").write_text("{}", encoding="utf-8")
    other_index = app.index_dir("me_163")
    other_index.mkdir(parents=True)
    asyncio.run(app.move_company_mailboxes())
    assert cli.calls == [("update", "me", 7, "imap.us.alibabacloud.com")]  # the 163 mailbox stays as it is
    assert not index.exists() and other_index.exists()  # the old UIDs are gone, the other mailbox keeps its own
    rec = saved[("mail", "server_move")]
    assert rec["accounts"] == ["me"] and rec["at"] and "pending" not in rec
    assert saved[("mail", "company_servers")]["imap_host"] == "imap.us.alibabacloud.com"  # the login screen too
    assert app.cases.moved == [(1, "handled")]  # the phone's mail case, not the Telegram one
    assert app.moved_at("me") == rec["at"] and not app.moved_at("me_163")
    # the stored password is tried as it is: nothing asked, no password stored
    assert app.password_needed() == [] and not any(c[0] == "password" for c in cli.calls)
    asyncio.run(app.move_company_mailboxes())  # the next start: nothing left to do
    assert len(cli.calls) == 1 and app.server_move()["at"] == rec["at"]


def test_a_move_that_failed_half_way_is_finished_at_the_next_start(moving, monkeypatch):
    cli, saved, _ = moving

    async def refused(name, revision, servers):
        raise accounts.AccountError("revision changed", "revision_conflict")
    real = cli.update_servers
    cli.update_servers = refused
    asyncio.run(app.move_company_mailboxes())
    asyncio.run(app.move_company_mailboxes())  # failing start after start
    assert app.mailmove.on_old_servers(cli.shows["me"])  # still on WorkMail
    assert app.cases.moved == []  # the phone's cases stay open while the mailbox has not moved
    cli.update_servers = real
    asyncio.run(app.move_company_mailboxes())
    assert cli.calls == [("update", "me", 7, "imap.us.alibabacloud.com")] and app.server_move()["accounts"] == ["me"]
    assert app.cases.moved == [(1, "handled")]
    app.cases.open.append({"id": 3, "channel": "email"})  # opened after the move: the next start leaves it
    asyncio.run(app.move_company_mailboxes())
    assert app.cases.moved == [(1, "handled")]


def test_cases_a_start_left_open_right_after_the_move_are_closed_at_the_next(moving, monkeypatch):
    cli, _, _ = moving
    real = app.cases.all_open

    def broken():
        raise app.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(app.cases, "all_open", broken)
    asyncio.run(app.move_company_mailboxes())
    assert cli.calls and app.cases.moved == []  # moved, the cases not yet
    monkeypatch.setattr(app.cases, "all_open", real)
    asyncio.run(app.move_company_mailboxes())
    assert app.cases.moved == [(1, "handled")] and len(cli.calls) == 1


def test_an_index_that_cannot_be_removed_stops_the_move(moving, monkeypatch):
    cli, _, _ = moving
    real = app.shutil.rmtree

    def locked(path, *a, **k):
        raise PermissionError("in use")
    monkeypatch.setattr(app.shutil, "rmtree", locked)
    app.index_dir("me").mkdir(parents=True)
    asyncio.run(app.move_company_mailboxes())
    assert not cli.calls  # the old UIDs would answer for the new server's mails
    monkeypatch.setattr(app.shutil, "rmtree", real)
    asyncio.run(app.move_company_mailboxes())
    assert cli.calls and not app.index_dir("me").exists()


def test_nothing_in_the_move_keeps_the_app_from_starting(moving, monkeypatch):
    cli, _, _ = moving

    def broken():
        raise app.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(app.cases, "all_open", broken)
    monkeypatch.setattr(app.config, "update", lambda path, value: (_ for _ in ()).throw(OSError("disk full")))
    asyncio.run(app.move_company_mailboxes())  # logged, retried at the next start
    assert not cli.calls  # the record of the move could not be written: nothing moved without it


def test_a_moved_mailbox_whose_password_alibaba_refuses_is_asked_for_it(moving):
    cli, _, _ = moving
    asyncio.run(app.move_company_mailboxes())
    cli.receiving = "Authentication or provider policy rejected the connection or sender"
    asyncio.run(app.check_password("me"))  # what a failed scan does
    assert [p["name"] for p in app.password_needed()] == ["me"]


def test_other_providers_are_not_asked(moving):
    cli, _, _ = moving
    asked = []
    real = cli.show

    async def show(name):
        asked.append(name)
        return await real(name)
    cli.show = show
    asyncio.run(app.move_company_mailboxes())
    assert asked == ["me"]  # me_163 is on example.org, not the company's domain


def test_nothing_moves_in_a_dev_copy(moving, monkeypatch):
    cli, saved, _ = moving
    monkeypatch.setattr(app, "DEV", True)
    asyncio.run(app.move_company_mailboxes())
    assert not cli.calls and not saved


def test_the_login_screen_s_servers_move_even_without_a_mailbox(moving, monkeypatch):
    cli, saved, _ = moving
    monkeypatch.setattr(app, "LOGGED_OUT", True)
    asyncio.run(app.move_company_mailboxes())
    assert saved[("mail", "company_servers")]["smtp_host"] == "smtp.us.alibabacloud.com" and not cli.calls
    assert app.company_preset()["imap_host"] == "imap.us.alibabacloud.com"


def test_a_stored_password_that_works_there_is_not_asked_again(moving):
    cli, _, _ = moving
    asyncio.run(app.move_company_mailboxes())
    cli.receiving = None
    asyncio.run(app.check_password("me"))
    assert app.password_needed() == []


# ---------------------------------------------------------------- after the move

def test_alibaba_mail_s_inbox_is_the_inbox():
    class Bridge:
        async def call(self, tool):
            return {"result": [{"name": "Inbox", "flags": [], "delimiter": "/"},
                               {"name": "Sent", "flags": ["\\Sent"], "delimiter": "/"},
                               {"name": "Draft", "flags": ["\\Drafts"], "delimiter": "/"},
                               {"name": "Trash", "flags": ["\\Trash"], "delimiter": "/"},
                               {"name": "Spam", "flags": ["\\Junk"], "delimiter": "/"}]}
    got = asyncio.run(app.MailBridge.folder_listing(Bridge()))
    assert got["names"][0] == "INBOX" and "Inbox" not in got["names"]
    assert got["roles"] == {"sent": "Sent", "drafts": "Draft", "trash": "Trash", "junk": "Spam"}


def test_before_the_folder_list_a_moved_mailbox_never_guesses_workmail_names(moving):
    app.folder_info.forget("me")
    try:
        with app.on_account("me"):
            assert app.sent_box() == "Sent Items"  # still on WorkMail
            asyncio.run(app.move_company_mailboxes())
            assert app.sent_box() == ""
    finally:
        app.folder_info.forget("me")


class FoldServer:
    """Alibaba Mail after 邮箱搬家, as the bg session sees it: move_emails takes at most 100 UIDs."""

    name = "bg"

    def __init__(self, mails):
        self.mails = {box: list(ids) for box, ids in mails.items()}
        self.calls = []

    async def folder_listing(self):
        names = TREE + [b for b in self.mails if b not in TREE]
        return {"names": names, "roles": ROLES, "special": ["Sent", "Spam", "Trash", "Draft"], "delimiter": "/"}

    async def call(self, tool, **args):
        self.calls.append((tool, args.get("mailbox") or args.get("source_mailbox"), args.get("destination_mailbox")))
        if tool == "create_mailbox":
            self.mails.setdefault(args["mailbox"], [])
            return {"status": "created"}
        if tool == "list_emails_metadata":
            size, page = args["page_size"], args.get("page", 1)
            ids = self.mails.get(args["mailbox"], [])[(page - 1) * size:page * size]
            return {"emails": [{"email_id": i, "message_id": f"<{i}@x>"} for i in ids]}
        if tool == "move_emails":
            assert len(args["email_ids"]) <= 100
            src = self.mails[args["source_mailbox"]]
            for i in args["email_ids"]:
                src.remove(int(i))
            self.mails.setdefault(args["destination_mailbox"], []).extend(int(i) for i in args["email_ids"])
            return {"result": f"Successfully moved {len(args['email_ids'])}"}
        raise AssertionError(tool)


def test_the_moved_mail_is_brought_up_and_the_tree_hidden(moving, monkeypatch):
    me = accounts.Account("eclair", "eclair@example.com", "", True)
    monkeypatch.setattr(app, "ACCOUNTS", [me])
    monkeypatch.setattr(app, "ACCOUNT", "eclair")
    monkeypatch.setitem(app.CFG["mail"], "server_move", {"at": "2026-11-02T04:09:14+00:00", "accounts": ["eclair"],
                                                         "pending": []})
    server = FoldServer({"eclair@example.com/INBOX": range(1, 137), "eclair@example.com/纯固薪": range(200, 211),
                         "eclair@example.com/Sent Items": range(300, 759)})
    monkeypatch.setattr(app, "bg", server)
    monkeypatch.setattr(app, "folders_changed", lambda new=None, old=None: None)
    monkeypatch.setattr(app, "FOLD_PAUSE", 0)
    monkeypatch.setattr(app, "FOLD_MAX", 10_000)
    app.folder_info.forget("eclair")
    try:
        with app.on_account("eclair"):
            asyncio.run(app.fold_moved_mail())
            assert len(server.mails["INBOX"]) == 136 and len(server.mails["Sent"]) == 459
            assert len(server.mails["纯固薪"]) == 11 and ("create_mailbox", "纯固薪", None) in server.calls
            assert not server.mails["eclair@example.com/INBOX"]  # moved, never deleted
            assert not any(c[0] == "create_mailbox" and c[1] in ("INBOX", "Sent") for c in server.calls)
            names = app.folder_info.get("names")
            assert "纯固薪" in names and not any(n.startswith("eclair@example.com") for n in names)
            assert app.deal_boxes() == app.DEAL_BOXES  # all three, the empty ones too: the triage files into them
            server.calls.clear()
            asyncio.run(app.fold_moved_mail())  # the next look: only reads
            assert not any(c[0] in ("move_emails", "create_mailbox") for c in server.calls)
            # a folder the server refuses to take mail into is skipped; the rest still go
            server.mails["eclair@example.com/Junk E-mail"] = [900]
            server.mails["eclair@example.com/纯CPS"] = [901]
            real = server.call

            async def refusing(tool, **args):
                if tool == "move_emails" and args["destination_mailbox"] == "Spam":
                    return {"result": "Move result [failed: 900]"}
                return await real(tool, **args)
            server.call = refusing
            asyncio.run(app.fold_moved_mail())
            assert server.mails["eclair@example.com/Junk E-mail"] == [900] and 901 in server.mails["纯CPS"]
            # a move cut off after the copy: the original is listed again, its copy is already there
            server.call = real
            server.mails["eclair@example.com/INBOX"] = [5, 999]
            asyncio.run(app.fold_moved_mail())
            assert server.mails["INBOX"].count(5) == 1 and 999 in server.mails["INBOX"]
            assert server.mails["eclair@example.com/INBOX"] == [5]  # left behind, hidden, never copied twice
    finally:
        app.folder_info.forget("eclair")


def test_a_big_old_mailbox_goes_over_several_looks(moving, monkeypatch):
    me = accounts.Account("eclair", "eclair@example.com", "", True)
    monkeypatch.setattr(app, "ACCOUNTS", [me])
    monkeypatch.setattr(app, "ACCOUNT", "eclair")
    monkeypatch.setitem(app.CFG["mail"], "server_move", {"at": "x", "accounts": ["eclair"]})
    server = FoldServer({"eclair@example.com/Sent Items": range(1, 1201)})
    monkeypatch.setattr(app, "bg", server)
    monkeypatch.setattr(app, "folders_changed", lambda new=None, old=None: None)
    monkeypatch.setattr(app, "FOLD_PAUSE", 0)
    app.folder_info.forget("eclair")
    try:
        with app.on_account("eclair"):
            asyncio.run(app.fold_moved_mail())
            assert len(server.mails["Sent"]) == app.FOLD_MAX  # the rest at the next looks
            asyncio.run(app.fold_moved_mail())
            asyncio.run(app.fold_moved_mail())
            assert len(server.mails["Sent"]) == 1200 and not server.mails["eclair@example.com/Sent Items"]
    finally:
        app.folder_info.forget("eclair")


def test_old_mail_brought_over_is_neither_announced_nor_logged(moving, monkeypatch):
    now = mailmove.CUTOVER + timedelta(days=5)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(app, "datetime", Clock)
    monkeypatch.setitem(app.CFG["mail"], "server_move", {"at": now.isoformat(), "accounts": ["me"]})
    old = {"message_id": "<old@x>", "sender": "Kol <kol@example.org>", "subject": "Collab", "email_id": "5",
           "date": (now - timedelta(days=90)).isoformat()}
    new = {"message_id": "<new@x>", "sender": "Kol <kol@example.org>", "subject": "Re: Collab", "email_id": "6",
           "date": (now - timedelta(hours=3)).isoformat()}
    announced, posted = [], []
    monkeypatch.setattr(app, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(app, "notify_seen", {"me": ["<seen@x>"]})
    monkeypatch.setattr(app, "notify_pending", set())
    monkeypatch.setattr(app, "save_notify_seen", lambda: None)
    monkeypatch.setattr(app, "spawn", lambda coro, what: coro.close())
    monkeypatch.setattr(app, "announce", lambda meta: (announced.append(meta["message_id"]), asyncio.sleep(0))[1])
    monkeypatch.setattr(app, "sheet_enabled", lambda: True)
    monkeypatch.setattr(app, "sheet_seen", {"<seen@x>": "posted"})
    monkeypatch.setattr(app, "save_sheet_seen", lambda: None)
    monkeypatch.setattr(app, "sheet_post", lambda op: posted.append(op["message_id"]))
    with app.on_account("me"):
        app.notify_new([new, old])
        app.sheet_note_incoming([new, old])
    assert announced == ["<new@x>"] and "<old@x>" in app.notify_seen["me"]
    assert posted == ["<new@x>"] and app.sheet_seen["<old@x>"] == "seed"
