"""The reply box saved as it is typed (app.Cache reply_drafts and /api/reply/save|saved|discard): per mailbox and per
KOL, an empty box drops the save, logging a mailbox out forgets its drafts, and a sent reply takes its draft along."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

KOL = "ana@example.org"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "cache", app.Cache(tmp_path / "cache.sqlite3"))
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    return TestClient(app.app, base_url=f"http://127.0.0.1:{app.PORT}")


def local(**extra):
    return {"X-KOL-Mail": "1", "Origin": f"http://127.0.0.1:{app.PORT}", **extra}


def test_a_typed_reply_comes_back_and_an_emptied_box_drops_it(client):
    r = client.post("/api/reply/save", headers=local(),
                    json={"address": "Ana@Example.org", "draft": {"notes": "问她报价", "body": "Hi Ana", "extra": "x"}})
    assert r.json()["saved_at"]
    d = client.get(f"/api/reply/saved?address={KOL}", headers=local()).json()["draft"]
    assert d["notes"] == "问她报价" and d["body"] == "Hi Ana" and "extra" not in d and d["saved_at"]
    client.post("/api/reply/save", headers=local(), json={"address": KOL, "draft": {"notes": " ", "body": ""}})
    assert client.get(f"/api/reply/saved?address={KOL}", headers=local()).json()["draft"] == {}


def test_drafts_are_per_mailbox_and_go_with_a_logout(client):
    client.post("/api/reply/save", headers=local(), json={"address": KOL, "draft": {"body": "primary"}})
    app.cache.save_draft("other", KOL, {"body": "second mailbox"})
    assert app.cache.draft(app.ACCOUNT, KOL)["body"] == "primary"
    app.cache.forget_account("other")
    assert app.cache.draft("other", KOL) is None and app.cache.draft(app.ACCOUNT, KOL)["body"] == "primary"


def test_discard_and_bad_addresses(client):
    client.post("/api/reply/save", headers=local(), json={"address": KOL, "draft": {"body": "x"}})
    assert client.post("/api/reply/discard", headers=local(), json={"address": KOL}).json()["ok"]
    assert app.cache.draft(app.ACCOUNT, KOL) is None
    assert client.post("/api/reply/save", headers=local(), json={"address": "not an address", "draft": {}}).status_code == 400
    assert client.get("/api/reply/saved", headers=local()).status_code == 400


def test_a_huge_field_is_cut(client):
    client.post("/api/reply/save", headers=local(), json={"address": KOL, "draft": {"body": "x" * 60_000}})
    assert len(app.cache.draft(app.ACCOUNT, KOL)["body"]) == app.DRAFT_LIMIT


def test_a_send_that_may_have_gone_out_marks_the_draft_until_it_is_sent(client):
    client.post("/api/reply/save", headers=local(), json={"address": KOL, "draft": {"body": "Hi Ana"}})
    app.cache.draft_uncertain(app.ACCOUNT, KOL)
    client.post("/api/reply/save", headers=local(), json={"address": KOL, "draft": {"body": "Hi Ana, edited"}})
    d = client.get(f"/api/reply/saved?address={KOL}", headers=local()).json()["draft"]
    assert d["uncertain"] is True and d["body"] == "Hi Ana, edited"
    app.cache.draft_uncertain(app.ACCOUNT, "nobody@example.org")  # no draft: nothing to mark
    assert app.cache.draft(app.ACCOUNT, "nobody@example.org") is None


def test_drafts_nobody_came_back_to_are_forgotten(client, monkeypatch):
    app.cache.save_draft(app.ACCOUNT, "old@example.org", {"body": "x"})
    now = app.time.time()
    monkeypatch.setattr(app.time, "time", lambda: now + app.DRAFT_KEEP + 1)
    app.cache.save_draft(app.ACCOUNT, KOL, {"body": "y"})
    assert app.cache.draft(app.ACCOUNT, "old@example.org") is None and app.cache.draft(app.ACCOUNT, KOL)
