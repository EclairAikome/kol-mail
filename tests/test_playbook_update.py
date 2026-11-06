"""手册更新: one sentence of the owner becomes changes of the playbook and, where a hard rule fixed the old value, of
data/rules_overrides.yaml, confirmed item by item and in force at once; dated lines that run out; /策略 from the
owner's phone only."""

import asyncio
import json
from datetime import date

import learnkit
from learnkit import Req

import app
import compliance
import mobile
import playbook_learn as pl

SWEETENER_8 = ("Hi! What is your rate for one video? We also offer a commission: 8% on new users' orders and 1% on "
               "returning users.\n\nMe")
SHARE_60 = "Hi! We can offer 60% of the net profit on every order.\n\nMe"
PUSHBACK = compliance.Context(kol_texts=["Hi, 20% is too low, other platforms give more"])


def model(monkeypatch, answer):
    calls = []

    async def fake(system, user, schema, role, timeout, files=None):
        calls.append({"system": system, "user": user, "schema": schema, "kind": app.LEARN and app.LLM_KIND.get()})
        return answer
    monkeypatch.setattr(app, "run_claude", fake)
    return calls


def rules_flagged(body: str) -> set[str]:
    return {v.rule for v in compliance.check(body, PUSHBACK, compliance.load())}


EIGHT = {
    "hunks": [
        {"section": "一", "op": "replace", "anchor": "1. 先推纯 CPS，新用户订单 GMV 的 5%，老用户 1%。",
         "new_text": "1. 先推纯 CPS，新用户订单 GMV 的 8%（2026-11-03 起，至 11-30），老用户 1%。", "kind": "rule",
         "touches_rules": ["cps-opens-at-default"], "evidence_episode_ids": [], "evidence_quote": "", "why_zh": "新用户改 8%"},
        {"section": "二", "op": "replace", "anchor": "纯 CPS 开口先报 5% 和 1% 的默认档。",
         "new_text": "纯 CPS 开口先报 8% 和 1% 的默认档。", "kind": "rule", "touches_rules": [],
         "evidence_episode_ids": [], "evidence_quote": "", "why_zh": "默认档改 8%"},
        {"section": "零", "op": "replace", "anchor": "这句不存在", "new_text": "x", "kind": "rule", "touches_rules": [],
         "evidence_episode_ids": [], "evidence_quote": "", "why_zh": "坏的"},
    ],
    "rule_changes": [{"rule_id": "", "field": "vocab", "vocab_name": "default_cps", "old": "",
                      "new": r"\b8\s?%[^\n]{0,120}\b1\s?%|\b1\s?%[^\n]{0,120}\b8\s?%", "why_zh": "默认档改成 8% 和 1%"}],
    "summary_zh": "联盟新用户改 8%",
}


def test_a_new_rule_changes_every_line_and_the_hard_rule_so_drafts_are_not_sent_back(tmp_path, monkeypatch):
    book = learnkit.setup(tmp_path, monkeypatch)
    assert "no-sweetener-before-quote" in rules_flagged(SWEETENER_8)  # before: the new rate counts as a sweetener
    calls = model(monkeypatch, EIGHT)
    r = asyncio.run(app.api_playbook_update(Req({"text": "联盟新用户改 8%（11-03 起，到 11-30）"})))
    card = json.loads(r.body)["change"]
    assert r.status_code == 200 and calls[0]["kind"] == "update" and "联盟新用户改 8%" in calls[0]["user"]
    assert [h["state"] for h in card["hunks"]] == ["pending", "pending", "rejected"]
    assert book.read_text(encoding="utf-8") == learnkit.PLAYBOOK  # nothing written before the owner says yes
    assert "这条会改动硬规则" in card["rule_changes"][0]["card_zh"] and "原：" in card["rule_changes"][0]["card_zh"]
    asyncio.run(app.api_playbook_decide(Req({"id": card["id"], "all": True, "accept": True})))
    text = book.read_text(encoding="utf-8")
    assert "8%（2026-11-03 起，至 11-30）" in text and "先报 8% 和 1%" in text and " 5%" not in text
    assert (tmp_path / "rules_overrides.yaml").exists()
    assert "no-sweetener-before-quote" not in rules_flagged(SWEETENER_8)  # after: the check keeps the new rule
    log = [c for c in app.LEARN.changes() if c["source"] == "update" and c["status"] == "applied"]
    assert log and log[0]["note"] == "联盟新用户改 8%（11-03 起，到 11-30）"  # the owner's own words are kept
    # undone: the playbook and the rule are back
    asyncio.run(app.api_playbook_revert(Req({"id": log[0]["id"]})))
    assert book.read_text(encoding="utf-8") == learnkit.PLAYBOOK
    assert "no-sweetener-before-quote" in rules_flagged(SWEETENER_8)


def test_a_new_cap_is_kept_by_the_check(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    assert "profit-share-cap" in rules_flagged(SHARE_60)
    model(monkeypatch, {"hunks": [{"section": "二", "op": "replace", "anchor": "最高到 50%。", "new_text": "最高到 60%。",
                                   "kind": "rule", "touches_rules": ["profit-share-cap"], "evidence_episode_ids": [],
                                   "evidence_quote": "", "why_zh": "上限 60%"}],
                        "rule_changes": [{"rule_id": "profit-share-cap", "field": "cap", "vocab_name": "", "old": "50",
                                          "new": "60", "why_zh": "上限 60%"}], "summary_zh": ""})
    card = json.loads(asyncio.run(app.api_playbook_update(Req({"text": "分成上限提到 60%"}))).body)["change"]
    asyncio.run(app.api_playbook_decide(Req({"id": card["id"], "rule": 0, "accept": True})))
    assert "profit-share-cap" not in rules_flagged(SHARE_60)
    assert "profit-share-cap" in rules_flagged(SHARE_60.replace("60%", "65%"))


def test_a_rule_change_the_check_could_not_use_is_refused(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    bad = [{"rule_id": "", "field": "vocab", "vocab_name": "default_cps", "old": "", "new": "([", "why_zh": "坏正则"},
           {"rule_id": "no-such-rule", "field": "cap", "vocab_name": "", "old": "1", "new": "2", "why_zh": ""}]
    out = app.clean_rule_changes(bad)
    assert len(out) == 1 and out[0]["state"] == "rejected" and "检查没通过" in out[0]["card_zh"]


def test_broken_overrides_never_switch_the_check_off(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    (tmp_path / "rules_overrides.yaml").write_text("rules:\n  profit-share-cap:\n    cap: lots\n", encoding="utf-8")
    assert "profit-share-cap" in rules_flagged(SHARE_60)
    assert "没生效" in compliance.overrides_problem()


def test_dated_lines_that_ran_out_are_reminded_not_deleted(tmp_path, monkeypatch):
    playbook = learnkit.PLAYBOOK.replace("2. 对方拒绝纯 CPS，先提两周试合作。",
                                         "2. 对方拒绝纯 CPS，先提两周试合作。\n3. 新用户 8%（2026-11-03 起，至 11-30）。")
    book = learnkit.setup(tmp_path, monkeypatch, playbook)
    assert pl.dated_lines(playbook, date(2026, 11, 30)) == []
    assert pl.dated_lines(playbook, date(2026, 12, 1))[0]["until"] == "2026-11-30"
    ids = app.expiry_scan(date(2026, 12, 1))
    assert len(ids) == 1 and app.expiry_scan(date(2026, 12, 2)) == []  # one reminder per line
    assert book.read_text(encoding="utf-8") == playbook
    asyncio.run(app.api_playbook_expired(Req({"id": ids[0], "action": "delete"})))
    assert "至 11-30" not in book.read_text(encoding="utf-8")


def test_policy_from_the_phone_is_the_owners_only(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    assert mobile.parse_command("/策略 联盟新用户改 8%") == ("policy", "联盟新用户改 8%")
    assert mobile.parse_command("策略 联盟新用户改 8%") == ("policy", "联盟新用户改 8%")
    asked = []

    async def policy(says, reply_to=None):
        asked.append(says)
    monkeypatch.setattr(app, "owner_policy", policy)
    monkeypatch.setattr(app, "owner_chat", lambda: 42)
    sent = []

    class Bot:
        async def send(self, chat, text, *a):
            sent.append(text)

    async def run(chat_id):
        await app.handle_update(Bot(), {"message": {"chat": {"id": chat_id, "type": "private"}, "message_id": 1,
                                                    "text": "/策略 联盟新用户改 8%"}})
        await asyncio.sleep(0.05)
    asyncio.run(run(7))   # a stranger: nothing at all
    assert asked == [] and sent == []
    asyncio.run(run(42))
    assert asked == ["联盟新用户改 8%"]


def test_playbook_buttons_only_work_on_a_pending_card(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    answers = []

    class Bot:
        async def answer(self, qid, text):
            answers.append(text)
    asyncio.run(app.on_playbook_button(Bot(), {"id": "q"}, ["pb", "y", "999", "0"], None))
    assert answers == ["这条已经处理过了"]


def test_rule_changes_that_would_switch_red_lines_off_are_marked_and_never_bulk_accepted(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    out = app.clean_rule_changes([
        {"rule_id": "", "field": "vocab", "vocab_name": "negation", "old": "", "new": ".", "why_zh": "x"},
        {"rule_id": "profit-share-cap", "field": "cap", "vocab_name": "", "old": "50", "new": "nan", "why_zh": "x"},
        {"rule_id": "payment-after-publication", "field": "disabled", "vocab_name": "", "old": "", "new": "true",
         "why_zh": "x"}])
    assert len(out) == 1 and out[0]["redline"] and out[0]["card_zh"].startswith("⚠ 这条会关闭硬规则检查")
    cid = app.LEARN.add_change("update", "pending", [], meta={"rule_changes": out})
    asyncio.run(app.api_playbook_decide(Req({"id": cid, "all": True, "accept": True})))
    assert not (tmp_path / "rules_overrides.yaml").exists()  # bulk "yes" left it alone
    asyncio.run(app.api_playbook_decide(Req({"id": cid, "rule": 0, "accept": True})))
    assert "payment-after-publication" not in {r["id"] for r in compliance.load().rules}
    view = json.loads(asyncio.run(app.api_playbook_changes(Req(method="GET"))).body)
    assert any("已关闭检查" in line for line in view["overrides"])
    asyncio.run(app.api_playbook_overrides_reset(Req({})))
    assert "payment-after-publication" in {r["id"] for r in compliance.load().rules}


def test_an_owner_who_allowed_a_red_line_in_words_makes_an_exception(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    allowed = app.record_episode({"ai_body": "x", "notes": "这次破例，可以先付一半"}, channel="email",
                                 address="a@example.org", sent="Hi! We settle half on signing.\n\nMe")
    refused = app.record_episode({"ai_body": "x", "feedback_log": ["别提预付"]}, channel="email",
                                 address="a@example.org", sent="Hi!\n\nMe")
    assert app.LEARN.episode(allowed)["exception"] and not app.LEARN.episode(refused)["exception"]
