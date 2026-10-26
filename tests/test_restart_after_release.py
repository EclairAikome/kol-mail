"""After a release, publish.py restarts the service running from the dev checkout, which never updates itself."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from tools import publish  # noqa: E402

PAGE = {"x-kol-mail": "1", "origin": "http://127.0.0.1:8765"}


@pytest.fixture
def client(monkeypatch):
    spawned = []
    monkeypatch.setattr(app, "spawn", lambda coro, what: (spawned.append(what), coro.close()))
    monkeypatch.setitem(app.state, "restart_scheduled", False)
    c = TestClient(app.app, base_url="http://127.0.0.1:8765")
    c.spawned = spawned
    return c


def test_dev_copy_schedules_one_idle_restart(client, monkeypatch):
    monkeypatch.setattr(app.updater, "is_dev_copy", lambda base: True)
    monkeypatch.setattr(app, "DEV", False)
    r = client.post("/api/restart", json={}, headers=PAGE)
    assert r.status_code == 200 and r.json()["ok"] is True
    client.post("/api/restart", json={}, headers=PAGE)
    assert client.spawned == ["restart after release"]


def test_installed_copy_and_test_copy_refuse(client, monkeypatch):
    monkeypatch.setattr(app.updater, "is_dev_copy", lambda base: False)
    monkeypatch.setattr(app, "DEV", False)
    assert client.post("/api/restart", json={}, headers=PAGE).status_code == 409
    monkeypatch.setattr(app.updater, "is_dev_copy", lambda base: True)
    monkeypatch.setattr(app, "DEV", True)
    assert client.post("/api/restart", json={}, headers=PAGE).status_code == 409
    assert client.spawned == []


def test_restart_needs_the_page_headers(client, monkeypatch):
    monkeypatch.setattr(app.updater, "is_dev_copy", lambda base: True)
    monkeypatch.setattr(app, "DEV", False)
    assert client.post("/api/restart", json={}).status_code == 403
    assert client.post("/api/restart", json={}, headers={**PAGE, "origin": "http://evil.io"}).status_code == 403
    assert client.get("/api/restart", headers=PAGE).status_code == 405
    assert client.spawned == []


def test_a_mail_send_under_way_counts_as_busy(monkeypatch):
    seen = []

    async def call(self, tool, **kw):
        seen.append(app.busy_sending())
        return {"result": "Email sent successfully"}
    monkeypatch.setattr(app.MailBridge, "call", call)
    monkeypatch.setattr(app.cases, "recent", lambda n: [])
    assert not app.busy_sending()
    asyncio.run(app.mail.send(recipients=["a@example.com"], subject="s", body="b"))
    assert seen == [True] and not app.busy_sending()


def test_restart_waits_for_the_send_then_exits(monkeypatch):
    busy = [True, True, False]
    slept, exited = [], []
    monkeypatch.setattr(app, "busy_sending", lambda: busy.pop(0))

    async def sleep(s):
        slept.append(s)
    monkeypatch.setattr(app.asyncio, "sleep", sleep)
    monkeypatch.setattr(app.os, "_exit", exited.append)
    asyncio.run(app.restart_when_idle("release"))
    assert slept == [5, 5, 2] and exited == [0] and busy == []


class FakeService:
    """The local service: `restarts_after` status polls after the restart request a new pid answers."""

    def __init__(self, restarts_after=2, running="0.3.4", restart_status=200, reachable=True, slow=0):
        self.restarts_after, self.running = restarts_after, running
        self.restart_status, self.reachable, self.slow = restart_status, reachable, slow
        self.pid, self.requested, self.calls = 100, False, []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url), request.headers.get("x-kol-mail"),
                           request.headers.get("origin")))
        if not self.reachable:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/api/restart":
            self.requested = True
            return httpx.Response(self.restart_status, json={"ok": True})
        if self.requested:
            if self.slow:  # the old process is busy: one poll times out before it exits
                self.slow -= 1
                raise httpx.ReadTimeout("slow", request=request)
            if self.restarts_after:
                self.restarts_after -= 1
                if self.restarts_after == 0:
                    self.pid = 200
                else:
                    raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"version": self.running if self.pid == 200 else "0.3.3", "pid": self.pid})


def run(monkeypatch, capsys, svc, version="0.3.4"):
    real = httpx.Client
    made = []

    def client(**kw):
        made.append(kw)
        return real(transport=httpx.MockTransport(svc.handle), **kw)
    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.delenv("KOL_MAIL_PORT", raising=False)
    publish.restart_local_service(version, wait=0.3, poll=0)
    assert made and made[0]["trust_env"] is False
    return capsys.readouterr().out


def test_waits_for_a_new_process(monkeypatch, capsys):
    svc = FakeService(restarts_after=3)
    out = run(monkeypatch, capsys, svc)
    assert "已重启，现在运行 v0.3.4" in out and "这个文件夹" not in out
    assert svc.calls[1][:2] == ("POST", "http://127.0.0.1:8765/api/restart")
    assert all(c[2:] == ("1", "http://127.0.0.1:8765") for c in svc.calls)


def test_a_timeout_from_the_old_process_is_not_a_restart(monkeypatch, capsys):
    svc = FakeService(restarts_after=0, slow=1)  # times out once, then the old pid answers again
    out = run(monkeypatch, capsys, svc)
    assert "已重启" not in out and "还没重启完" in out


def test_reports_a_folder_that_holds_other_code(monkeypatch, capsys):
    out = run(monkeypatch, capsys, FakeService(restarts_after=1, running="0.3.3"))
    assert "现在运行 v0.3.3（这个文件夹里的代码是 v0.3.3，发布的是 v0.3.4）" in out


def test_no_service_running_is_fine(monkeypatch, capsys):
    svc = FakeService(reachable=False)
    out = run(monkeypatch, capsys, svc)
    assert "没有在运行" in out and [c[0] for c in svc.calls] == ["GET"]


def test_refused_restart_says_so(monkeypatch, capsys):
    out = run(monkeypatch, capsys, FakeService(restart_status=409))
    assert "没有重启" in out and "手动重启" in out


def test_the_scan_knows_an_api_key_from_a_word_ending_in_sk(tmp_path, monkeypatch):
    """A rule id like task-center-not-for-joining once stopped a release as an "sk-" API key."""
    monkeypatch.setattr(publish, "ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "publish-denylist.txt").write_text("", encoding="utf-8")
    (tmp_path / "rules.yaml").write_text("- id: task-center-not-for-joining\n", encoding="utf-8")
    (tmp_path / "keys.py").write_text('KEY = "sk-' + "a1B2c3D4e5F6g7H8i9J0k" + '"\nauth = "Bearer sk-' + "z" * 24 + '"\n',
                                      encoding="utf-8")
    hits = publish.scan(tmp_path, ["rules.yaml", "keys.py"])
    assert [h.split(":")[:2] for h in hits] == [["keys.py", "1"], ["keys.py", "2"]]


def test_only_a_pushed_release_restarts_and_a_failure_never_fails_it():
    src = (app.BASE / "tools" / "publish.py").read_text(encoding="utf-8")
    tail = src[src.index('print(f"导出完成'):]
    assert "if a.push:" in tail and "restart_local_service(version)" in tail and "except Exception" in tail
