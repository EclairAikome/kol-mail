"""Any message of a conversation can be answered, to whoever the owner puts in To, Cc and Bcc. A conversation follows
everyone on it: the KOL, an agency answering for them, a second address on Cc and our replies to several people."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import compliance  # noqa: E402
import replyflow  # noqa: E402

ME = "me@example.com"
SUBJECT = "Re: Collab with Example | Kai"


def mail(uid, box, sender, recipients, subject=SUBJECT, day=20):
    return (box, {"email_id": str(uid), "message_id": f"<{uid}@x>", "subject": subject, "sender": sender,
                  "recipients": recipients, "date": f"2026-10-{day}T10:00:00Z"})


KOL = mail(1, "INBOX", "Kai <kai@example.org>", [ME, "rob@agency.example.com"], day=20)          # Kai copies his agency
AGENCY = mail(2, "INBOX", "Rob <rob@agency.example.com>", [ME, "kai@example.org"], day=21)        # the agency answers
OURS = mail(3, "Sent Items", f"Me <{ME}>", ["rob@agency.example.com", "kai@example.org"], day=22)  # we answer both
STRANGER = mail(4, "INBOX", "Ann <ann@other.example.org>", [ME], day=21)                        # same subject, nobody shared
ELSEWHERE = mail(5, "INBOX", "Rob <rob@agency.example.com>", [ME], subject="Another creator", day=22)


@pytest.fixture
def indexed(monkeypatch):
    entries = [KOL, AGENCY, OURS, STRANGER, ELSEWHERE]

    async def fake_entries():
        return entries
    monkeypatch.setattr(app, "indexed_entries", fake_entries)
    return entries


def ids(members):
    return sorted(m["email_id"] for _, m in members)


def test_the_agency_and_our_reply_to_both_are_one_conversation(indexed):
    members = asyncio.run(app.conversation_members("kai@example.org", app.norm_subject(SUBJECT)))
    assert ids(members) == ["1", "2", "3"]


TEMPLATE = "Re: Collab with Example"  # one subject for every creator: the outreach was a template


def test_an_agency_with_two_creators_keeps_two_conversations(monkeypatch):
    """Seen in review: Alice and Bob both copy their manager under the same templated subject."""
    alice = mail(11, "INBOX", "Alice <alice@x.com>", [ME, "mgr@mcn.example.com"], subject=TEMPLATE)
    ours_alice = mail(12, "Sent Items", f"Me <{ME}>", ["alice@x.com", "mgr@mcn.example.com"], subject=TEMPLATE)
    bob = mail(13, "INBOX", "Bob <bob@y.com>", [ME, "mgr@mcn.example.com"], subject=TEMPLATE)
    ours_bob = mail(14, "Sent Items", f"Me <{ME}>", ["bob@y.com", "mgr@mcn.example.com"], subject=TEMPLATE)

    async def entries():
        return [alice, ours_alice, bob, ours_bob]

    async def bodies(members):  # no header ties Bob's mails to Alice's
        return {(box, m["email_id"]): {**m, "body": "x", "references": f"<outreach-{m['email_id']}@x>"}
                for box, m in members}
    monkeypatch.setattr(app, "indexed_entries", entries)
    monkeypatch.setattr(app, "thread_bodies", bodies)
    assert ids(asyncio.run(app.conversation_members("alice@x.com", app.norm_subject(TEMPLATE)))) == ["11", "12"]


def test_an_agency_answering_without_copying_the_kol_is_tied_in_by_the_headers(monkeypatch):
    outreach = mail(21, "Sent Items", f"Me <{ME}>", ["kai@example.org", "kai@agency.example.com"])
    agency = mail(22, "INBOX", "Rob <rob@agency.example.com>", [ME, "kai@agency.example.com"])  # answers the outreach
    other = mail(23, "INBOX", "Rob <rob@agency.example.com>", [ME, "kai@agency.example.com"])  # about something else

    async def entries():
        return [outreach, agency, other]

    async def bodies(members):
        refs = {"22": "<21@x>", "23": "<99@x>"}
        return {(box, m["email_id"]): {**m, "body": "x", "references": refs.get(m["email_id"])} for box, m in members}
    monkeypatch.setattr(app, "indexed_entries", entries)
    monkeypatch.setattr(app, "thread_bodies", bodies)
    assert ids(asyncio.run(app.conversation_members("kai@example.org", app.norm_subject(SUBJECT)))) == ["21", "22"]
    # opened from the agency's side, our outreach to the KOL is tied in the same way
    assert ids(asyncio.run(app.conversation_members("rob@agency.example.com", app.norm_subject(SUBJECT)))) == ["21", "22", "23"]


def test_opening_the_agencys_mail_shows_the_kols_mails_too(indexed):
    box, opened = AGENCY
    candidates = asyncio.run(app.thread_candidates(opened))
    others = app.thread_others(box, "2", app.message_identity(box, opened), app.norm_subject(SUBJECT), candidates)
    assert ids(others) == ["1", "3"]


def test_our_mail_to_several_people_has_a_conversation_and_a_main_address(indexed):
    box, ours = OURS
    assert app.counterpart(ours) == ""  # one recipient per conversation was the old rule: this mail had none
    assert app.main_address(ours) == "rob@agency.example.com"
    candidates = asyncio.run(app.thread_candidates(ours))
    assert ids(app.thread_others(box, "3", app.message_identity(box, ours), app.norm_subject(SUBJECT),
                                 candidates)) == ["1", "2"]


def test_the_draft_history_reads_the_whole_conversation(indexed, monkeypatch):
    async def bodies(members):
        return {(box, m["email_id"]): {**m, "body": f"body {m['email_id']}"} for box, m in members}
    monkeypatch.setattr(app, "thread_bodies", bodies)
    ours, earlier = asyncio.run(app.indexed_history("rob@agency.example.com", "<2@x>", subject=app.norm_subject(SUBJECT)))
    assert [m["email_id"] for m in ours] == ["3"]       # our mail to both, though it was not one-to-one
    assert sorted(m["email_id"] for m in earlier) == ["1", "5"]  # Kai's mail, and Rob's other one-to-one mail


# ---------------------------------------------------------------- the reply context and the prompt


def test_answering_our_own_mail_never_counts_it_as_the_kols_words():
    ours = {"body": "Can we pay you via PayPal?", "sender": f"Me <{ME}>", "recipients": ["kai@example.org"]}
    ctx = compliance.context_for({"msg": ours, "own_target": True, "earlier": [{"body": "Hi!"}], "ours": []})
    assert ctx.kol_texts == ["Hi!"] and ctx.our_texts == [ours["body"]]


def test_the_prompt_shows_what_is_answered_and_to_whom():
    ctx = {"msg": {"date": "d", "sender": f"Me <{ME}>", "recipients": ["kai@example.org"], "subject": "s", "body": "b"},
           "own_target": True, "earlier": [{"date": "d", "sender": "Rob <rob@agency.example.com>", "subject": "s", "body": "x"}],
           "ours": [], "reply_to": {"to": ["rob@agency.example.com"], "cc": ["kai@example.org"]}}
    text = "\n".join(replyflow.context_sections("b", ctx))
    assert "<<b:OUR_EMAIL_REPLIED_TO>>" in text and "<<b:KOL_EMAIL>>" not in text and "To: kai@example.org" in text
    assert "From: Rob <rob@agency.example.com>" in text  # whose earlier mail is whose
    assert "<<b:REPLY_RECIPIENTS>>\nTo: rob@agency.example.com\nCc: kai@example.org" in text


def test_the_draft_is_told_the_recipients_typed_on_the_page():
    ctx = app.with_recipients({"kol": "kai@example.org"}, {"to": "Rob <rob@agency.example.com>", "cc": f"kai@example.org, {ME}, x"})
    assert ctx["reply_to"] == {"to": ["rob@agency.example.com"], "cc": ["kai@example.org"]}
    assert "reply_to" not in app.with_recipients({"kol": "k"}, {"to": ""})


# ---------------------------------------------------------------- sending


class FakeMail:
    def __init__(self, msg):
        self.msg, self.sent = msg, []

    async def get_message(self, mailbox, email_id):
        return self.msg

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return "Email sent successfully to them. Message-Id: <new@example.com>"


class Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


TEXT = "Hi Rob,\n\nThanks!\n\nMe"


@pytest.fixture
def outbox(monkeypatch):
    box, msg = AGENCY
    fake = FakeMail({**msg, "references": "<1@x>", "body": "Hello"})
    filed, closed = [], []
    monkeypatch.setattr(app, "mail", fake)
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app, "sent_changed", lambda: None)
    monkeypatch.setattr(app, "close_cases_for", lambda channel, peer: closed.append(peer))
    monkeypatch.setattr(app, "sheet_guard", lambda what, fn, *args: filed.append(args))
    monkeypatch.setitem(app.checked_texts, app.text_hash(TEXT), time.time())
    return fake, filed, closed


def send(body):
    response = asyncio.run(app.api_send(Req({"mailbox": "INBOX", "email_id": "2", "subject": SUBJECT, "body": TEXT,
                                             "draft_key": "kai@example.org", **body})))
    return response.status_code, json.loads(response.body)


def test_a_reply_to_the_agency_copies_the_kol_and_stays_in_the_thread(outbox):
    fake, filed, closed = outbox
    status, out = send({"to": "rob@agency.example.com", "cc": "Kai <kai@example.org>", "bcc": "boss@example.com"})
    assert status == 200 and out["to"] == ["rob@agency.example.com"] and out["cc"] == ["kai@example.org"]
    sent = fake.sent[0]
    assert sent["recipients"] == ["rob@agency.example.com"] and sent["cc"] == ["kai@example.org"]
    assert sent["bcc"] == ["boss@example.com"]
    assert sent["in_reply_to"] == "<2@x>" and sent["references"] == "<1@x> <2@x>"
    assert closed == ["rob@agency.example.com", "kai@example.org"]
    assert filed[0][0] == "kai@example.org" and filed[0][-1] is None  # the KOL's row; several people: no Message-ID


def test_a_one_person_reply_keeps_its_message_id_for_the_sheet(outbox):
    fake, filed, _ = outbox
    status, _ = send({"to": "kai@example.org"})
    assert status == 200 and "cc" not in fake.sent[0] and "bcc" not in fake.sent[0]
    assert filed[0][0] == "kai@example.org" and filed[0][-1] == "<new@example.com>"


@pytest.mark.parametrize("body,error", [
    ({"to": ""}, "收件人、主题和正文都不能为空"),
    ({"to": "rob@agency.example.com", "cc": "not an address"}, "这些不像邮箱地址"),
    ({"to": "rob@agency.example.com", "bcc": ME}, "收件人里有你自己的邮箱"),
    ({"to": ",".join(f"p{i}@x.com" for i in range(app.COMPOSE_MAX_RECIPIENTS + 1))}, "最多"),
])
def test_a_reply_that_cannot_go_out_as_written_is_refused(outbox, body, error):
    fake, _, _ = outbox
    status, out = send(body)
    assert status == 400 and error in out["error"] and fake.sent == []


def test_a_reply_to_our_own_mail_is_threaded_under_it(outbox, monkeypatch):
    fake, filed, _ = outbox
    fake.msg = {**OURS[1], "references": None, "body": "Following up"}
    status, _ = send({"to": "rob@agency.example.com, kai@example.org"})
    assert status == 200 and fake.sent[0]["in_reply_to"] == "<3@x>"
    assert fake.sent[0]["recipients"] == ["rob@agency.example.com", "kai@example.org"]
