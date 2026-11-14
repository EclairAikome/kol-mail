"""One manager, two creators (convperson.py). Two creators invited at their own addresses under one templated
subject both pass the invitation to the same MCN manager, who answers about each from one address, sometimes copying
the creator, sometimes not. Filed by sender and subject, the mails about one ran into the other's conversation.
Every mail is now filed under the creator it is about."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import convperson  # noqa: E402

ME = "me@example.com"
AVA, BEN, MIA = "ava@creator-a.example.org", "ben@creator-b.example.org", "mia@mcn.example.com"
TEMPLATE = "Paid collab with Example"
SENT = "Sent"


def mail(uid, box, sender, recipients, subject, mid, day, **extra):
    return (box, {"email_id": str(uid), "message_id": mid, "subject": subject, "sender": sender,
                  "recipients": recipients, "date": f"2026-11-{day:02d}T10:00:00Z", **extra})


OUT_AVA = mail(1, SENT, f"Me <{ME}>", [AVA], TEMPLATE, "<o-ava@example.com>", 1)
OUT_BEN = mail(2, SENT, f"Me <{ME}>", [BEN], TEMPLATE, "<o-ben@example.com>", 1)
MIA_AVA_CC = mail(11, "INBOX", f"Mia <{MIA}>", [ME, AVA], f"Re: Fwd: {TEMPLATE}", "<m-ava-1@mcn.example.com>", 3)
MIA_BEN_CC = mail(12, "INBOX", f"Mia <{MIA}>", [ME, BEN], f"Re: Fwd: {TEMPLATE}", "<m-ben-1@mcn.example.com>", 3)
OURS_AVA = mail(3, SENT, f"Me <{ME}>", [MIA], f"Re: Fwd: {TEMPLATE}", "<r-ava-1@example.com>", 4)        # Mia alone
OURS_BEN = mail(4, SENT, f"Me <{ME}>", [MIA, BEN], f"Re: Fwd: {TEMPLATE}", "<r-ben-1@example.com>", 4)   # both
MIA_AVA = mail(13, "INBOX", f"Mia <{MIA}>", [ME], f"Re: Fwd: {TEMPLATE}", "<m-ava-2@mcn.example.com>", 5)  # no Cc
MIA_BEN = mail(14, "INBOX", f"Mia <{MIA}>", [ME], f"Re: Fwd: {TEMPLATE}", "<m-ben-2@mcn.example.com>", 5)  # no Cc
MIA_WHO = mail(15, "INBOX", f"Mia <{MIA}>", [ME], f"Re: Fwd: {TEMPLATE}", "<m-who@mcn.example.com>", 6)   # no clue

BODIES = {
    "3": {"in_reply_to": "<m-ava-1@mcn.example.com>", "references": "<m-ava-1@mcn.example.com>", "body": "Hi Mia"},
    "13": {"in_reply_to": "<r-ava-1@example.com>", "references": "<m-ava-1@mcn.example.com> <r-ava-1@example.com>",
           "body": "Ava can do 500 USD."},
    # Ben forwarded our invitation; Mia's first mail about him without Cc quotes it, his address in the forward
    "14": {"in_reply_to": "<fwd-ben@creator-b.example.org>", "references": "<fwd-ben@creator-b.example.org>",
           "body": "Ben asks 900 USD.\n\n---------- Forwarded message ---------\nFrom: Ben <ben@creator-b.example.org>\n"
                   "To: me@example.com\nSubject: Paid collab with Example"},
    "15": {"in_reply_to": "", "references": "", "body": "Any news?"},
}
ALL = [OUT_AVA, OUT_BEN, MIA_AVA_CC, MIA_BEN_CC, OURS_AVA, OURS_BEN, MIA_AVA, MIA_BEN, MIA_WHO]


def full(m):
    box, meta = m
    return {**meta, **BODIES.get(meta["email_id"], {"in_reply_to": "", "references": "", "body": "x"})}


@pytest.fixture
def world(monkeypatch):
    by_box = {"INBOX": [m for b, m in ALL if b == "INBOX"], SENT: [m for b, m in ALL if b == SENT]}
    monkeypatch.setattr(app, "sent_box", lambda: SENT)
    monkeypatch.setattr(app, "index_boxes", lambda: ("INBOX", SENT))
    monkeypatch.setattr(app, "folder_metas_known", lambda box: by_box.get(box, []))

    async def entries():
        return list(ALL)

    async def bodies(members):
        return {(box, m["email_id"]): full((box, m)) for box, m in members}
    monkeypatch.setattr(app, "indexed_entries", entries)
    monkeypatch.setattr(app, "thread_bodies", bodies)
    return by_box


def learn_all():
    for m in ALL:
        app.learn_person(full(m))


def ids(members):
    return sorted((m["email_id"] for _, m in members), key=int)


def test_a_creator_on_cc_or_answered_alone_files_the_mail(world):
    assert app.person_of(MIA_AVA_CC[1]) == AVA and app.person_of(MIA_BEN_CC[1]) == BEN
    assert app.person_of(OURS_BEN[1]) == BEN       # our answer to Mia and Ben
    assert app.person_of(OUT_AVA[1]) == AVA
    assert app.person_of(MIA_AVA[1]) == MIA        # nothing on it says whom: until it is read, Mia's


def test_reading_a_mail_files_it_by_what_it_answers_or_quotes(world):
    learn_all()
    assert app.person_of(OURS_AVA[1]) == AVA       # our answer to Mia alone answers her mail about Ava
    assert app.person_of(MIA_AVA[1]) == AVA        # she answers that answer
    assert app.person_of(MIA_BEN[1]) == BEN        # she quotes Ben's forward
    assert app.person_of(MIA_WHO[1]) == MIA        # no rule can tell: stays hers
    assert app.person_how(MIA_AVA[1]) == "refs" and app.person_how(MIA_BEN[1]) == "text"
    assert app.person_how(MIA_AVA_CC[1]) == "people" and app.person_how(MIA_WHO[1]) == ""


def test_the_list_shows_one_row_per_creator(world):
    learn_all()
    key = lambda m: app.thread_key(m[1])  # noqa: E731
    assert key(MIA_AVA_CC) == key(MIA_AVA) == key(OURS_AVA) == key(OUT_AVA)
    assert key(MIA_BEN_CC) == key(MIA_BEN) == key(OURS_BEN) == key(OUT_BEN)
    assert key(MIA_AVA) != key(MIA_BEN) and key(MIA_WHO) not in (key(MIA_AVA), key(MIA_BEN))


def conversation(opened):
    box, meta = opened
    candidates = asyncio.run(app.thread_candidates(meta))
    return ids(app.thread_others(box, meta["email_id"], app.message_identity(box, meta),
                                 app.norm_subject(meta["subject"]), candidates))


def test_opening_a_mail_about_one_creator_shows_only_theirs(world):
    learn_all()
    assert conversation(MIA_AVA) == ["1", "3", "11"]
    assert conversation(MIA_BEN) == ["2", "4", "12"]
    assert conversation(MIA_AVA_CC) == ["1", "3", "13"]
    assert conversation(MIA_WHO) == []  # none of the creators' mails: what is left of Mia's own


def test_even_before_anything_was_read_the_cc_keeps_them_apart(world):
    assert "12" not in conversation(MIA_AVA_CC) and "11" not in conversation(MIA_BEN_CC)


def test_a_draft_about_one_creator_reads_none_of_the_other(world):
    learn_all()
    ours, earlier = asyncio.run(app.indexed_history(AVA, "<m-ava-2@mcn.example.com>", subject=app.norm_subject(TEMPLATE)))
    assert sorted(m["email_id"] for m in earlier) == ["11"]
    assert sorted(m["email_id"] for m in ours) == ["1", "3"]


def test_the_reply_context_reads_the_creators_history_and_answers_the_manager(world, monkeypatch):
    learn_all()

    async def body(bridge, box, uid):
        return full(next(m for m in ALL if m[1]["email_id"] == uid))

    async def materials(found):
        return []
    monkeypatch.setattr(app, "load_body", body)
    monkeypatch.setattr(app, "email_materials", materials)
    ctx = asyncio.run(app.gather_reply_context("INBOX", "13"))
    assert ctx["kol"] == MIA and ctx["person"] == AVA
    assert "900 USD" not in json.dumps(ctx["earlier"]) and [m["email_id"] for m in ctx["earlier"]] == ["11"]


def test_the_owner_can_file_a_mail_by_hand_and_what_answers_it_follows(world):
    learn_all()
    store = app.person_store()
    store.set_hand("<m-who@mcn.example.com>", BEN)
    assert app.person_of(MIA_WHO[1]) == BEN and app.person_how(MIA_WHO[1]) == "hand"
    assert conversation(MIA_WHO) == ["2", "4", "12", "14"]
    later = mail(16, "INBOX", f"Mia <{MIA}>", [ME], f"Re: {TEMPLATE}", "<m-later@mcn.example.com>", 7)
    app.learn_person({**later[1], "in_reply_to": "<m-who@mcn.example.com>", "references": "", "body": "?"})
    assert app.person_of(later[1]) == BEN
    store.set_hand("<m-who@mcn.example.com>", "")  # back to the rules
    assert app.person_of(MIA_WHO[1]) == MIA


def test_the_owners_choice_beats_every_rule(world):
    app.person_store().set_hand("<m-ava-1@mcn.example.com>", BEN)  # Mia copied Ava, but wrote about Ben
    assert app.person_of(MIA_AVA_CC[1]) == BEN
    app.learn_person(full(MIA_AVA_CC))
    assert app.person_store().hand("<m-ava-1@mcn.example.com>") == BEN


def test_an_ordinary_creator_is_filed_as_before(world):
    kai = mail(30, "INBOX", "Kai <kai@example.org>", [ME], "Hello there", "<kai@example.org>", 2)[1]
    assert app.person_of(kai) == "kai@example.org" == app.counterpart(kai) and app.person_how(kai) == ""
    group = mail(31, SENT, f"Me <{ME}>", [AVA, BEN, MIA, "x@example.org", "y@example.org"], TEMPLATE, "<g@x>", 2)[1]
    assert app.person_of(group) == ""  # a group mail is nobody's conversation, as before


# ---------------------------------------------------------------- the sheet, the send, the phone cases


def test_the_sheet_books_the_managers_mail_on_the_creators_row(world, monkeypatch):
    posted = []
    monkeypatch.setattr(app, "sheet_post", posted.append)
    monkeypatch.setattr(app, "sheet_enabled", lambda: True)
    monkeypatch.setattr(app, "sheet_seen", {})
    monkeypatch.setattr(app, "save_sheet_seen", lambda: None)
    monkeypatch.setattr(app, "quiet_old", lambda meta: False)
    app.sheet_note_incoming([MIA_BEN_CC[1], MIA_AVA_CC[1]])
    assert [(p["email"], "name" in p) for p in posted] == [(AVA, False), (BEN, False)]  # oldest first
    posted.clear()
    app.sheet_note_translation("<m-ava-1@mcn.example.com>", full(MIA_AVA_CC), "可以合作")  # the gist, same row
    assert posted[0]["email"] == AVA and posted[0]["summary"] == "Mia 代为回信：可以合作"


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


TEXT = "Hi Mia,\n\nThanks!\n\nMe"


def test_a_reply_to_the_manager_alone_is_filed_under_the_creator(world, monkeypatch):
    learn_all()
    fake, filed, closed = FakeMail(full(MIA_AVA)), [], []
    monkeypatch.setattr(app, "mail", fake)
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app, "sent_changed", lambda: None)
    monkeypatch.setattr(app, "close_cases_for", lambda channel, peer, person="": closed.append((peer, person)))
    monkeypatch.setattr(app, "sheet_guard", lambda what, fn, *args: filed.append(args))
    monkeypatch.setattr(app, "record_episode", lambda *a, **k: None)
    monkeypatch.setitem(app.checked_texts, app.text_hash(TEXT), time.time())
    response = asyncio.run(app.api_send(Req({"mailbox": "INBOX", "email_id": "13", "subject": f"Re: {TEMPLATE}",
                                             "body": TEXT, "to": MIA, "draft_key": AVA})))
    assert response.status_code == 200 and fake.sent[0]["recipients"] == [MIA]
    assert filed[0][0] == AVA and closed == [(MIA, AVA)]
    assert app.person_of({"message_id": "<new@example.com>", "sender": ME, "recipients": [MIA],
                          "subject": f"Re: {TEMPLATE}"}) == AVA  # our new mail is Ava's at once


def test_answering_one_creator_leaves_the_managers_case_about_the_other_open(world, monkeypatch):
    learn_all()
    moved, kv = [], {"person:7": BEN}

    class Cases:
        def open_for_peer(self, channel, peer):
            return [{"id": 6, "channel": "email", "peer": MIA, "ref": "<m-ava-2@mcn.example.com>"},
                    {"id": 7, "channel": "email", "peer": MIA, "ref": "<unknown@mcn.example.com>"},
                    {"id": 8, "channel": "email", "peer": MIA, "ref": "<m-who@mcn.example.com>"}]

        def get_kv(self, key, default=""):
            return kv.get(key, default)

        def move(self, case_id, frm, to):
            moved.append(case_id)
            return False
    monkeypatch.setattr(app, "cases", Cases())
    app.close_cases_for("email", MIA, AVA)
    assert moved == [6, 8]  # Ava's, and the one nobody could file; Ben's stays open
    moved.clear()
    app.close_cases_for("email", MIA)
    assert moved == [6, 7, 8]  # a new mail to Mia herself answers all of hers, as before


# ---------------------------------------------------------------- moving a mail by hand on the page


class GetReq:
    method = "GET"

    def __init__(self, params):
        self.query_params = params


class PostReq(Req):
    method = "POST"


def test_the_page_can_ask_whom_and_move_a_mail(world, monkeypatch):
    learn_all()

    async def body(bridge, box, uid):
        return full(next(m for m in ALL if m[1]["email_id"] == uid))
    monkeypatch.setattr(app, "load_body", body)
    out = json.loads(asyncio.run(app.api_conv_person(GetReq({"mailbox": "INBOX", "id": "15"}))).body)
    assert out["person"] == MIA and out["how"] == "" and out["sender"] == MIA
    assert out["candidates"][0] == MIA and AVA in out["candidates"] and BEN in out["candidates"]
    out = json.loads(asyncio.run(app.api_conv_person(PostReq({"mailbox": "INBOX", "id": "15", "person": AVA}))).body)
    assert out["person"] == AVA and out["how"] == "hand"
    for bad, error in (("not an address", "不像邮箱"), (ME, "你自己"), ("__compose__", "不像邮箱")):
        r = asyncio.run(app.api_conv_person(PostReq({"mailbox": "INBOX", "id": "15", "person": bad})))
        assert r.status_code == 400 and error in json.loads(r.body)["error"]
    out = json.loads(asyncio.run(app.api_conv_person(PostReq({"mailbox": "INBOX", "id": "15", "person": ""}))).body)
    assert out["person"] == MIA


def test_a_tag_in_front_of_the_invitation_is_no_reply_marker(world, monkeypatch):
    """Review: "[Paid Collab] ..." matched the reply prefixes, so tagged invitations never counted."""
    tagged = "[Paid Collab] Example x your channel"
    out = mail(41, SENT, f"Me <{ME}>", [AVA], tagged, "<o-tag@example.com>", 1)
    reply = mail(42, "INBOX", f"Mia <{MIA}>", [ME, AVA], f"Re: {tagged}", "<m-tag@mcn.example.com>", 2)
    monkeypatch.setattr(app, "folder_metas_known", lambda box: [out[1]] if box == SENT else [reply[1]])
    assert app.first_mail(out[1]) and not app.first_mail({"subject": f"[Paid Collab] Re: {tagged}"})
    assert app.person_of(reply[1]) == AVA


def test_answering_one_creators_mail_while_quoting_another_learns_nothing(world):
    """Review: the manager hits reply on the latest mail (about Ava) and writes about Ben, his forward quoted."""
    learn_all()
    odd = {**MIA_WHO[1], "message_id": "<m-odd@mcn.example.com>", "in_reply_to": "<r-ava-1@example.com>",
           "references": "", "body": "About Ben:\nFrom: Ben <ben@creator-b.example.org>"}
    app.learn_person(odd)
    assert app.person_store().get("<m-odd@mcn.example.com>") is None and app.person_of(odd) == MIA


def test_a_chain_back_to_the_manager_alone_leaves_it_to_the_text(world):
    learn_all()
    later = {**MIA_WHO[1], "message_id": "<m-later2@mcn.example.com>", "in_reply_to": "<m-who@mcn.example.com>",
             "references": "", "body": "Ben agrees.\n> To: ben@creator-b.example.org"}
    app.learn_person(later)
    assert app.person_of(later) == BEN and app.person_how(later) == "text"


def test_an_agency_we_also_invited_keeps_its_mail_with_the_creator_on_cc_in_their_conversation(world, monkeypatch):
    invited_too = mail(43, SENT, f"Me <{ME}>", [MIA], TEMPLATE, "<o-mia@example.com>", 1)
    by_box = {"INBOX": [m for b, m in ALL if b == "INBOX"], SENT: [m for b, m in ALL if b == SENT] + [invited_too[1]]}
    monkeypatch.setattr(app, "folder_metas_known", lambda box: by_box.get(box, []))
    assert app.person_of(MIA_AVA_CC[1]) == MIA  # two invited on it: the sender, as the old rule did
    assert "11" in conversation(OUT_AVA)


def test_a_store_file_that_cannot_be_read_never_breaks_the_list(tmp_path, monkeypatch):
    """Review: a locked file (virus scanner, a bad ACL) raised from every person_of, so the list failed."""
    path = tmp_path / "persons.json"
    path.write_text(json.dumps({"<a@x>": [AVA, "hand"]}), encoding="utf-8")
    store = convperson.Store(path)
    real = Path.read_text

    def locked(self, *a, **k):
        if self == path:
            raise PermissionError("locked")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", locked)
    assert store.get("<a@x>") is None and store.readonly
    assert not store.learn("<b@x>", BEN, "refs") or json.loads(real(path, encoding="utf-8")) == {"<a@x>": [AVA, "hand"]}
    monkeypatch.setattr(Path, "read_text", real)
    monkeypatch.setattr(convperson.time, "monotonic", lambda: store.retry_at + 1)
    assert store.hand("<a@x>") == AVA and not store.readonly  # read again a minute later


# ---------------------------------------------------------------- the store and the rules on their own


def test_the_rules_on_their_own():
    assert convperson.by_people(MIA, {MIA, AVA}, False, {AVA, BEN}) == AVA
    assert convperson.by_people(MIA, {MIA, AVA, BEN}, False, {AVA, BEN}) == ""      # both on it: no rule tells
    assert convperson.by_people(AVA, {AVA, MIA}, False, {AVA, MIA}) == AVA          # two invited: the sender
    assert convperson.by_people(ME, {AVA, MIA}, True, {AVA, MIA}) == ""
    assert convperson.ids_nearest_first("<c@x>", "<a@x> <b@x> <c@x>") == ["<c@x>", "<b@x>", "<a@x>"]
    assert convperson.by_refs(["<m@x>", "<o@x>"], {"<m@x>": MIA, "<o@x>": AVA}.get, {AVA}) == AVA
    assert convperson.by_refs(["<m@x>"], {"<m@x>": MIA}.get, {AVA}) == MIA
    assert convperson.by_text("cc ava@creator-a.example.org.", {AVA, BEN}, MIA, ME) == AVA
    assert convperson.by_text(f"{AVA} and {BEN}", {AVA, BEN}, MIA, ME) == ""


def test_the_store_keeps_the_owners_choices_and_never_writes_over_a_bad_file(tmp_path, monkeypatch):
    path = tmp_path / "persons.json"
    store = convperson.Store(path)
    assert store.learn("<A@x>", AVA, "refs") and store.get("<a@x>") == (AVA, "refs")
    assert not store.learn("<a@x>", AVA, "refs")  # unchanged: nothing written
    store.set_hand("<b@x>", BEN)
    assert not store.learn("<b@x>", AVA, "refs") and store.hand("<b@x>") == BEN
    assert json.loads(path.read_text(encoding="utf-8")) == {"<a@x>": [AVA, "refs"], "<b@x>": [BEN, "hand"]}
    monkeypatch.setattr(convperson, "KEEP", 2)
    store.learn("<c@x>", AVA, "text")
    assert store.get("<a@x>") is None and store.hand("<b@x>") == BEN  # the oldest learned one goes first

    path.write_text("{oops", encoding="utf-8")
    again = convperson.Store(path)
    assert again.get("<b@x>") is None and (tmp_path / "persons.bad").read_text(encoding="utf-8") == "{oops"

    bad = tmp_path / "held.json"
    bad.write_text("{oops", encoding="utf-8")
    held = convperson.Store(bad)

    def refuse(src, dst):
        raise OSError("held by another program")
    monkeypatch.setattr(convperson.os, "replace", refuse)
    held.learn("<d@x>", AVA, "refs")
    assert bad.read_text(encoding="utf-8") == "{oops" and held.readonly
