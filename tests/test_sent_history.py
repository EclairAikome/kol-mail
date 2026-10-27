"""Our earlier mails to a KOL (MailBridge.sent_to), which every reply draft reads when the folder index has none."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

FLAKY = "Error executing tool list_emails_metadata: provider_failure: metadata provider request failed"
MINE = {"email_id": "7", "recipients": ["kol@example.com"]}


class Fake(app.MailBridge):
    def __init__(self, failures: list[str]):
        super().__init__("t")
        self.failures, self.calls = failures, []

    async def call(self, tool, timeout=180, raw=False, **args):
        self.calls.append(tool)
        if tool == "list_emails_metadata":
            if self.failures:
                raise app.MailError(self.failures.pop(0))
            return {"emails": [MINE]}
        return {"emails": [{**MINE, "body": "our offer"}]}


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    async def sleep(s):
        pass
    monkeypatch.setattr(app.asyncio, "sleep", sleep)


def test_one_server_hiccup_does_not_fail_the_draft():
    # Seen 2026-10-27 (Lynn, Mac): the server dropped one IMAP session and the whole draft failed with this error.
    bridge = Fake([FLAKY])
    got = asyncio.run(bridge.sent_to("kol@example.com"))
    assert [e["body"] for e in got] == ["our offer"]
    assert bridge.calls == ["list_emails_metadata", "list_emails_metadata", "get_emails_content"]


def test_a_server_that_keeps_failing_is_still_reported():
    bridge = Fake([FLAKY, FLAKY])
    with pytest.raises(app.MailError, match="provider_failure"):
        asyncio.run(bridge.sent_to("kol@example.com"))
    assert bridge.calls == ["list_emails_metadata", "list_emails_metadata"]


def test_other_errors_are_not_retried():
    bridge = Fake(["邮件后端连接中断，正在重连"])
    with pytest.raises(app.MailError, match="连接中断"):
        asyncio.run(bridge.sent_to("kol@example.com"))
    assert bridge.calls == ["list_emails_metadata"]
