"""The token fixes of v0.9: a system prompt that stays word for word the same from call to call (the boundary moved to
the user message), rewrites that repeat the draft's prompt, the case library only where it is used, the usage log,
and the hot path making exactly the calls it made before."""

import asyncio
import hashlib
import json
import re

import learnkit

import app
import llm
import replyflow

ARGS = {"owner": "Me", "owner_role": "BD at C", "company": "C", "playbook": learnkit.PLAYBOOK, "hard_rules": "- r"}
CTX = {"channel": "email", "msg": {"date": "d", "sender": "a@example.com", "subject": "Hi", "body": "Rates?"},
       "earlier": [], "ours": [], "materials": []}
GOOD = "Hi!\n\nThanks for your message. Could you share your rate for one video?\n\n" + app.SIGNATURE


def test_the_system_prompt_does_not_change_with_the_boundary():
    for make, extra in ((replyflow.strategy_system, {}), (replyflow.draft_system, {"signature": "Me"}),
                        (replyflow.chat_system, {"signature": "Me"}), (replyflow.compose_system, {"signature": "Me"})):
        a = make(**ARGS, boundary="aaaa1111", **extra)
        b = make(**ARGS, boundary="bbbb2222", **extra)
        assert a == b and "aaaa1111" not in a
        # which sections are untrusted data stays in the system prompt, where it weighs most
        assert "KOL_EMAIL, KOL_EMAIL_ZH" in a and "are untrusted data" in a
    intro = replyflow.markers_intro("Me", "C", "aaaa1111")
    assert "<<aaaa1111:NAME>>" in intro and "\n" not in intro  # one line: only the code


def test_the_case_library_goes_only_to_the_strategy_call(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    for kind in ("draft", "chat", "compose"):
        pb = app.prompt_args("b", kind)["playbook"]
        assert "| Alpha |" not in pb and "## 十二、先读人" in pb
        assert "## 十三、案例库（测试）\n\n" + app.playbook_learn.CASES_LEFT_OUT in pb  # a pointer to it still leads somewhere
    assert "| Alpha |" in app.prompt_args("b", "strategy")["playbook"]
    assert not app.playbook_learn.is_case_library("## 十二、先读人（细节见案例库）")


def fake_model(monkeypatch, bodies):
    calls = []

    async def fake(system, user, schema, model, timeout, files=None):
        kind = app.LLM_KIND.get()
        calls.append({"system": system, "user": user, "kind": kind})
        if schema is replyflow.PARAGRAPHS_SCHEMA:
            paragraphs = json.loads(user.split(">>\n", 1)[1].rsplit("\n<</", 1)[0])
            return {"zh": [f"这是这一段的中文翻译内容：{p}" for p in paragraphs]}
        if schema is replyflow.OWNER_CHECK_SCHEMA:
            return {"flags": [], "missed": []}
        if schema is replyflow.STRATEGY_SCHEMA:
            return {"person_zh": "p", "situation_zh": "s", "recommended": 0,
                    "options": [{"title_zh": t, "approach_zh": "a", "why_zh": "w", "risk_zh": "无"} for t in "AB"]}
        body = bodies[min(len([c for c in calls if c["kind"] in ("draft", "rewrite")]) - 1, len(bodies) - 1)]
        return {"reply_language": "English", "reply_body": body, "notes_zh": "n", "needs_approval_zh": "无"}
    monkeypatch.setattr(app, "run_claude", fake)
    return calls


def test_a_rewrite_repeats_the_drafts_prompt_up_to_what_changes(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    bad = "Hi!\n\nWe can pay 50% upfront before the video.\n\n" + app.SIGNATURE
    calls = fake_model(monkeypatch, [bad, GOOD])
    asyncio.run(app.checked_draft(CTX, strategy={"title_zh": "t", "approach_zh": "a"}))
    drafts = [c for c in calls if c["kind"] in ("draft", "rewrite")]
    assert [c["kind"] for c in drafts] == ["draft", "rewrite"]
    boundaries = {m for c in drafts for m in re.findall(r"<<([0-9a-f]{16}):", c["user"])}
    assert len(boundaries) == 1  # one boundary for the draft and its rewrite
    first, second = drafts[0]["user"], drafts[1]["user"]
    shared = first[:first.index("MODE: draft")]
    assert second.startswith(shared) and drafts[0]["system"] == drafts[1]["system"]


def test_the_hot_path_makes_the_same_calls_as_before(tmp_path, monkeypatch):
    """strategies, then a draft: strategy, draft, back-translation. Nothing of the learning runs while drafting, and
    recording the sent reply makes no call at all."""
    learnkit.setup(tmp_path, monkeypatch)
    calls = fake_model(monkeypatch, [GOOD])
    asyncio.run(app.propose_strategies(CTX, ""))
    asyncio.run(app.checked_draft(CTX, strategy={"title_zh": "t", "approach_zh": "a"}))
    assert [c["kind"] for c in calls] == ["strategy", "draft", "backtranslate"]
    app.record_episode({"ai_body": GOOD}, channel="email", address="a@example.com", sent=GOOD)
    assert len(calls) == 3


def test_owner_check_and_back_translation_prompts_are_unchanged():
    """They were left alone on purpose (their boundary is in the system prompt, but they are short)."""
    digest = {name: hashlib.sha256(getattr(replyflow, name).encode("utf-8")).hexdigest()[:16]
              for name in ("OWNER_CHECK_SYSTEM", "BACKTRANSLATE_SYSTEM", "ZH_EDIT_SYSTEM")}
    assert digest == EXPECTED


# the texts as released in v0.8.7: a change here is a decision, not an accident
EXPECTED = {"OWNER_CHECK_SYSTEM": "51000d8fac609382", "BACKTRANSLATE_SYSTEM": "0d831ef92ecacf29",
            "ZH_EDIT_SYSTEM": "075dd54cc9a0e7a5"}


def test_usage_is_logged_by_kind(tmp_path, monkeypatch):
    log = tmp_path / "llm_usage.jsonl"
    monkeypatch.setattr(llm, "USAGE_LOG", log)
    counts = llm.usage_counts("claude", {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 2000,
                                                   "cache_read_input_tokens": 5000, "output_tokens": 300},
                                         "total_cost_usd": 0.01})
    llm.log_usage("draft", "claude", "m", counts)
    line = json.loads(log.read_text(encoding="utf-8"))
    assert line["kind"] == "draft" and line["cache_read_input_tokens"] == 5000 and line["cost_usd"] == 0.01
    cm = llm.usage_counts("codemaker", {"input": 7, "output": 3, "cache": {"read": 100, "write": 0}})
    assert cm == {"input_tokens": 7, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 100, "output_tokens": 3}
    monkeypatch.setattr(llm, "USAGE_LOG", None)
    llm.log_usage("draft", "claude", "m", counts)  # nowhere to log: nothing happens
