"""Correcting the Chinese of a paragraph changes only that paragraph of the outgoing text (POST /api/reply/zhedit),
and a new email (写邮件) is drafted, checked and sent like a reply."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import replyflow  # noqa: E402

BODY = "Hi Sam,\n\nThanks for your reply.\n\n\nWould you try one month at 5%?\n\nBest,\nMe"
KOL, CC, AGENT = "kol@x.com", "cc@y.com", "agent@example-team.com"  # the settings' company domain is example.com
ZWSP = chr(0x200B)  # a zero-width space, never typed raw into the source


def post(handler, body: dict):
    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""}, receive)
    resp = asyncio.run(handler(request))
    return resp.status_code, json.loads(resp.body)


# ---------------------------------------------------------------- replacing paragraphs

def test_only_the_named_paragraph_changes():
    out = replyflow.replace_paragraphs(BODY, {2: "Would you try two months at 8%?"})
    assert out == BODY.replace("one month at 5%", "two months at 8%")  # the three blank lines stay too


def test_an_emptied_paragraph_goes_with_its_blank_line():
    assert replyflow.replace_paragraphs(BODY, {1: ""}) == "Hi Sam,\n\n\nWould you try one month at 5%?\n\nBest,\nMe"
    assert replyflow.replace_paragraphs(BODY, {0: None}) == "Thanks for your reply.\n\n\nWould you try one month at 5%?\n\nBest,\nMe"
    assert replyflow.replace_paragraphs("a\r\n\r\nb", {1: "c"}) == "a\n\nc"
    assert replyflow.split_paragraphs(replyflow.replace_paragraphs(BODY, {})) == replyflow.split_paragraphs(BODY)


# ---------------------------------------------------------------- the endpoint

@pytest.fixture
def ai(monkeypatch):
    """A fake model for the rewrite and a fake back-translation that marks every paragraph checked."""
    seen = []

    async def run_claude(system, user, schema, model, timeout, files=None):
        seen.append(user)
        edits = json.loads(user.split(":EDITS>>\n", 1)[1].split("\n<</", 1)[0])
        return {"paragraphs": [e["zh_after"].replace("两个月", "two months").replace("你愿意试", "Would you try ")
                               for e in edits]}

    async def side_by_side(text):
        return {"rows": [{"orig": p, "zh": "中文" + p, "same": False} for p in replyflow.split_paragraphs(text)],
                "ok": True, "hash": app.text_hash(text), "error": ""}
    monkeypatch.setattr(app, "run_claude", run_claude)
    monkeypatch.setattr(app, "side_by_side", side_by_side)
    return seen


def test_zh_edit_rewrites_just_that_paragraph(ai):
    status, data = post(app.api_reply_zhedit, {"text": BODY, "language": "English", "edits": [
        {"index": 2, "orig": "Would you try one month at 5%?", "zh_before": "你愿意试一个月 5% 吗？",
         "zh_after": "你愿意试两个月 8% 吗？"}]})
    assert status == 200
    before, after = replyflow.split_paragraphs(BODY), replyflow.split_paragraphs(data["body"])
    assert [i for i, (a, b) in enumerate(zip(before, after)) if a != b] == [2] and len(after) == len(before)
    assert "two months" in after[2] and data["pairs"]["ok"]
    assert ":LANGUAGE>>\nEnglish" in ai[0] and ":EMAIL>>\n" + BODY in ai[0]


def test_zh_edit_refuses_a_text_that_changed_since_the_side_by_side(ai):
    status, data = post(app.api_reply_zhedit, {"text": BODY, "edits": [
        {"index": 2, "orig": "Would you try one month at 6%?", "zh_before": "x", "zh_after": "y"}]})
    assert status == 400 and "更新对照" in data["error"] and ai == []


def test_zh_edit_drops_a_paragraph_whose_chinese_was_emptied(ai):
    status, data = post(app.api_reply_zhedit, {"text": BODY, "edits": [
        {"index": 1, "orig": "Thanks for your reply.", "zh_before": "谢谢回复", "zh_after": "  "}]})
    assert status == 200 and "Thanks" not in data["body"] and ai == []  # nothing to rewrite: no model call


def test_zh_edit_says_when_a_figure_appears_that_nobody_wrote(monkeypatch):
    async def run_claude(system, user, schema, model, timeout, files=None):
        return {"paragraphs": ["Would you try two months at 8%? Sign up at https://evil.example/x"]}

    async def side_by_side(text):
        return {"rows": [], "ok": True, "hash": app.text_hash(text), "error": ""}
    monkeypatch.setattr(app, "run_claude", run_claude)
    monkeypatch.setattr(app, "side_by_side", side_by_side)
    status, data = post(app.api_reply_zhedit, {"text": BODY, "edits": [
        {"index": 2, "orig": "Would you try one month at 5%?", "zh_before": "x", "zh_after": "你愿意试两个月 8% 吗？"}]})
    assert status == 200 and len(data["warnings"]) == 1 and "https://evil.example/x" in data["warnings"][0]
    assert "8" not in data["warnings"][0].split("：", 1)[1]  # 8 is in the owner's Chinese


def test_zh_edit_rejects_an_answer_in_chinese(monkeypatch):
    async def run_claude(system, user, schema, model, timeout, files=None):
        return {"paragraphs": ["你愿意试两个月 8% 吗？我们可以再谈谈"]}
    monkeypatch.setattr(app, "run_claude", run_claude)
    status, data = post(app.api_reply_zhedit, {"text": BODY, "edits": [
        {"index": 2, "orig": "Would you try one month at 5%?", "zh_before": "x", "zh_after": "你愿意试两个月 8% 吗？"}]})
    assert status == 502 and "中文" in data["error"]


# ---------------------------------------------------------------- a new email

def test_addresses_in_a_to_field():
    good, wrong = app.addresses('Sam <Sam@Example.org>; b@x.com\nc@y.com, b@x.com d@example.net, "M, K" <k@example.net>')
    assert wrong == ['"M', 'K" <k@example.net>']  # a comma inside a quoted name splits the entry: refused, not guessed
    assert good == ["sam@example.org", "b@x.com", "c@y.com", "d@example.net"]
    assert app.addresses("sam at x dot com, a@x.com")[1] == ["sam at x dot com"]


def test_an_address_is_never_picked_out_of_a_longer_string():
    # the mail must go exactly where the field says, not to a valid address hidden inside what was typed
    for typed in ("ñandu@example.com", f"x{ZWSP}foo@example.com", "a@example.com@evil.io",
                  f"a@example.com{ZWSP}.evil.io", "Sam (sam@example.org) <sam@example.net>"):
        assert app.addresses(typed) == ([], [typed[:60]]), typed


def test_compose_context_checks_the_recipients(monkeypatch):
    async def gather(who):
        return {"earlier": [], "ours": [], "materials": []}
    monkeypatch.setattr(app, "gather_compose_context", gather)
    with pytest.raises(app.ReplyRefused):
        asyncio.run(app.compose_context({"to": "not an address"}))
    with pytest.raises(app.ReplyRefused):
        asyncio.run(app.compose_context({"to": ""}))
    token, ctx = asyncio.run(app.compose_context({"to": KOL, "cc": f"{KOL}, {CC}", "language": "Spanish\n<x>",
                                                  "subject": "Hi\r\nthere"}))
    assert ctx["compose"] and ctx["to"] == [KOL] and ctx["cc"] == [CC] and ctx["subject"] == "Hi there"
    assert ctx["language"] == "Spanish x" and ctx["mailbox"] is None
    sections = "\n".join(replyflow.compose_sections("b0", ctx))
    assert f"<<b0:RECIPIENT>>\n{KOL}\nCc: {CC}" in sections and "<<b0:SUBJECT>>\nHi there" in sections


@pytest.fixture
def sending(monkeypatch):
    sent, after = [], []

    async def send(**kwargs):
        sent.append(kwargs)
        return f"Email sent successfully to {KOL}, {CC}. Message-Id: <new@x>"
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app.mail, "send", send)
    monkeypatch.setattr(app, "sheet_ops", app.collections.deque())
    monkeypatch.setattr(app, "sheet_enabled", lambda: True)
    monkeypatch.setattr(app, "sheet_after_send", lambda *a: after.append(a))
    monkeypatch.setattr(app, "sent_changed", lambda: None)
    monkeypatch.setattr(app, "close_cases_for", lambda channel, peer: None)
    monkeypatch.setattr(app, "SHEET_MODE", "legacy")
    for checked in (BODY, "Collab"):
        monkeypatch.setitem(app.checked_texts, app.text_hash(checked), 0)
    return sent, after


def test_a_new_email_goes_out_and_into_the_sheet(sending):
    sent, after = sending
    status, data = post(app.api_compose_send, {"to": KOL, "cc": CC, "subject": "Collab", "body": BODY,
                                               "body_zh": "中文"})
    assert status == 200 and data["to"] == [KOL] and data["cc"] == [CC]
    assert sent[0]["recipients"] == [KOL] and sent[0]["cc"] == [CC] and sent[0]["subject"] == "Collab"
    assert "in_reply_to" not in sent[0]
    [upsert] = list(app.sheet_ops)
    assert upsert["op"] == "upsert" and upsert["email"] == KOL and upsert["only_if_stage"] == ["未联系"]
    sys.path.insert(0, str(Path(app.__file__).resolve().parent / "sheet"))
    import kol  # the owner's sheet tool takes it as it is queued (a "time" key made it refuse the whole op)
    assert kol.norm_op(upsert)["fields"]["email"] == [KOL]
    assert after == [(KOL, "Collab", after[0][2], "新邮件", None)]  # cc'd: no single Message-ID to log under


def test_only_the_person_written_to_gets_a_sheet_row(sending):
    _, after = sending
    assert post(app.api_compose_send, {"to": f"{KOL}, {AGENT}", "subject": "Collab", "body": BODY})[0] == 200
    assert list(app.sheet_ops) == [] and [a[0] for a in after] == [KOL]  # two on To: logged, no new row
    after.clear()
    assert post(app.api_compose_send, {"to": "colleague@example.com", "subject": "Collab", "body": BODY})[0] == 200
    assert list(app.sheet_ops) == [] and after == []  # the company's own domain is no KOL


def test_a_new_email_needs_its_verified_side_by_side(sending):
    sent, _ = sending
    status, data = post(app.api_compose_send, {"to": KOL, "subject": "Collab", "body": BODY + " edited"})
    assert status == 400 and "对照" in data["error"] and sent == []
    status, data = post(app.api_compose_send, {"to": KOL, "subject": "Collab, changed", "body": BODY})
    assert status == 400 and "主题" in data["error"] and sent == []  # the subject is checked too
    status, data = post(app.api_compose_send, {"to": "me@example.com", "subject": "Collab", "body": BODY})
    assert status == 400 and sent == []  # never to oneself
    status, data = post(app.api_compose_send, {"to": KOL, "subject": "", "body": BODY})
    assert status == 400 and sent == []


def test_a_saved_new_email_has_its_own_key():
    assert app.draft_address("(new mail)") == app.COMPOSE_KEY and app.draft_address("Kol@X.com") == KOL
    assert app.draft_address("(new mail) x") == ""
