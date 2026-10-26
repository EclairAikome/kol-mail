"""Several mailboxes: settings, server presets, the mcp-email-server command line, per-mailbox caches and folders,
which mailbox a request is about, and adding / logging out through the page."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import accounts  # noqa: E402
import app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PRIMARY = accounts.Account("me", "me@example.com", "", True)
OTHER = accounts.Account("me_163", "me@example.org", "个人", False)


# ---------------------------------------------------------------- accounts.py

def test_settings_round_trip_and_bad_entries_are_dropped():
    mail = {"account_name": "me", "address": "Me@Example.com",
            "extra_accounts": [{"name": "me_163", "address": "me@example.org", "label": "个人"},
                               {"name": "me", "address": "dup@x.com"}, {"name": "bad name", "address": "a@y.com"},
                               {"name": "noaddr", "address": "nope"}, "junk"]}
    got = accounts.from_settings(mail)
    assert [a.name for a in got] == ["me", "me_163"] and got[0].address == "me@example.com" and got[0].primary
    assert accounts.to_settings(got) == [{"name": "me_163", "address": "me@example.org", "label": "个人"}]
    assert got[1].short == "个人" and got[0].short == "me@example.com"


def test_account_names_are_safe_and_never_reused():
    import re
    first, again = accounts.name_for("li.na+kol@example.org", set()), accounts.name_for("li.na+kol@example.org", set())
    assert re.fullmatch(r"linakol_example_[0-9a-f]{4}", first) and accounts.NAME.match(first)
    assert first != again or accounts.name_for("li.na+kol@example.org", {first}) != first  # a removed name stays reserved
    assert accounts.NAME.match(accounts.name_for("a" * 60 + "@" + "b" * 60 + ".com", set()))


@pytest.mark.parametrize("address", ["%USERNAME%@x.com", "${KEY}@evil.com", "a*@x.com", "~bob@x.com",
                                     "--help@x.com", "a\0b@x.com", "a b@x.com", "a@-x.example.com"])
def test_addresses_the_command_line_could_expand_are_refused(address):
    assert not accounts.EMAIL.match(address)


def test_ordinary_addresses_pass():
    for address in ("li.na+kol@example.org", "a_b-c@mail.example.net", "x9@y.com"):
        assert accounts.EMAIL.match(address)


def test_servers_come_from_the_user_a_preset_or_the_company_mailbox():
    assert accounts.servers_for("a@163.com", {}, None)["imap_host"] == "imap.163.com"
    icloud = accounts.servers_for("a@icloud.com", {}, None)
    assert icloud["smtp_port"] == 587 and icloud["smtp_security"] == "starttls"
    company = {"domain": "example.com", "imap_host": "imap.example.com", "imap_port": 993, "imap_security": "ssl",
               "smtp_host": "smtp.example.com", "smtp_port": 465, "smtp_security": "ssl"}
    assert accounts.servers_for("b@example.com", {}, company)["smtp_host"] == "smtp.example.com"
    typed = accounts.servers_for("c@example.org", {"imap_host": "mail.example.org", "imap_port": "143",
                                               "imap_security": "starttls", "smtp_host": "mail.example.org",
                                               "smtp_security": "starttls"}, None)
    assert typed["imap_port"] == 143 and typed["smtp_port"] == 587
    with pytest.raises(accounts.AccountError, match="OAuth"):
        accounts.servers_for("x@outlook.com", {}, None)
    with pytest.raises(accounts.AccountError, match="服务器"):
        accounts.servers_for("x@example.org", {}, None)
    with pytest.raises(accounts.AccountError):
        accounts.servers_for("x@example.org", {"imap_host": "bad host; rm -rf", "smtp_host": "s.example.org"}, None)


def test_cli_reads_the_json_document_and_turns_error_codes_into_chinese():
    cli = accounts.Cli(sys.executable)
    ok = asyncio.run(cli.json("-c", 'print("warning"); print(\'{"ok": true, "data": {"accounts": []}}\')'))
    assert ok == {"accounts": []}
    script = ('import sys; print(\'{"ok": false, "error": {"code": "authentication_or_provider_rejected", '
              '"message": "x"}}\'); sys.exit(1)')
    with pytest.raises(accounts.AccountError, match="密码"):
        asyncio.run(cli.json("-c", script))
    code, out = asyncio.run(cli.run("-c", "import sys; print(len(sys.stdin.read().split()))", secrets=("p1", "p2")))
    assert code == 0 and out == "2"  # secrets reach stdin, one per line, never argv
    code, out = asyncio.run(cli.run("-c", "import sys; print('echo:' + sys.stdin.readline())", secrets=("hunter2",)))
    assert "hunter2" not in out and "***" in out  # whatever the component prints, the password never comes back
    try:
        asyncio.run(cli.json("-c", 'print(\'{"ok": false, "error": {"code": "account_name_exists"}}\')'))
    except accounts.AccountError as e:
        assert e.code == "account_name_exists"


def test_a_hanging_command_is_ended_with_its_children():
    cli = accounts.Cli(sys.executable)
    code, out = asyncio.run(cli.run("-c", "import time; time.sleep(30)", timeout=1))
    assert code == -1 and "没有响应" in out


# ---------------------------------------------------------------- per-mailbox state

@pytest.fixture
def two(monkeypatch):
    monkeypatch.setattr(app, "ACCOUNTS", [PRIMARY, OTHER])
    monkeypatch.setattr(app, "ACCOUNT", "me")
    yield
    for per in (app.lists, app.folder_info, app.bodies, app.unread, app.folder_index):
        per.forget("me_163")


def test_caches_and_self_address_follow_the_current_mailbox(two):
    with app.on_account("me_163"):
        app.lists[("INBOX", 1)] = (1.0, {"emails": ["163"]})
        assert app.self_address() == "me@example.org" and not app.is_primary()
        assert app.is_skippable({"sender": "Me <me@example.org>"})
    with app.on_account("me"):
        assert ("INBOX", 1) not in app.lists or app.lists[("INBOX", 1)][1] != {"emails": ["163"]}
        assert not app.is_skippable({"sender": "Me <me@example.org>", "subject": "hi"})
    app.lists.forget("me_163")
    with app.on_account("me_163"):
        assert ("INBOX", 1) not in app.lists


def test_special_folders_are_found_by_flag_or_name(two):
    with app.on_account("me_163"):
        assert app.deal_boxes() == () and app.index_boxes() == ("INBOX",)  # not known yet: nothing guessed
        app.folder_info["names"] = ["INBOX", "已发送", "草稿箱", "垃圾邮件", "已删除", "纯CPS"]
        app.folder_info["roles"] = {}
        assert app.sent_box() == "已发送" and app.folder_role("drafts") == "草稿箱"
        assert app.thread_boxes() == ("INBOX", "纯CPS") and app.search_boxes()[-1] == "垃圾邮件"
        app.folder_info["roles"] = {"sent": "Sent Messages"}
        assert app.sent_box() == "Sent Messages"
        app.folder_info["names"], app.folder_info["roles"] = ["INBOX", "Gesendet"], {}
        assert app.sent_box() == "" and app.index_boxes() == ("INBOX",) and app.search_boxes() == ("INBOX",)
        assert asyncio.run(app.bg.sent_to("x@y.com")) == []  # no folder to search: no doomed IMAP call
    with app.on_account("me"):
        app.folder_info.forget("me")
        assert app.sent_box() == "Sent Items" and app.deal_boxes() == app.DEAL_BOXES  # the company mailbox as before


def test_server_flags_are_matched_whatever_their_case(two):
    class Fake(app.MailBridge):
        async def call(self, tool, timeout=180, **args):
            return {"result": [{"name": "INBOX", "flags": []}, {"name": "Gesendet", "flags": ["\\SENT"]},
                               {"name": "[Gmail]", "flags": ["\\NoSelect"]}]}
    names, roles = asyncio.run(Fake("t").list_folders())
    assert names == ["INBOX", "Gesendet"] and roles == {"sent": "Gesendet"}


def test_another_mailbox_is_scanned_and_announced_but_not_translated(two, monkeypatch):
    seen = {"enqueued": [], "notified": 0}
    data = {"emails": [{"message_id": "<a@x>", "email_id": "1", "sender": "Ana <ana@example.com>", "subject": "Hi"}]}

    async def listing(*a, **k):
        return data
    monkeypatch.setattr(app.bg.others, "list_messages", listing)
    monkeypatch.setattr(app, "enqueue", lambda *a, **k: seen["enqueued"].append(a))
    monkeypatch.setattr(app, "notify_new", lambda emails: seen.__setitem__("notified", len(emails)))
    with app.on_account("me_163"):
        asyncio.run(app.scan_account())
    assert seen == {"enqueued": [], "notified": 1}


def test_the_background_session_is_split_by_mailbox(two):
    with app.on_account("me"):
        assert app.bg.name == "bg"
    with app.on_account("me_163"):
        assert app.bg.name == "bg-more"
    assert app.bg.others.params() is not None  # two mailboxes: the second session may start


def test_logging_out_deletes_translations_and_attachments(two, tmp_path, monkeypatch):
    db = app.Cache(tmp_path / "c.sqlite3")
    monkeypatch.setattr(app, "cache", db)
    monkeypatch.setattr(app, "ATTACH_DIR", tmp_path / "attachments")
    db.put("<p@x>", status="done", body_zh="主邮箱")
    db.put("<s@x>", status="done", body_zh="私人")
    with app.on_account("me_163"):
        db.tag("me_163", "<s@x>")
        folder = app.attach_root() / "abc"
        folder.mkdir(parents=True)
        (folder / "file.pdf").write_bytes(b"x")
    assert app.attach_root("me") == tmp_path / "attachments"
    app.forget_account("me_163")
    assert db.get("<s@x>") is None and db.get("<p@x>")["body_zh"] == "主邮箱"
    assert not (tmp_path / "attachments" / "account-me_163").exists()


def test_translation_jobs_remember_their_mailbox(two, monkeypatch):
    monkeypatch.setattr(app, "jobs", app.collections.deque())
    monkeypatch.setattr(app, "queued", set())
    monkeypatch.setattr(app.cache, "put", lambda *a, **k: None)
    with app.on_account("me_163"):
        app.enqueue("<m@x>", "INBOX", "5")
    assert app.jobs[0] == ("<m@x>", "INBOX", "5", "me_163")


def test_each_mailbox_keeps_its_own_announced_list(two, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "NOTIFY_SEEN", tmp_path / "notify_seen.json")
    assert app.notify_seen_file("me").name == "notify_seen.json"
    assert app.notify_seen_file("me_163").name == "notify_seen-me_163.json"
    with app.on_account("me_163"):
        assert app.mail_link("INBOX", "7").endswith("#open=INBOX/7/me_163")
        assert app.mail_link("INBOX", None).endswith("/#account=me_163")  # a summary of several mails
        assert app.to_whom() == "（个人）"
    with app.on_account("me"):
        assert app.mail_link("INBOX", "7").endswith("#open=INBOX/7")
        assert app.mail_link("INBOX", None) == f"http://127.0.0.1:{app.PORT}/"


def run_guard(path: str, account: str):
    seen, sent = [], []

    async def inner(scope, receive, send):
        seen.append(app.current_account.get())

    async def send(message):
        sent.append(message)

    headers = [(b"host", f"127.0.0.1:{app.PORT}".encode()), (b"x-kol-mail", b"1"), (b"x-kol-account", account.encode())]
    scope = {"type": "http", "path": path, "method": "GET", "headers": headers}
    asyncio.run(app.guard(scope, None, send, inner))
    return seen[0] if seen else sent[0]["status"]


def test_only_mail_reading_follows_the_page_s_mailbox(two):
    assert run_guard("/api/messages", "me_163") == "me_163"
    assert run_guard("/api/thread", "me_163") == "me_163"
    assert run_guard("/api/messages", "") == "me"
    assert run_guard("/api/mobile/case", "me_163") == "me"  # the phone and the automations: always the primary
    assert run_guard("/api/sheet", "me_163") == "me"


def test_a_page_still_showing_a_logged_out_mailbox_is_told_to_reload(two):
    assert run_guard("/api/thread", "gone_1234") == 409  # its UIDs would open someone else's mail
    assert run_guard("/api/status", "gone_1234") == "me"  # status still answers, so the page can find its way back


# ---------------------------------------------------------------- adding and logging out through the page

class FakeCli:
    """mcp-email-server as it behaves: a removed account's name stays reserved and is not listed."""

    def __init__(self, receiving=None, sending=None, cleanup_ok=True):
        self.calls, self.receiving, self.sending, self.cleanup_ok = [], receiving, sending, cleanup_ok
        self.reserved = {"me"}

    async def names(self):
        return {"me"}

    async def show(self, name):
        return {"revision": 3, "incoming": {"host": "imap.example.com", "port": 993, "start_ssl": False},
                "outgoing": {"host": "smtp.example.com", "port": 465, "start_ssl": False}}

    async def add(self, name, address, full_name, servers, password):
        self.calls.append(("add", name, address, servers["imap_host"], password))
        if name in self.reserved:
            raise accounts.AccountError("exists", "account_name_exists")
        self.reserved.add(name)

    async def test(self, name, role):
        return self.receiving if role == "incoming" else self.sending

    async def remove(self, name):
        self.calls.append(("remove", name))
        return self.cleanup_ok


@pytest.fixture
def client(monkeypatch):
    saved = {}
    monkeypatch.setattr(app, "ACCOUNTS", [PRIMARY])
    monkeypatch.setattr(app, "ACCOUNT", "me")
    monkeypatch.setattr(app, "restart_pending", False)
    monkeypatch.setattr(app.config, "update", lambda path, value: saved.__setitem__(tuple(path), value))
    monkeypatch.setattr(app.config, "update_many", lambda changes: [saved.__setitem__(tuple(p), v) for p, v in changes])

    async def no_reload():
        return None
    monkeypatch.setattr(app, "reload_mail_sessions", no_reload)
    return TestClient(app.starlette_app), saved


def test_adding_a_mailbox_tests_it_and_keeps_the_password_out_of_settings(client, monkeypatch):
    c, saved = client
    cli = FakeCli(sending="smtp refused")
    monkeypatch.setattr(app, "mail_cli", cli)
    d = c.post("/api/accounts/add", json={"email": "Li@163.com", "password": "auth-code", "label": "个人"}).json()
    (_, name, address, host, password), = cli.calls
    assert name.startswith("li_163_") and (address, host, password) == ("li@163.com", "imap.163.com", "auth-code")
    assert [a["name"] for a in d["accounts"]] == ["me", name] and "发信" in d["warning"]
    assert saved[("mail", "extra_accounts")] == [{"name": name, "address": "li@163.com", "label": "个人"}]
    assert "auth-code" not in repr(saved)
    assert c.post("/api/accounts/add", json={"email": "li@163.com", "password": "x"}).status_code == 400  # twice


def test_the_same_address_can_be_added_again_after_a_wrong_password(client, monkeypatch):
    c, _ = client
    cli = FakeCli(receiving="Authentication or provider policy rejected")
    monkeypatch.setattr(app, "mail_cli", cli)
    assert c.post("/api/accounts/add", json={"email": "li@qq.com", "password": "wrong"}).status_code == 400
    cli.receiving = None
    assert c.post("/api/accounts/add", json={"email": "li@qq.com", "password": "right"}).status_code == 200
    first, second = cli.calls[0][1], cli.calls[-1][1]
    assert first != second  # the first name stays reserved after its removal


def test_a_reserved_name_is_retried(client, monkeypatch):
    c, _ = client
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    cli.reserved.add("li_163_aaaa")  # used once before, removed since
    names = iter(["li_163_aaaa", "li_163_bbbb"])
    monkeypatch.setattr(app.accounts, "name_for", lambda address, taken: next(names))
    assert c.post("/api/accounts/add", json={"email": "li@163.com", "password": "pw"}).status_code == 200
    assert [call[1] for call in cli.calls] == ["li_163_aaaa", "li_163_bbbb"]
    cli.reserved.update({"x_1", "x_2", "x_3"})  # never more than three tries
    names = iter(["x_1", "x_2", "x_3"])
    assert c.post("/api/accounts/add", json={"email": "li2@163.com", "password": "pw"}).status_code == 400


def test_a_wrong_password_leaves_nothing_behind(client, monkeypatch):
    c, saved = client
    cli = FakeCli(receiving="Authentication or provider policy rejected")
    monkeypatch.setattr(app, "mail_cli", cli)
    r = c.post("/api/accounts/add", json={"email": "li@qq.com", "password": "wrong"})
    assert r.status_code == 400 and "授权码" in r.json()["error"]
    assert cli.calls[-1][0] == "remove" and not saved and len(app.ACCOUNTS) == 1


def test_a_failed_rollback_is_reported(client, monkeypatch):
    c, _ = client
    cli = FakeCli(receiving="Authentication or provider policy rejected")

    async def broken(name):
        raise accounts.AccountError("credential store busy")
    cli.remove = broken
    monkeypatch.setattr(app, "mail_cli", cli)
    assert "维护者" in c.post("/api/accounts/add", json={"email": "li@qq.com", "password": "x"}).json()["error"]


def test_nothing_changes_in_dev_mode_or_while_restarting(client, monkeypatch):
    c, _ = client
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    monkeypatch.setattr(app, "DEV", True)
    assert "开发模式" in c.post("/api/accounts/add", json={"email": "li@qq.com", "password": "x"}).json()["error"]
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app, "restart_pending", True)
    assert "重启" in c.post("/api/accounts/add", json={"email": "li@qq.com", "password": "x"}).json()["error"]
    assert not cli.calls


def test_a_second_company_address_uses_the_company_servers(client, monkeypatch):
    c, _ = client
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    assert c.post("/api/accounts/add", json={"email": "team@example.com", "password": "pw"}).status_code == 200
    assert cli.calls[0][3] == "imap.example.com"


@pytest.mark.parametrize("body", [{"email": "not-an-address", "password": "x"}, {"email": "a@163.com", "password": ""},
                                  {"email": "a@163.com", "password": "a\nb"}])
def test_bad_input_is_refused_before_anything_runs(client, monkeypatch, body):
    c, _ = client
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    assert c.post("/api/accounts/add", json=body).status_code == 400 and not cli.calls


def test_logging_out_forgets_the_mailbox_and_the_primary_one_needs_a_successor(client, monkeypatch):
    c, saved = client
    monkeypatch.setattr(app, "ACCOUNTS", [PRIMARY, OTHER])
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    with app.on_account("me_163"):
        app.bodies[("INBOX", "1")] = {"body": "x"}
    monkeypatch.setattr(app, "forget_account", lambda name: app.bodies.forget(name))
    assert c.post("/api/accounts/remove", json={"name": "me"}).status_code == 400
    d = c.post("/api/accounts/remove", json={"name": "me_163"}).json()
    assert [a["name"] for a in d["accounts"]] == ["me"] and cli.calls == [("remove", "me_163")]
    assert saved[("mail", "extra_accounts")] == [] and d["warning"] is None
    with app.on_account("me_163"):
        assert ("INBOX", "1") not in app.bodies


def test_making_another_mailbox_primary_puts_the_old_one_s_state_aside(client, monkeypatch, tmp_path):
    c, saved = client
    monkeypatch.setattr(app, "ACCOUNTS", [PRIMARY, OTHER])
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "NOTIFY_SEEN", tmp_path / "notify_seen.json")
    monkeypatch.setattr(app, "SHEET_SEEN", tmp_path / "sheet_seen.json")
    (tmp_path / "notify_seen.json").write_text('["old"]', encoding="utf-8")
    (tmp_path / "notify_seen-me_163.json").write_text('["new"]', encoding="utf-8")
    (tmp_path / "sheet_seen.json").write_text('["old"]', encoding="utf-8")
    closed = []
    monkeypatch.setattr(app.cases, "all_open", lambda: [{"id": 7}])
    monkeypatch.setattr(app.cases, "move", lambda case_id, old, new: closed.append((case_id, new)))
    restarts = []

    async def fake_restart(why=""):
        restarts.append(why)
    monkeypatch.setattr(app, "restart_when_idle", fake_restart)
    assert c.post("/api/accounts/primary", json={"name": "me_163"}).json() == {"restarting": True}
    assert (tmp_path / "notify_seen.json").read_text(encoding="utf-8") == '["new"]'
    assert (tmp_path / "notify_seen-me.json").read_text(encoding="utf-8") == '["old"]'
    assert not (tmp_path / "sheet_seen.json").exists() and (tmp_path / "sheet_seen-me.json").exists()
    assert closed == [(7, "handled")] and saved[("mail", "account_name")] == "me_163"
    assert saved[("mail", "extra_accounts")] == [{"name": "me", "address": "me@example.com", "label": ""}]
    assert app.restart_pending
    assert c.post("/api/accounts/label", json={"name": "me", "label": "x"}).status_code == 400  # until restarted


@pytest.fixture
def handover(client, monkeypatch, tmp_path):
    """Logging out of the primary mailbox: the files it hands over, the restart, nothing else touched."""
    c, saved = client
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "NOTIFY_SEEN", tmp_path / "notify_seen.json")
    monkeypatch.setattr(app, "SHEET_SEEN", tmp_path / "sheet_seen.json")
    (tmp_path / "notify_seen.json").write_text('["old"]', encoding="utf-8")
    (tmp_path / "notify_seen-me_163.json").write_text('["new"]', encoding="utf-8")
    monkeypatch.setattr(app.cases, "all_open", lambda: [])
    forgot, restarts = [], []
    monkeypatch.setattr(app, "forget_account", forgot.append)

    async def fake_restart(why=""):
        restarts.append(why)
    monkeypatch.setattr(app, "restart_when_idle", fake_restart)
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    return c, saved, cli, forgot, restarts


def test_logging_out_of_the_primary_hands_it_to_the_chosen_successor(handover, monkeypatch, tmp_path):
    c, saved, cli, forgot, restarts = handover
    third = accounts.Account("work2", "work2@example.org", "", False)
    monkeypatch.setattr(app, "ACCOUNTS", [PRIMARY, OTHER, third])
    assert c.post("/api/accounts/remove", json={"name": "me", "successor": "nobody"}).status_code == 400
    assert not cli.calls
    d = c.post("/api/accounts/remove", json={"name": "me", "successor": "me_163"}).json()
    assert d == {"restarting": True, "login": False, "warning": None}
    assert cli.calls == [("remove", "me")] and forgot == ["me"] and restarts and app.restart_pending
    assert saved[("mail", "account_name")] == "me_163" and saved[("mail", "address")] == "me@example.org"
    assert saved[("mail", "label")] == "个人"
    assert saved[("mail", "extra_accounts")] == [{"name": "work2", "address": "work2@example.org", "label": ""}]
    assert (tmp_path / "notify_seen.json").read_text(encoding="utf-8") == '["new"]'
    assert not (tmp_path / "notify_seen-me.json").exists()  # logged out: its record is not kept aside


def test_logging_out_of_the_last_mailbox_leads_to_the_login_screen(handover):
    c, saved, cli, forgot, restarts = handover
    d = c.post("/api/accounts/remove", json={"name": "me"}).json()
    assert d == {"restarting": True, "login": True, "warning": None} and cli.calls == [("remove", "me")]
    assert saved[("mail", "account_name")] == saved[("mail", "address")] == "" and saved[("mail", "extra_accounts")] == []
    assert restarts


def test_a_failed_logout_changes_nothing(handover):
    c, saved, cli, forgot, restarts = handover

    async def broken(name):
        raise accounts.AccountError("credential store busy")
    cli.remove = broken
    assert c.post("/api/accounts/remove", json={"name": "me"}).status_code == 400
    assert set(saved) <= {("mail", "company_servers")}  # remembered beforehand at most; the mailbox stays
    assert not forgot and not restarts and not app.restart_pending


@pytest.fixture
def logged_out(client, monkeypatch):
    """A fresh install: no mailbox in the settings yet; the installer told the company's servers."""
    c, saved = client
    monkeypatch.setattr(app, "ACCOUNTS", accounts.from_settings({}))
    monkeypatch.setattr(app, "ACCOUNT", "")
    monkeypatch.setattr(app, "LOGGED_OUT", True)
    monkeypatch.setitem(app.IDENTITY, "name", "")
    monkeypatch.setitem(app.CFG["mail"], "company_servers", {"domain": "@example.com", "imap_host": "imap.example.com",
                                                             "smtp_host": "smtp.example.com"})
    restarts = []

    async def fake_restart(why=""):
        restarts.append(why)
    monkeypatch.setattr(app, "restart_when_idle", fake_restart)
    cli = FakeCli()
    cli.reserved = set()
    monkeypatch.setattr(app, "mail_cli", cli)
    return c, saved, cli, restarts


def test_without_a_mailbox_the_page_gets_the_login_screen_and_nothing_else(logged_out, monkeypatch):
    c, _, _, _ = logged_out
    s = c.get("/api/status").json()
    assert s["login_needed"] and s["accounts"] == [] and s["login"]["domain"] == "example.com"
    assert run_guard("/api/messages", "") == 409 and run_guard("/api/sheet", "") == 409
    assert run_guard("/api/status", "") == "" and run_guard("/api/accounts/add", "") == ""
    assert run_guard("/api/pet", "") == "" and run_guard("/api/skin", "") == ""


def test_signing_in_makes_the_first_mailbox_the_primary_one(logged_out):
    c, saved, cli, restarts = logged_out
    body = {"email": "Alex.Li@example.com", "password": "pw", "label": "工作"}
    assert "署名" in c.post("/api/accounts/add", json=body).json()["error"] and not cli.calls  # who signs the mail?
    d = c.post("/api/accounts/add", json={**body, "full_name": "Alex Li"}).json()
    (_, name, address, host, _), = cli.calls
    assert d == {"restarting": True, "added": name, "warning": None} and restarts and app.restart_pending
    assert (address, host) == ("alex.li@example.com", "imap.example.com")  # the company's servers, from the installer
    assert saved[("mail", "account_name")] == name and saved[("mail", "address")] == "alex.li@example.com"
    assert saved[("mail", "label")] == "工作" and saved[("identity", "name")] == "Alex Li"
    assert ("mail", "extra_accounts") not in saved


def test_status_and_pet_count_every_mailbox(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(app, "ACCOUNTS", [PRIMARY, OTHER])
    monkeypatch.setattr(app, "unread_counts", lambda: {"INBOX": 2 if app.current_account.get() == "me" else 3})
    s = c.get("/api/status").json()
    assert [a["unread"] for a in s["accounts"]] == [2, 3] and s["account"] == "me"
    assert c.get("/api/pet").json()["unread"] == 5
