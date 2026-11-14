"""KOL replies Alibaba Mail filed as spam go back to INBOX (junkrescue.py). Seen 2026-11-12: 7 replies in 垃圾邮件
within a week, a signed contract among them, none of them announced or logged."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import junkrescue  # noqa: E402

JUNK = "垃圾邮件"
SENT = [{"email_id": "50", "sender": "me@example.com", "recipients": ["Ana <ana@example.com>"],
         "message_id": "<SES-1@ses.example.net>"},
        {"email_id": "51", "sender": "me@example.com", "recipients": ["sd@agency.example.com"],
         "message_id": "<179377396273.11620.2@example.com>"}]


def junk_mail(uid, sender, mid):
    return {"email_id": uid, "sender": sender, "recipients": ["me@example.com"], "message_id": mid, "subject": "Re: x"}


def head(uid, references="", in_reply_to=""):
    return {"email_id": uid, "references": references, "in_reply_to": in_reply_to}


class Server(app.MailBridge):
    def __init__(self, junk, heads, move_result=None):
        super().__init__("t")
        self.junk, self.heads, self.moves, self.fetched = list(junk), heads, [], []
        self.move_result = move_result

    async def call(self, tool, timeout=180, raw=False, **args):
        if tool == "list_emails_metadata":
            return {"emails": list(self.junk)[:args.get("page_size", 10)] if args["mailbox"] == JUNK else []}
        if tool == "get_emails_content":
            assert args["mark_as_read"] is False
            self.fetched += args["email_ids"]
            return {"emails": [self.heads.get(uid, head(uid)) for uid in args["email_ids"]]}
        if tool == "move_emails":
            self.moves.append((args["source_mailbox"], args["destination_mailbox"], list(args["email_ids"])))
            if self.move_result:
                return {"result": self.move_result}
            self.junk = [m for m in self.junk if m["email_id"] not in args["email_ids"]]
            return {"result": f"Successfully moved {len(args['email_ids'])} email(s) to INBOX"}
        raise AssertionError(tool)


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "junk_memories", {})
    monkeypatch.setattr(app, "junk_left", {})
    monkeypatch.setattr(app, "junk_tries", {})
    monkeypatch.setattr(app, "folder_role", lambda role: {"junk": JUNK, "sent": "已发送"}.get(role, ""))
    monkeypatch.setattr(app, "sent_box", lambda: "已发送")
    sent = {"metas": SENT}
    monkeypatch.setattr(app, "folder_metas_known", lambda box: sent["metas"] if box == "已发送" else [])
    monkeypatch.setitem(app.folder_info.mine(), "names", ["INBOX", JUNK, "已发送"])

    def spawn(coro, what):
        coro.close()
    monkeypatch.setattr(app, "spawn", spawn)

    def make(junk, heads=None, move_result=None, sent_metas=SENT):
        sent["metas"] = sent_metas
        server = Server(junk, heads or {}, move_result)
        monkeypatch.setattr(app, "bg", server)
        monkeypatch.setattr(app, "mail", server)
        return server
    return make


def test_an_answer_to_one_of_our_mails_goes_back(mailbox):
    """A signed contract: In-Reply-To names our mail as Amazon SES rewrote its Message-ID."""
    server = mailbox([junk_mail("3", "Ana <ana@example.com>", "<s@mail.example.org>")],
                     {"3": head("3", "<x@mail.example.org> <ses-1@ses.example.net>", "<ses-1@ses.example.net>")})
    asyncio.run(app.rescue_junk())
    assert server.moves == [(JUNK, "INBOX", ["3"])]


def test_a_colleague_answering_our_mail_to_an_agency_goes_back(mailbox):
    """An agency colleague answered our mail to sd@ from kay@: only References tie it to us."""
    server = mailbox([junk_mail("8", "Kay <kay@agency.example.com>", "<kay@mail.example.com>")],
                     {"8": head("8", "<179377396273.11620.2@example.com> <010001a1a5@ses.example.net>")})
    asyncio.run(app.rescue_junk())
    assert server.moves == [(JUNK, "INBOX", ["8"])]


def test_an_answer_to_a_mail_whose_sent_copy_was_lost_goes_back_by_our_domain(mailbox):
    server = mailbox([junk_mail("4", "New <new@example.org>", "<n@mail.example.org>")],
                     {"4": head("4", "<999.1.3@example.com>", "<999.1.3@example.com>")})
    asyncio.run(app.rescue_junk())
    assert server.moves == [(JUNK, "INBOX", ["4"])]


def test_a_forged_kol_sender_answering_nothing_of_ours_stays(mailbox):
    """Phishing posing as a KOL we wrote to: the From alone is never trusted."""
    server = mailbox([junk_mail("5", "Ana <ana@example.com>", "<p@phish.example.net>")], {"5": head("5", "<x@phish.example.net>")})
    asyncio.run(app.rescue_junk())
    asyncio.run(app.rescue_junk())
    assert server.moves == [] and server.fetched == ["5"]  # not read again either


def test_with_sent_not_indexed_yet_a_mail_is_asked_again_later(mailbox, monkeypatch):
    """Just after a start the Sent index may be empty: a mail it would have matched is not given up on."""
    mail = junk_mail("3", "Ana <ana@example.com>", "<s@mail.example.org>")
    heads = {"3": head("3", "<ses-1@ses.example.net>")}
    server = mailbox([mail], heads, sent_metas=[])
    asyncio.run(app.rescue_junk())
    assert server.moves == []
    monkeypatch.setattr(app, "folder_metas_known", lambda box: SENT if box == "已发送" else [])
    asyncio.run(app.rescue_junk())
    assert server.moves == [(JUNK, "INBOX", ["3"])]


def test_bounces_stay_in_junk_unread(mailbox):
    server = mailbox([junk_mail("6", "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "<d@x>")])
    asyncio.run(app.rescue_junk())
    assert server.moves == [] and server.fetched == []


def test_a_mail_moved_back_once_and_put_into_junk_again_stays(mailbox):
    mail = junk_mail("3", "Ana <ana@example.com>", "<s@mail.example.org>")
    heads = {"3": head("3", "<ses-1@ses.example.net>"), "9": head("9", "<ses-1@ses.example.net>")}
    server = mailbox([mail], heads)
    asyncio.run(app.rescue_junk())
    server.junk = [dict(mail, email_id="9")]  # the owner put it back into junk in the webmail
    asyncio.run(app.rescue_junk())
    assert server.moves == [(JUNK, "INBOX", ["3"])]
    assert "<s@mail.example.org>" in junkrescue.Memory(app.DATA / "junk_rescued.json")  # also after a restart


def test_a_move_the_server_keeps_refusing_is_tried_three_times_only(mailbox):
    server = mailbox([junk_mail("3", "Ana <ana@example.com>", "<s@mail.example.org>")],
                     {"3": head("3", "<ses-1@ses.example.net>")}, move_result="Move result: 3 failed")
    for _ in range(5):
        asyncio.run(app.rescue_junk())
    assert len(server.moves) == app.JUNK_TRIES
    assert "<s@mail.example.org>" not in junkrescue.Memory(app.DATA / "junk_rescued.json")


class Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


@pytest.mark.parametrize("cached", [True, False])
def test_a_mail_the_owner_files_as_junk_is_never_taken_out(mailbox, monkeypatch, cached):
    """Also one no cache knows (moved from a search result): its Message-ID is read before the move."""
    mail = junk_mail("3", "Ana <ana@example.com>", "<s@mail.example.org>")
    monkeypatch.setattr(app, "known_meta", lambda box, uid: mail if cached and (box, uid) == ("INBOX", "3") else None)
    heads = {"3": dict(head("3", "<ses-1@ses.example.net>"), message_id="<s@mail.example.org>"),
             "12": head("12", "<ses-1@ses.example.net>")}
    server = mailbox([], heads)
    asyncio.run(app.api_move(Req({"mailbox": "INBOX", "to": JUNK, "email_ids": ["3"]})))
    server.junk = [dict(mail, email_id="12")]  # where the server put it
    asyncio.run(app.rescue_junk())
    assert server.moves == [("INBOX", JUNK, ["3"])]


def test_another_mailbox_than_the_primary_one_is_left_alone(mailbox, monkeypatch):
    server = mailbox([junk_mail("3", "Ana <ana@example.com>", "<s@mail.example.org>")],
                     {"3": head("3", "<ses-1@ses.example.net>")})
    monkeypatch.setattr(app, "is_primary", lambda: False)
    asyncio.run(app.rescue_junk())
    assert server.moves == [] and server.fetched == []


def test_only_a_few_headers_are_read_per_scan_and_the_rest_next_time(mailbox):
    junk = [junk_mail(str(i), f"x{i} <x{i}@spam.example.net>", f"<{i}@spam.example.net>") for i in range(1, 21)]
    server = mailbox(junk)
    asyncio.run(app.rescue_junk())
    assert len(server.fetched) == junkrescue.CONTENT_MAX
    asyncio.run(app.rescue_junk())
    assert len(set(server.fetched)) == 2 * junkrescue.CONTENT_MAX


def test_no_junk_folder_means_no_call(mailbox, monkeypatch):
    server = mailbox([])
    monkeypatch.setattr(app, "folder_role", lambda role: "")
    asyncio.run(app.rescue_junk())
    assert server.moves == [] and server.fetched == []


def test_a_failing_rescue_never_stops_the_inbox_scan(mailbox, monkeypatch):
    async def broken():
        raise app.MailError("junk folder unreadable")
    seen = []

    async def inbox(box, page, size=30):
        seen.append(box)
        return {"emails": []}
    server = mailbox([])
    monkeypatch.setattr(app, "rescue_junk", broken)
    monkeypatch.setattr(server, "list_messages", inbox)
    for name in ("sheet_guard", "notify_new"):
        monkeypatch.setattr(app, name, lambda *a, **k: None)

    async def nothing(*a, **k):
        pass
    monkeypatch.setattr(app, "offhours_email", nothing)
    asyncio.run(app.scan_account())
    assert seen == ["INBOX"]


def test_a_garbled_memory_file_is_kept_aside_not_overwritten(tmp_path):
    path = tmp_path / "junk_rescued.json"
    path.write_text("{oops", encoding="utf-8")
    memory = junkrescue.Memory(path)
    assert "<a@x>" not in memory
    memory.add(["<B@x>"])
    assert (tmp_path / "junk_rescued.bad").read_text(encoding="utf-8") == "{oops"
    assert json.loads(path.read_text(encoding="utf-8")) == ["<b@x>"] and "<b@X>" in memory


def test_ids_compare_without_case():
    assert junkrescue.answers_us({"references": "<ABC@Example.com>"}, {"<abc@example.com>"}, "other.com")
    assert not junkrescue.answers_us({"references": "<abc@example.com.example.net>"}, set(), "example.com")


def test_on_a_public_mailbox_the_domain_alone_proves_nothing(mailbox, monkeypatch):
    """Review of 1.0.1: on 163.com or icloud.com everyone's Message-IDs end the same way, so a spam quoting any
    such ID looked like an answer to us. Only our own Sent IDs count there."""
    monkeypatch.setattr(app, "self_address", lambda: "li@163.com")
    server = mailbox([junk_mail("3", "Spam <spam@spam.example.net>", "<s@spam.example.net>"),
                      junk_mail("4", "Ana <ana@example.com>", "<a@mail.example.org>")],
                     {"3": head("3", "<a@163.com>"), "4": head("4", "", "<SES-1@ses.example.net>")})
    asyncio.run(app.rescue_junk())
    assert server.moves == [(JUNK, "INBOX", ["4"])]


def test_one_mail_with_huge_headers_does_not_hold_up_the_others(mailbox):
    """Review of 1.0.1: the library refuses a whole batch when one mail's References are over its limit. The
    batch is read again one by one, the oversized mail is left alone for this run, the real reply goes back."""
    server = mailbox([junk_mail("3", "Ana <ana@example.com>", "<a@mail.example.org>"),
                      junk_mail("4", "Big <big@spam.example.net>", "<big@spam.example.net>")],
                     {"3": head("3", "", "<SES-1@ses.example.net>")})
    real = server.call

    async def call(tool, timeout=180, raw=False, **args):
        if tool == "get_emails_content" and "4" in args["email_ids"]:
            server.fetched += args["email_ids"]
            raise app.MailError("limit_exceeded: an email thread header exceeds 65536 bytes")
        return await real(tool, timeout=timeout, raw=raw, **args)
    server.call = call
    asyncio.run(app.rescue_junk())
    assert server.moves == [(JUNK, "INBOX", ["3"])]
    assert server.fetched == ["3", "4", "3", "4"]  # the batch, then one by one
    server.fetched.clear()
    asyncio.run(app.rescue_junk())  # the next scan does not ask for the oversized one again
    assert server.fetched == [] and len(server.moves) == 1


def test_a_lone_mail_with_huge_headers_is_left_without_failing_the_scan(mailbox):
    server = mailbox([junk_mail("4", "Big <big@spam.example.net>", "<big@spam.example.net>")])

    async def call(tool, timeout=180, raw=False, **args):
        assert tool == "list_emails_metadata" or tool == "get_emails_content"
        if tool == "get_emails_content":
            server.fetched += args["email_ids"]
            raise app.MailError("limit_exceeded: an email thread header exceeds 65536 bytes")
        return {"emails": list(server.junk)}
    server.call = call
    asyncio.run(app.rescue_junk())  # no error
    asyncio.run(app.rescue_junk())
    assert server.fetched == ["4"] and server.moves == []


def test_a_batch_that_fails_for_another_reason_is_tried_again_next_scan(mailbox):
    server = mailbox([junk_mail("3", "Ana <ana@example.com>", "<a@mail.example.org>")],
                     {"3": head("3", "", "<SES-1@ses.example.net>")})
    real, failing = server.call, [True]

    async def call(tool, timeout=180, raw=False, **args):
        if tool == "get_emails_content" and failing:
            raise app.MailError("provider_failure: timed out")
        return await real(tool, timeout=timeout, raw=raw, **args)
    server.call = call
    with pytest.raises(app.MailError):
        asyncio.run(app.rescue_junk())
    failing.clear()
    asyncio.run(app.rescue_junk())
    assert server.moves == [(JUNK, "INBOX", ["3"])]
