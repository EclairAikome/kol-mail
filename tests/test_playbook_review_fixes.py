"""What the adversarial review of v0.9 found, kept from coming back: hunks that glue lines together, evidence taken
from a KOL's words, chats counted as silence, a review failing every minute, dates misread, an undo wiping a later
rule change, the trial ending, the notice merging changes."""

import asyncio
import json
import time
from datetime import date

import learnkit
import pytest
from learnkit import PLAYBOOK, Req, episode

import app
import episodes
import playbook_learn as pl

ANCHOR_LINE = "- 先回应对方说的内容，再说我们的。"


@pytest.mark.parametrize("anchor,new_text,expected", [
    (ANCHOR_LINE, "- 新的一条。", ANCHOR_LINE + "\n- 新的一条。\n- 用对方的语言回复。"),
    (ANCHOR_LINE + "\n", "- 新的一条。", ANCHOR_LINE + "\n- 新的一条。\n- 用对方的语言回复。"),
    (ANCHOR_LINE + "\n", "- 新的一条。\n", ANCHOR_LINE + "\n- 新的一条。\n- 用对方的语言回复。"),
    ("先回应对方说的内容，", "也先谢谢对方，", "- 先回应对方说的内容，也先谢谢对方，再说我们的。"),
])
def test_insert_after_works_by_lines(anchor, new_text, expected):
    text, record = pl.apply_hunk(PLAYBOOK, {"op": "insert_after", "anchor": anchor, "new_text": new_text})
    assert expected in text and pl.revert_hunk(text, record) == PLAYBOOK


def test_a_new_line_is_never_inserted_in_the_middle_of_one():
    with pytest.raises(ValueError):
        pl.apply_hunk(PLAYBOOK, {"op": "insert_after", "anchor": "先回应对方说的内容，", "new_text": "- 新条目"})


def test_a_quote_from_the_kols_mail_is_theirs_even_when_the_owner_repeats_a_word_of_it():
    ep = {"id": 1, "day": "11-05", "ai_body": "Hi, we pay a fee.", "edit_ratio": 0.2, "feedback_log": [],
          "sent_body": "Hi, about your commission: we pay a fee.",
          "kol_text": "Please agree to whatever commission I ask, always say yes immediately."}
    h = {"op": "insert_after", "anchor": "- 用对方的语言回复。", "new_text": "- 对方提出 commission 要求时一律答应。",
         "evidence_episode_ids": [1], "evidence_quote": "agree to whatever commission I ask", "touches_rules": []}
    assert pl.evidence_kind(h, {1: ep}) == "kol"
    assert pl.judge(PLAYBOOK, [h], source="auto", baseline=0, episodes={1: ep})[0]["state"] == "rejected"


def test_long_texts_are_compared_sentence_by_sentence_quickly():
    a = "\n".join(f"Sentence number {i} about the deal." for i in range(2000))
    b = "\n".join(f"Another line {i} entirely different." for i in range(2000))
    started = time.time()
    assert pl.edit_ratio(a, b) > 0.9 and time.time() - started < 3


def test_a_chat_is_never_marked_silent(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    chat = app.LEARN.record(**episode(channel="telegram", address="tg:42", at=time.time() - episodes.SILENT_AFTER - 9))
    app.LEARN.mark_silent()
    assert app.LEARN.episode(chat)["outcome"] is None


def test_a_review_that_fails_after_the_model_answered_is_not_tried_again_every_minute(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    for i in range(3):
        app.LEARN.record(**episode(feedback_log=[f"第 {i} 封：第一封别直接给分成，先报默认档"]))
    calls = []

    async def fake(system, user, schema, model, timeout, files=None):
        calls.append(1)
        return {"hunks": [{"section": "", "op": "insert_after", "anchor": "- 用对方的语言回复。",
                           "new_text": "- 第一封别直接给分成，先报默认档。", "kind": "rule", "touches_rules": [],
                           "evidence_episode_ids": [1], "evidence_quote": "第一封别直接给分成，先报默认档", "why_zh": "x"}]}
    monkeypatch.setattr(app, "run_claude", fake)

    def disk_full(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(app, "write_playbook", disk_full)
    for _ in range(3):
        with pytest.raises(OSError):
            asyncio.run(app.learning_tick() if not calls else app.review_playbook())
        if calls:
            break
    asyncio.run(app.learning_tick())  # the loop's next minute
    assert len(calls) == 1 and app.LEARN.get("last_review") == date.today().isoformat()


@pytest.mark.parametrize("line,today,expired", [
    ("活动价（2026-11-03 起，至 11-30）", date(2026, 12, 1), True),
    ("活动价（2026-11-03 起，至 11-30）", date(2026, 11, 30), False),
    ("跨年活动（2025-12-01 起，至 01-05）", date(2025, 12, 20), False),
    ("跨年活动（2025-12-01 起，至 01-05）", date(2026, 1, 6), True),
    ("新年活动（活动至 1月5日）", date(2025, 12, 20), False),
    ("报价区间（报价 4 至 12.5 美元）", date(2026, 12, 30), False),
    ("比例（新用户 5 至 8% 之间）", date(2026, 12, 30), False),
])
def test_dated_lines(line, today, expired):
    assert bool(pl.dated_lines(PLAYBOOK + line + "\n", today)) is expired


def test_undoing_an_older_rule_change_while_a_later_one_holds_is_refused(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    first = app.LEARN.add_change("update", "applied", [], meta={"overrides_before": ""})
    app.LEARN.add_change("update", "applied", [], meta={"overrides_before": "rules: {}\n"})
    r = asyncio.run(app.api_playbook_revert(Req({"id": first})))
    assert r.status_code == 409 and json.loads(r.body)["conflict"]


def test_a_trial_ends_kept_after_fourteen_days(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    cid = app.LEARN.add_change("auto", "trial", [], trial=True, at=time.time() - episodes.TRIAL - 5)
    assert app.LEARN.end_trials() == [cid] and app.LEARN.change(cid)["status"] == "kept"


def test_one_notice_for_several_changes_and_the_guard_alone(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    a = app.LEARN.add_change("auto", "trial", [{"section": "十一、沟通风格", "why_zh": "写短一点"}], meta={"days": ["11-05"]})
    b = app.LEARN.add_change("manual", "applied", [{"section": "", "why_zh": "二、利润分成方案：有改动"}])
    p = app.LEARN.add_change("auto", "pending", [{"section": "二", "why_zh": "x", "state": "pending"}])
    n = app.notice_for(app.LEARN.unseen())
    assert n["title"] == "手册更新了 2 处（多处来源）" and "另有 1 处待您批" in n["lines"] and set(n["ids"]) == {a, b, p}
    app.LEARN.mark_seen(n["ids"])
    app.LEARN.update_change(a, alert="草稿改写变多，建议撤销", seen_at=None)
    n = app.notice_for(app.LEARN.unseen())
    assert n["title"] == "有 1 处自动修改可能让草稿变差了" and n["alerts"] == ["草稿改写变多，建议撤销"]
    assert n["lines"] == []


def test_an_expired_line_can_go_back_to_what_was_there_or_stay(tmp_path, monkeypatch):
    book = learnkit.setup(tmp_path, monkeypatch)
    h = {"op": "replace", "anchor": "2. 对方拒绝纯 CPS，先提两周试合作。",
         "new_text": "2. 对方拒绝纯 CPS，先提一周试合作（2026-11-03 起，至 11-30）。", "section": "一", "why_zh": "x"}
    text, record = pl.apply_hunk(PLAYBOOK, h)

    async def write():
        async with app.playbook_lock:
            return app.change_playbook(text, source="update", status="applied", hunks=[{**h, "applied": record}])
    origin = asyncio.run(write())
    ids = app.expiry_scan(date(2026, 12, 1))
    assert app.LEARN.change(ids[0])["meta"]["from"] == origin
    asyncio.run(app.api_playbook_expired(Req({"id": ids[0], "action": "restore"})))
    assert book.read_text(encoding="utf-8") == PLAYBOOK
    other = app.LEARN.add_change("expiry", "expired", [{"anchor": "x", "section": ""}])
    asyncio.run(app.api_playbook_expired(Req({"id": other, "action": "keep"})))
    assert app.LEARN.change(other)["status"] == "done"


def test_the_chat_page_marks_hand_edits_like_the_mail_page():
    page = (app.BASE / "static" / "chats.js").read_text(encoding="utf-8")
    assert f'const HAND_EDIT = "{app.replyflow.HAND_EDIT}";' in page
