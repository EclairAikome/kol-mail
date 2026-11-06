"""Alibaba Mail (the company mailboxes moved there on 2026-11-05) answers an IMAP SEARCH by address (TO, FROM) with
BAD "invalid command or parameters", which mcp-email-server reports as provider_failure, and HEADER TO finds nothing.
An unfiltered list of a folder works. So the searches for our earlier mails to someone and theirs to us fall back to
the folder's newest mails, filtered here; the phone's "did you answer it on the PC already" check went through
sent_to and failed every send from the phone (#177, 2026-11-06)."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

REFUSED = "Error executing tool list_emails_metadata: provider_failure: metadata provider request failed"
KOL = "kol@example.com"
SENT = [{"email_id": "9", "recipients": ["other@example.com"], "sender": "me@example.com", "date": "2026-11-05T10:00"},
        {"email_id": "8", "recipients": [KOL], "sender": "me@example.com", "date": "2026-11-05T09:00",
         "message_id": "<8@x>"},
        {"email_id": "7", "recipients": ["x@example.com", KOL], "sender": "me@example.com", "date": "2026-11-04T09:00"}]
INBOX = [{"email_id": "5", "sender": f"Kol <{KOL}>", "recipients": ["me@example.com"], "date": "2026-11-05T08:00",
          "message_id": "<5@x>"},
         {"email_id": "4", "sender": "someone@example.org", "recipients": ["me@example.com"], "date": "2026-11-05T07:00"}]


class Alibaba(app.MailBridge):
    """mcp-email-server over Alibaba Mail: any address filter is refused, an unfiltered list works."""

    def __init__(self):
        super().__init__("t")
        self.calls = []

    async def call(self, tool, timeout=180, raw=False, **args):
        self.calls.append((tool, {k: v for k, v in args.items() if k in ("to_address", "from_address", "page")}))
        if tool == "list_emails_metadata":
            if args.get("to_address") or args.get("from_address"):
                raise app.MailError(REFUSED)
            emails = SENT if args.get("mailbox") == app.sent_box() else INBOX if args.get("mailbox") == "INBOX" else []
            return {"emails": emails if args.get("page", 1) == 1 else []}
        return {"emails": [{"email_id": i, "body": f"mail {i}"} for i in args.get("email_ids") or []]}


@pytest.fixture(autouse=True)
def alibaba(monkeypatch):
    async def sleep(s):
        pass
    monkeypatch.setattr(app.asyncio, "sleep", sleep)
    monkeypatch.setattr(app, "sent_box", lambda: "已发送")
    monkeypatch.setattr(app, "thread_boxes", lambda: ("INBOX",))
    app.MailBridge.no_address_search.clear()


def test_our_mails_to_someone_are_found_without_a_search_by_address():
    bridge = Alibaba()
    got = asyncio.run(bridge.sent_to(KOL))
    assert [e["body"] for e in got] == ["mail 8", "mail 7"]
    # the second time the refused search is not tried again
    bridge.calls.clear()
    asyncio.run(bridge.sent_to(KOL))
    assert not any(args.get("to_address") for _, args in bridge.calls)


def test_their_mails_to_us_are_found_without_a_search_by_address():
    got = asyncio.run(Alibaba().received_from(KOL, exclude_message_id=None))
    assert [(e["body"], e["mailbox"]) for e in got] == [("mail 5", "INBOX")]


def test_a_reply_from_the_phone_goes_out(monkeypatch):
    """#177: 没有发出，可以再点发送：Error executing tool list_emails_metadata: provider_failure."""
    case = {"id": 177, "channel": "email", "peer": "new@example.com", "ref": "<in@x>", "mailbox": "INBOX",
            "email_id": "12", "body": "Hi!\n\nMe", "zh": "你好", "pairs_ok": 1, "created_at": "2026-11-06T00:30:00+08:00",
            "language": "English", "who": "New", "strategies": None, "chosen": None}

    class Mail:
        def __init__(self):
            self.sent = []

        async def get_message(self, mailbox, email_id):
            return {"message_id": "<in@x>", "sender": "New <new@example.com>", "subject": "Hello",
                    "date": "2026-11-06T00:20:00+08:00", "body": "Hi there"}

        async def send(self, **kwargs):
            self.sent.append(kwargs)
            return "Email sent successfully to them. Message-Id: <out@x>"
    mail = Mail()
    monkeypatch.setattr(app, "mail", mail)
    monkeypatch.setattr(app, "bg", Alibaba())
    for name in ("sent_changed", "sheet_guard", "record_autoreply", "record_case_episode"):
        monkeypatch.setattr(app, name, lambda *a, **k: None)
    status, note = asyncio.run(app.send_case(case))
    assert status == "sent" and len(mail.sent) == 1, note


def test_a_folder_that_cannot_even_be_listed_is_still_reported():
    class Down(Alibaba):
        async def call(self, tool, timeout=180, raw=False, **args):
            raise app.MailError(REFUSED)
    with pytest.raises(app.MailError, match="provider_failure"):
        asyncio.run(Down().sent_to(KOL))
    assert not app.MailBridge.no_address_search  # nothing learned from a server that is simply down
