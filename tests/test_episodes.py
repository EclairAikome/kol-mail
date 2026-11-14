"""Every reply sent is kept as an episode next to what the AI wrote (episodes.py, app.record_episode): recorded after
the send without ever failing it, with the owner's words, marked when a red line was broken on purpose, thinned out
after 180 days."""

import asyncio
import json
import time

import learnkit
import pytest
from learnkit import episode
from starlette.testclient import TestClient

import app
import episodes
import playbook_learn as pl

KOL = "kai@example.org"
TEXT = "Hi Kai,\n\nWe can offer 5% on new users and 1% on returning ones.\n\nMe"
AI = "Hi Kai,\n\nWe can offer 20% of net profit right away.\n\nMe"


def test_the_edit_ratio():
    assert pl.edit_ratio(AI, AI) == 0
    assert 0.03 < pl.edit_ratio(AI, TEXT) < 0.7
    assert pl.edit_ratio("", TEXT) == 1.0 and pl.edit_ratio("", "") == 0.0


def test_an_episode_keeps_the_owners_words_and_works_out_the_ratio(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    body = {"ai_body": AI, "feedback_log": ["先报默认档", app.replyflow.HAND_EDIT], "notes": "回他佣金",
            "zh_edits": [{"zh_before": "20% 净利润", "zh_after": "5% 和 1%", "index": 1}],
            "strategy": {"options": [{"title_zh": "A", "approach_zh": "a"}, {"title_zh": "B", "approach_zh": "b"}],
                         "recommended": 0, "chosen": 1},
            "compliance": {"checked": True, "revisions": 1, "missed": [], "waived_rules": [], "flagged": ["x"]}}
    eid = app.record_episode(body, channel="email", address=KOL.upper(), sent=TEXT, zh="中文", kol_text="Hello")
    ep = app.LEARN.episode(eid)
    assert ep["address"] == KOL and ep["ai_body"] == AI and ep["sent_body"] == TEXT
    assert ep["feedback_log"] == ["先报默认档"]  # the hand-edit marker is in the diff already
    assert ep["zh_edits"] == [{"zh_before": "20% 净利润", "zh_after": "5% 和 1%"}]
    assert ep["recommended"] == 0 and ep["chosen"] == 1 and ep["notes"] == "回他佣金"
    assert ep["edit_ratio"] == pl.edit_ratio(AI, TEXT) and not ep["exception"]
    assert ep["playbook_sha"] == app.LEARN.get("playbook_sha")
    assert pl.has_contrast(ep) and pl.learnable(ep)


@pytest.mark.parametrize("sent,check,exception", [
    (TEXT, {"waived_rules": ["payment-after-publication"]}, True),   # the owner allowed a red line once
    (TEXT, {"broken": [{"rule": "no-bank-transfer"}]}, True),
    ("Hi!\n\nWe can pay 50% upfront before the video.\n\nMe", {}, True),  # edited in by hand
    (TEXT, {"waived_rules": ["owner-wording-kept"]}, False),
])
def test_a_red_line_broken_on_purpose_is_an_exception(tmp_path, monkeypatch, sent, check, exception):
    learnkit.setup(tmp_path, monkeypatch)
    eid = app.record_episode({"ai_body": AI, "compliance": check}, channel="email", address=KOL, sent=sent)
    assert bool(app.LEARN.episode(eid)["exception"]) is exception
    assert pl.learnable(app.LEARN.episode(eid)) is not exception


def test_recording_never_fails_a_send(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)

    def broken(**fields):
        raise OSError("disk full")
    monkeypatch.setattr(app.LEARN, "record", broken)
    assert app.record_episode({"ai_body": AI}, channel="email", address=KOL, sent=TEXT) is None


class FakeMail:
    def __init__(self):
        self.sent = []

    async def get_message(self, mailbox, email_id):
        return {"message_id": "<1@x>", "sender": f"Kai <{KOL}>", "recipients": ["me@example.com"],
                "subject": "Rates", "body": "What do you pay?", "date": "2026-11-05T10:00:00+08:00"}

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return "Email sent successfully to them. Message-Id: <new@example.com>"


@pytest.fixture
def sending(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    fake = FakeMail()
    monkeypatch.setattr(app, "mail", fake)
    monkeypatch.setattr(app, "DEV", False)
    monkeypatch.setattr(app, "sent_changed", lambda: None)
    monkeypatch.setattr(app, "close_cases_for", lambda channel, peer, person="": None)
    monkeypatch.setattr(app, "sheet_guard", lambda *a: None)
    monkeypatch.setitem(app.checked_texts, app.text_hash(TEXT), time.time())
    return fake


def send(extra):
    r = asyncio.run(app.api_send(learnkit.Req({"mailbox": "INBOX", "email_id": "2", "subject": "Re: Rates",
                                               "body": TEXT, "to": KOL, "draft_key": KOL, **extra})))
    return r.status_code, json.loads(r.body)


def test_a_reply_sent_from_the_page_becomes_an_episode(sending):
    status, _ = send({"ai_body": AI, "feedback_log": ["先报默认档"], "body_zh": "中文"})
    assert status == 200 and len(sending.sent) == 1
    ep = app.LEARN.episodes()[-1]
    assert ep["sent_body"] == TEXT and ep["ai_body"] == AI and ep["kol_text"] == "What do you pay?"
    assert ep["message_id"] == "<new@example.com>" and ep["body_zh"] == "中文"


def test_a_send_still_succeeds_when_recording_fails(sending, monkeypatch):
    def broken(**fields):
        raise RuntimeError("sqlite gone")
    monkeypatch.setattr(app.LEARN, "record", broken)
    status, out = send({"ai_body": AI})
    assert status == 200 and out["ok"] and len(sending.sent) == 1


def test_the_saved_draft_keeps_the_ai_version_apart_from_the_edited_body(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "cache", app.Cache(tmp_path / "cache.sqlite3"))
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    client = TestClient(app.app, base_url=f"http://127.0.0.1:{app.PORT}")
    headers = {"X-KOL-Mail": "1", "Origin": f"http://127.0.0.1:{app.PORT}"}
    strategy = json.dumps({"options": [{"title_zh": "A"}], "recommended": 0, "chosen": 0})
    client.post("/api/reply/save", headers=headers, json={"address": KOL, "draft": {
        "body": TEXT, "ai_body": AI, "strategy": strategy, "zh_edits": "[]", "compliance": "{}", "mode": "draft"}})
    d = client.get(f"/api/reply/saved?address={KOL}", headers=headers).json()["draft"]
    assert d["body"] == TEXT and d["ai_body"] == AI and json.loads(d["strategy"])["chosen"] == 0


def test_the_page_restores_the_saved_ai_version_not_the_body():
    page = (app.BASE / "static" / "index.html").read_text(encoding="utf-8")
    assert "ctx.aiBody = d.body" not in page and "ctx.aiBody = d.ai_body ? d.ai_body : null" in page
    assert "ctx.aiBody = c.ai_body || c.body" in page


def test_how_they_answered_goes_on_our_last_reply(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    first = app.LEARN.record(**episode(at=1000.0))
    second = app.LEARN.record(**episode(at=2000.0))
    assert app.LEARN.note_reply(KOL.replace("kai", "kol"), "counter", 2500.0, ref="<a@x>") == second
    assert app.LEARN.episode(second)["stance_next"] == "counter" and not app.LEARN.episode(first)["stance_next"]
    # the same mail translated again, or a second mail in a row: nothing moves onto an older reply
    assert app.LEARN.note_reply("kol@example.org", "accept", 2500.0, ref="<a@x>") is None
    assert app.LEARN.note_reply("kol@example.org", "accept", 2600.0, ref="<b@x>") is None
    assert not app.LEARN.episode(first)["stance_next"]
    third = app.LEARN.record(**episode(at=3000.0))
    assert app.LEARN.note_reply("kol@example.org", "made-up", 3100.0) == third  # an unknown stance is "other"
    assert app.LEARN.episode(third)["stance_next"] == "other"


def test_a_week_of_silence_and_old_texts_dropped(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    old = app.LEARN.record(**episode(at=time.time() - episodes.RETENTION - 10))
    quiet = app.LEARN.record(**episode(address="quiet@example.org", at=time.time() - episodes.SILENT_AFTER - 10))
    assert app.LEARN.mark_silent() >= 1 and app.LEARN.episode(quiet)["outcome"].startswith("沉默")
    assert app.LEARN.prune() == 1
    ep = app.LEARN.episode(old)
    assert ep["ai_body"] is None and ep["sent_body"] is None and ep["kol_text"] is None
    assert "- We can offer 20% of net profit right away." in ep["summary"]


def test_a_reply_refusing_a_red_line_gets_no_result(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    refused = app.LEARN.record(**episode(sent_body="Sorry, we can't do any upfront payment.\n\nMe"))
    monkeypatch.setattr(app, "deal_addresses", lambda: {"kol@example.org"})
    asyncio.run(app.collect_outcomes())
    assert app.LEARN.episode(refused)["outcome_source"] == "redline"


def test_a_deal_folder_is_a_result(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    eid = app.LEARN.record(**episode())
    monkeypatch.setattr(app, "deal_addresses", lambda: {"kol@example.org"})
    asyncio.run(app.collect_outcomes())
    assert app.LEARN.episode(eid)["outcome_source"] == "deal_box"


def test_the_translation_carries_the_stance_without_requiring_it():
    assert "stance" in app.TRANSLATE_SCHEMA["properties"] and "stance" not in app.TRANSLATE_SCHEMA["required"]
    assert "stance:" in app.TRANSLATE_SYSTEM
