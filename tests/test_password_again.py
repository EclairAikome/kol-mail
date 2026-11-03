"""A mailbox whose stored password the mail server stopped taking (the company reset every password on 2026-11-02):
one login test after a failed scan, no more logins with the old password, a new one entered in the page."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import accounts  # noqa: E402
import app  # noqa: E402
import autoreport  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PRIMARY = accounts.Account("me", "me@example.com", "", True)
OTHER = accounts.Account("me_163", "me@example.org", "个人", False)
REFUSED = "Error: Authentication or provider policy rejected the connection or sender"


class FakeCli:
    def __init__(self, receiving=REFUSED, sending=None):
        self.receiving, self.sending, self.calls = receiving, sending, []

    async def test(self, name, role):
        self.calls.append(("test", name, role))
        return self.receiving if role == "incoming" else self.sending

    async def set_password(self, name, password):
        self.calls.append(("password", name, password))


@pytest.fixture
def mailboxes(monkeypatch):
    monkeypatch.setattr(app, "ACCOUNTS", [PRIMARY, OTHER])
    monkeypatch.setattr(app, "ACCOUNT", "me")
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app, "restart_pending", False)
    monkeypatch.setattr(app, "password_rejected", {})
    monkeypatch.setattr(app, "password_checked", {})
    spawned = []
    monkeypatch.setattr(app, "spawn", lambda coro, what: (spawned.append(what), coro.close()))
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    return cli, spawned


def test_a_refused_password_is_found_once_and_not_tried_again_for_a_while(mailboxes):
    cli, _ = mailboxes
    asyncio.run(app.check_password("me"))
    assert "me" in app.password_rejected and [p["name"] for p in app.password_needed()] == ["me"]
    asyncio.run(app.check_password("me"))
    assert len(cli.calls) == 1  # known already: no second login with the old password for a while


def test_a_refusal_that_passes_clears_itself(mailboxes):
    cli, spawned = mailboxes
    asyncio.run(app.check_password("me"))
    cli.receiving = None  # the provider took it after all (the component reports both the same way)
    app.password_checked["me"] -= app.PASSWORD_CHECK_GAP
    asyncio.run(app.check_password("me"))
    assert app.password_needed() == [] and "reconnect a mailbox whose password works again" in spawned


def test_an_outage_is_not_a_password(mailboxes):
    cli, _ = mailboxes
    cli.receiving = "Error: TLS or network connection failed; verify endpoint and TLS settings"
    asyncio.run(app.check_password("me"))
    assert app.password_needed() == []
    asyncio.run(app.check_password("me"))
    assert len(cli.calls) == 1  # tested again only after PASSWORD_CHECK_GAP
    cli.receiving = None
    app.password_checked["me"] -= app.PASSWORD_CHECK_GAP
    asyncio.run(app.check_password("me"))
    assert app.password_needed() == [] and len(cli.calls) == 2


def test_nothing_logs_in_with_a_refused_password(mailboxes):
    app.password_rejected["me"] = REFUSED

    class Bridge(app.MailBridge):
        def __init__(self):
            self.base_args = None

        async def _call_once(self, *a, **k):
            return {"ok": True}
    with app.on_account("me"):
        with pytest.raises(app.NotReady, match="新密码"):
            asyncio.run(Bridge().call("list_emails_metadata", mailbox="INBOX"))
    with app.on_account("me_163"):  # the other mailbox goes on as before
        assert asyncio.run(Bridge().call("list_emails_metadata", mailbox="INBOX")) == {"ok": True}
    telegram = Bridge()
    telegram.base_args = {}  # a bridge to another kind of server is never stopped
    with app.on_account("me"):
        assert asyncio.run(telegram.call("get_me")) == {"ok": True}


def test_a_failed_scan_tests_the_password_but_not_when_it_is_already_known(mailboxes, monkeypatch):
    _, spawned = mailboxes

    async def failing():
        raise app.MailError("Error executing tool list_emails_metadata: provider_failure: metadata provider request failed")
    monkeypatch.setattr(app, "scan_account", failing)
    monkeypatch.setitem(app.state, "last_scan", None)
    monkeypatch.setattr(app, "scan_now", None)  # restored afterwards; each round below gets its own event

    async def one_round():
        app.scan_now = asyncio.Event()
        app.state["last_scan"] = None
        task = asyncio.create_task(app.scan_loop())
        while app.state["last_scan"] is None:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(one_round())
    assert spawned.count("password check") == 2  # once for each mailbox that failed

    spawned.clear()

    async def refused():
        raise app.NotReady(app.PASSWORD_REJECTED)
    monkeypatch.setattr(app, "scan_account", refused)
    asyncio.run(one_round())
    assert "password check" not in spawned  # a session that is not ready says nothing about the password
    app.password_rejected["me"] = REFUSED
    asyncio.run(one_round())
    assert spawned.count("password check") == 1  # taken for refused: tested again now and then


def test_the_new_password_is_tested_and_kept_out_of_settings(mailboxes, monkeypatch):
    cli, spawned = mailboxes
    saved = {}
    monkeypatch.setattr(app.config, "update", lambda path, value: saved.__setitem__(tuple(path), value))
    app.password_rejected["me"] = REFUSED
    c = TestClient(app.starlette_app)
    r = c.post("/api/accounts/password", json={"name": "me", "password": "wrong"})
    assert r.status_code == 400 and "还是没接受" in r.json()["error"] and "me" in app.password_rejected
    cli.receiving, cli.sending = None, "smtp refused"
    d = c.post("/api/accounts/password", json={"name": "me", "password": "right"}).json()
    assert d["ok"] and "发信" in d["warning"] and app.password_needed() == []
    assert ("password", "me", "right") in cli.calls and not saved and "reconnect after a new password" in spawned
    assert c.post("/api/accounts/password", json={"name": "nobody", "password": "x"}).status_code == 404
    assert c.post("/api/accounts/password", json={"name": "me", "password": ""}).status_code == 400
    monkeypatch.setattr(app, "DEV", True)
    assert "开发模式" in c.post("/api/accounts/password", json={"name": "me", "password": "x"}).json()["error"]


def test_logging_out_forgets_a_refused_password(mailboxes, monkeypatch, tmp_path):
    app.password_rejected["me_163"] = REFUSED
    app.password_checked["me_163"] = 1.0
    monkeypatch.setattr(app, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setattr(app, "ATTACH_DIR", tmp_path / "attachments")
    monkeypatch.setattr(app.cache, "forget_account", lambda name: 0)
    app.forget_account("me_163")
    assert "me_163" not in app.password_rejected and "me_163" not in app.password_checked


def test_the_maintainer_s_report_says_what_to_do():
    assert "新密码" in autoreport.diagnose(app.PASSWORD_REJECTED, "收信检查")


def test_the_command_line_takes_the_password_on_stdin_only(monkeypatch):
    cli, calls = accounts.Cli("mcp-email-server"), []

    async def fake_json(*args, secrets=()):
        calls.append((args, secrets))
        return {}
    monkeypatch.setattr(cli, "json", fake_json)
    asyncio.run(cli.set_password("me", "s3cret"))
    assert [c[0] for c in calls] == [("account", "set-secret", "me", role, "--password-stdin")
                                     for role in ("incoming", "outgoing")]
    assert all(c[1] == ("s3cret",) for c in calls)
