"""The page refreshes the list it shows every minute (GET /api/messages?mode=revalidate). A mail server that fails one
of those refreshes must leave that list in place, not turn into an error on the page and in the feedback group."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

FLAKY = "Error executing tool list_emails_metadata: provider_failure: metadata provider request failed"
SHOWN = {"emails": [{"email_id": "7", "message_id": "<m7@example.com>", "subject": "Collab",
                     "sender": "kol@example.com", "recipients": ["me@example.com"], "date": "2026-10-27T20:00:00Z"}],
         "total": 1}


def get(mode: str):
    request = Request({"type": "http", "method": "GET", "path": "/api/messages", "headers": [],
                       "query_string": f"mailbox=INBOX&page=1&mode={mode}".encode()})
    return asyncio.run(app.api_messages(request))


@pytest.fixture(params=[app.MailError(FLAKY), app.NotReady("邮件后端连接中断，正在重连")], ids=["server", "reconnecting"])
def failing_server(monkeypatch, request):
    async def fetch_list(bridge, mailbox, page):
        raise request.param
    monkeypatch.setattr(app, "fetch_list", fetch_list)
    monkeypatch.setattr(app, "refresh_unread_if_stale", lambda box: None)
    return request.param


def test_a_failed_background_refresh_keeps_the_list_on_the_page(failing_server, monkeypatch):
    # Seen 2026-10-28 01:46 (Lynn, Mac): the page left open overnight, the server failed one refresh, and that
    # became 页面操作 GET /api/messages in the feedback group.
    monkeypatch.setitem(app.lists, ("INBOX", 1), (time.time() - 120, SHOWN))
    resp = get("revalidate")
    data = json.loads(resp.body)
    assert resp.status_code == 200 and [e["email_id"] for e in data["emails"]] == ["7"] and data["age"] >= 120


def test_a_refresh_the_user_asked_for_still_says_it_failed(failing_server, monkeypatch):
    monkeypatch.setitem(app.lists, ("INBOX", 1), (time.time() - 120, SHOWN))
    with pytest.raises(type(failing_server)):
        get("live")  # the 刷新 button: an old list shown as if it were new would hide the failure


def test_the_unread_search_beside_the_refresh_fails_quietly_too(monkeypatch):
    # the same outage must not come back as 后台任务 unread refresh
    reported = []
    monkeypatch.setattr(app.autoreport, "report", lambda *a, **k: reported.append(a))

    async def refresh_unread(bridge, box):
        raise app.MailError(FLAKY)
    monkeypatch.setattr(app, "refresh_unread", refresh_unread)
    asyncio.run(app.refresh_unread_quietly("INBOX"))
    assert reported == []


def test_with_no_list_to_keep_the_failure_shows(failing_server, monkeypatch):
    if ("INBOX", 1) in app.lists:
        monkeypatch.delitem(app.lists, ("INBOX", 1))
    with pytest.raises(type(failing_server)):
        get("revalidate")
