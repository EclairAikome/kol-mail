"""compliance.py and the rule file: every rule of rules/negotiation_rules.yaml catches what it is about and lets the
compliant wording through, and app.py rewrites a draft that breaks one at most max_revisions times before flagging
it. The real past drafts these rules come from are checked in tests/private/ (kept out of git)."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import compliance as c  # noqa: E402
import mobile  # noqa: E402
import replyflow  # noqa: E402

GUIDE = "https://lootbar.com/affiliate/guide"
PLAYBOOK = f"Join the affiliate program at {GUIDE}."


def found(body: str, **ctx) -> set[str]:
    ctx.setdefault("playbook", PLAYBOOK)
    return {v.rule for v in c.check(body, c.Context(**ctx))}


# (rule, a draft that breaks it, the same move done right, context of the bad one, context of the good one)
CASES = [
    ("payment-after-publication", "We can pay 50% upfront and the rest after the video is live.",
     "Payment is made in full after the video goes live.", {}, {}),
    ("payment-after-publication", "50% после согласования черновика, 50% после выхода ролика.",
     "Оплату до публикации сделать не сможем, оплата полностью после выхода ролика.", {}, {}),
    ("no-bank-transfer", "We can also send it by bank transfer.", "All payments go through the Task Center.", {}, {}),
    ("no-paypal-unasked", "You can also withdraw to PayPal.", "You can also withdraw to PayPal.",
     {"kol_texts": ["What is your offer?"]}, {"kol_texts": ["Can I get paid with PayPal?"]}),
    ("no-asking-payment-method", "How would you like to receive the payment?",
     "After the video is live, you submit its link in the Task Center and the money goes to your balance.", {}, {}),
    ("no-discount-code-promise", "Your viewers get an exclusive 10% discount code.",
     "We can't set up a discount code right now, but we can give your fans free coupons for a giveaway.", {}, {}),
    ("aff-signup-not-utm", "Here's your link: https://lootbar.com/top-up/game?utm_source=me",
     f"Join at {GUIDE} and send me your link once it's set up.", {}, {}),
    ("task-center-not-for-joining", "Please join the affiliate program through the Task Center.",
     "Once the video is live, you collect the payment in the Task Center.", {}, {}),
    ("trial-no-guarantee", "After two weeks you will get a fixed fee.",
     "After two weeks we review the results, and this doesn't guarantee a fixed fee.", {}, {}),
    ("profit-share-cap", "We can offer 60% of the net profit.", "We can offer 50% of the net profit.",
     {"our_texts": ["You get 30% of the net profit."]}, {"our_texts": ["You get 30% of the net profit."]}),
    ("cps-opens-at-default", "You get 20% of the net profit on every order through your link.",
     "You get 20% of the net profit on every order through your link.",
     {"kol_texts": ["What do you offer?"]}, {"kol_texts": ["Another site gives me 10%, that is too low."]}),
    ("no-order-level-visibility", "You can see every order in your dashboard.",
     "You can see your earnings in the dashboard.", {}, {}),
    ("no-sweetener-before-quote", "What's your rate? We can also add commission on top of the fee.",
     "What's your rate? We can also add commission on top of the fee.",
     {"kol_texts": ["Sounds good!"]}, {"kol_texts": ["My rate is 200 USD per video."]}),
    ("no-extra-banner-promise", "I'll send you the banner later.", "The banner is at the end of the brief.", {}, {}),
    ("no-attachment-claims", "Please see the attached brief.", "Please see the attached brief.",
     {}, {"outgoing_attachments": True}),
    ("no-unlisted-links", "Details: https://example.com/made-up-page", "Details: https://example.com/made-up-page",
     {}, {"owner_texts": ["发这个链接 https://example.com/made-up-page"]}),
    ("no-official-authorization-claim", "We are an officially authorized partner of the developer.",
     "We are not officially authorized, LootBar is a third-party platform.", {}, {}),
    ("withdrawal-fees-truthful", "Withdrawals are completely free of any fees.",
     "The withdrawal fee from your balance is paid by you.", {}, {}),
    ("no-country-value-judgment", "Viewers from Brazil aren't worth that much to us.",
     "We based the offer on past partnerships with similar creators.", {}, {}),
    ("no-internal-terms", "That works out to a CPM of 40.", "That is above what we can get approved.", {}, {}),
    ("no-giveaway-for-excluded-games", "We can run a giveaway for your fans.", "We can run a giveaway for your fans.",
     {"subject": "Genshin Impact collaboration"}, {"subject": "Collaboration"}),
    ("no-reasking-received-data", "Could you send a screenshot of your audience analytics?",
     "Could you send a screenshot of your audience analytics?",
     {"kol_attachments": ["stats.png"]}, {"kol_attachments": []}),
    ("owner-wording-kept", "Our rating is 4.8 on Trustpilot.",
     "Rated 4.8 on Trustpilot with over 50,000 reviews, far above other sites.",
     {"owner_texts": ["我们在trustpilot有5w条评论，评分高达4.8，远高于同行。就这么回他。"],
      "zh": "我们在 Trustpilot 的评分是 4.8。"},
     {"owner_texts": ["我们在trustpilot有5w条评论，评分高达4.8，远高于同行。就这么回他。"],
      "zh": "Trustpilot 评分 4.8，超过 50,000 条评价，远高于同行。"}),
    ("amounts-listed-for-owner", "We can offer 150 USD for two videos.", "We can offer 150 USD for two videos.",
     {}, {"needs_approval": "- 两条 150 美元，需审批"}),
    ("coupon-count-listed-for-owner", "We can give your fans 2 free coupons.", "We can give your fans 2 free coupons.",
     {}, {"needs_approval": "- 券给 2 张"}),
    ("handle-cannot-change-reminder", "Set up your link at https://lootbar.com/account/affiliate/ and send it to me.",
     "Set up your link at https://lootbar.com/account/affiliate/, the handle can't be changed after saving.",
     {"playbook": "https://lootbar.com/account/affiliate/"}, {"playbook": "https://lootbar.com/account/affiliate/"}),
    ("no-uid-for-link", "Could you send me your LootBar UID so I can set things up?",
     "For the Task Center payout, could you send me your LootBar UID?", {}, {}),
    ("no-confirming-unapproved-amount", "Deal, 300 USD per video works for us.",
     "Deal, 300 USD per video works for us.", {}, {"owner_texts": ["300 可以，接受"]}),
    ("no-ban-guarantee", "With LootBar you will never get banned.",
     "Normal purchases follow a strict safety process and generally don't lead to bans.", {}, {}),
    ("guarantee-terms-for-owner", "We can add a guaranteed minimum commission each month.",
     "We can add a commission on every order.", {}, {}),
    ("no-casual-congrats-on-life-change", "Congrats on going full-time!", "Congrats on the new video!",
     {"kol_texts": ["I quit my job on Friday."]}, {"kol_texts": ["Here is my new video."]}),
]


@pytest.mark.parametrize("rule,bad,good,bad_ctx,good_ctx", CASES, ids=[f"{x[0]}-{i}" for i, x in enumerate(CASES)])
def test_each_rule_catches_the_breach_and_passes_the_right_wording(rule, bad, good, bad_ctx, good_ctx):
    assert rule in found(bad, **bad_ctx)
    assert rule not in found(good, **good_ctx)


def test_every_rule_in_the_file_has_a_case_here():
    assert {r["id"] for r in c.load().rules} == {x[0] for x in CASES}


def test_the_rule_file_loads_and_says_what_to_fix():
    rules = c.load()
    assert rules.max_revisions == 2
    for r in rules.rules:
        assert r["action"] in c.ACTIONS and r["title_zh"] and r["source"]
        assert r["action"] == "review" or len(r["fix"]) > 20


@pytest.mark.parametrize("change,message", [
    ({"check": "nope"}, "没有对应的检查"),
    ({"action": "maybe"}, "action"),
    ({"fix": ""}, "没写 fix"),
    ({"patterns": ["(unclosed"]}, "正则写错了"),
])
def test_a_broken_rule_file_is_refused_with_a_reason(change, message):
    rule = {"id": "x", "title_zh": "x", "action": "revise", "check": "patterns", "fix": "do it", "patterns": ["a"]}
    with pytest.raises(c.RulesError, match=message):
        c.parse({"rules": [{**rule, **change}]})


def test_the_rule_file_is_read_again_after_an_edit(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("rules:\n  - {id: a, title_zh: A, action: revise, check: patterns, fix: f, patterns: ['x']}\n",
                    encoding="utf-8")
    assert [r["id"] for r in c.load(path).rules] == ["a"]
    import os
    path.write_text("rules:\n  - {id: b, title_zh: B, action: revise, check: patterns, fix: f, patterns: ['y']}\n",
                    encoding="utf-8")
    os.utime(path, ns=(1, 10**18))  # a new mtime even on a coarse clock
    assert [r["id"] for r in c.load(path).rules] == ["b"]


def test_quoted_history_and_titles_are_not_the_draft_or_the_kol():
    assert c.own_words("Can we talk?\n\nOn Mon, Eclair wrote:\n> PayPal works too") == "Can we talk?\n"
    # a KOL's video title quoted in a compliment is not a promise
    assert "no-discount-code-promise" not in found('I enjoyed your video "Every discount code tier list".')


def test_signature_is_not_checked():
    assert not found("Thanks!\n\nBest,\nMe\nhttps://example.com/x", signature="Best,\nMe\nhttps://example.com/x")


def test_numbers_are_read_as_people_write_them():
    assert c.numbers_in("Рейтинг 4,8, больше 53 000 отзывов, 53,465 в сумме, 100%") == [4.8, 53000, 53465, 100]
    assert c.owner_numbers("5w条评论，评分4.8，17:00 前") == [50000, 4.8]


def test_revision_brief_names_the_rule_the_evidence_and_the_fix():
    v = c.check("I'll send you the banner later.", c.Context())
    brief = c.revision_brief(v)
    assert "Rule no-extra-banner-promise" in brief and "I'll send you the banner later." in brief
    assert "end of the game" in brief


# ---------------------------------------------------------------- the draft pipeline in app.py

BAD = "Hi!\n\nWe can pay 50% upfront and I'll send you the banner later.\n\n" + app.SIGNATURE
GOOD = "Hi!\n\nPayment is made in full after the video goes live. The banner is at the end of the brief.\n\n" + \
    app.SIGNATURE


class Calls(list):
    """The draft calls in order; .checks holds the owner checks (owner_check) in order."""
    checks: list


def fake_model(monkeypatch, bodies: list[str], fail_from: int = 0, verdicts=None, check_fails: bool = False):
    """run_claude faked: drafts come from bodies in turn (from call fail_from on, the model fails); each
    back-translation is Chinese with its paragraph echoed, so a side-by-side shows which text it belongs to. The
    owner check answers from verdicts in turn, each (rule ids the owner asked for, instructions missed); by default
    the owner asked for none of the flags and nothing is missed. check_fails: the owner check fails."""
    import json
    calls = Calls()
    calls.checks = []

    async def fake_claude(system, user_text, schema, model, timeout, files=None):
        if schema is replyflow.PARAGRAPHS_SCHEMA:
            paragraphs = json.loads(user_text.split(">>\n", 1)[1].rsplit("\n<</", 1)[0])
            return {"zh": [f"这是这一段话的中文翻译内容，原文是：{p}" for p in paragraphs]}
        if schema is replyflow.OWNER_CHECK_SCHEMA:
            calls.checks.append(user_text)
            if check_fails:
                raise app.ClaudeError("超时")
            allowed, missed = (verdicts or [((), [])])[min(len(calls.checks), len(verdicts or [0])) - 1]
            flags = json.loads(user_text.split(":FLAGS>>\n", 1)[1].rsplit("\n<</", 1)[0])
            return {"flags": [{"id": f["id"], "owner_asked": f["id"] in allowed, "why_zh": "x"} for f in flags],
                    "missed": list(missed)}
        calls.append(user_text)
        if fail_from and len(calls) >= fail_from:
            raise app.ClaudeError("超时")
        return {"reply_language": "English", "reply_body": bodies[min(len(calls), len(bodies)) - 1],
                "notes_zh": f"第 {len(calls)} 版", "needs_approval_zh": "无"}
    monkeypatch.setattr(app, "run_claude", fake_claude)
    monkeypatch.setattr(app, "read_file", lambda path, what: PLAYBOOK)
    return calls


CTX = {"channel": "email", "msg": {"date": "d", "sender": "a@example.com", "subject": "Hi", "body": "Rates?"},
       "earlier": [], "ours": [], "materials": []}


def test_a_breaking_draft_is_rewritten_until_it_complies(monkeypatch):
    calls = fake_model(monkeypatch, [BAD, GOOD])
    draft, pairs = asyncio.run(app.checked_draft(CTX, notes="回他"))
    assert draft["reply_body"] == GOOD and pairs["ok"]
    assert draft["compliance"] == {"checked": True, "ok": True, "revisions": 1, "broken": [], "waived": [],
                                   "missed": [],
                                   "fixed": [r["title_zh"] for r in c.load().rules
                                             if r["id"] in ("payment-after-publication", "no-extra-banner-promise")]}
    assert any(w.startswith("合规检查自动改写了 1 次") for w in draft["warnings"])
    assert draft["notes_zh"] == "第 1 版"  # what the version does, not the rewrite's note
    rewrite = calls[1]
    assert rewrite.startswith("MODE: comply") and "COMPLIANCE_FEEDBACK>>" in rewrite and "PREVIOUS_DRAFT>>" in rewrite
    assert "Rule payment-after-publication" in rewrite and "OWNER_NOTES>>\n回他" in rewrite


def test_a_draft_still_breaking_after_two_rewrites_is_flagged(monkeypatch):
    calls = fake_model(monkeypatch, [BAD])
    draft, _ = asyncio.run(app.checked_draft(CTX, notes="回他"))
    assert len(calls) == 3  # the draft and two rewrites, no more
    assert draft["compliance"]["ok"] is False and draft["compliance"]["revisions"] == 2
    assert {b["rule"] for b in draft["compliance"]["broken"]} == {"payment-after-publication", "no-extra-banner-promise"}
    assert any(w.startswith("⚠️ 合规检查没通过（自动改写 2 次后仍违反）") for w in draft["warnings"])


def test_review_rules_go_to_the_owner_not_back_to_the_model(monkeypatch):
    calls = fake_model(monkeypatch, ["Hi!\n\nWe can offer 150 USD for two videos.\n\n" + app.SIGNATURE])
    draft, _ = asyncio.run(app.checked_draft(CTX, notes="回他"))
    assert len(calls) == 1 and draft["compliance"]["ok"]
    assert "草稿里写了金额 150 USD" in draft["needs_approval_zh"]


def test_an_amount_the_owner_gave_is_not_listed_for_approval(monkeypatch):
    fake_model(monkeypatch, ["Hi!\n\nWe can offer 150 USD for two videos and 2 free coupons.\n\n" + app.SIGNATURE])
    draft, _ = asyncio.run(app.checked_draft(CTX, notes="报 150 美元两条，送 2 张券"))
    assert "150" not in draft["needs_approval_zh"] and "张数" not in draft["needs_approval_zh"]


def test_an_unreadable_rule_file_never_stops_a_draft(monkeypatch, tmp_path):
    fake_model(monkeypatch, [GOOD])
    monkeypatch.setattr(c, "RULES_FILE", tmp_path / "missing.yaml")
    draft, pairs = asyncio.run(app.checked_draft(CTX))
    assert draft["reply_body"] == GOOD and pairs["ok"]
    assert draft["compliance"]["checked"] is False and any("这版没做合规检查" in w for w in draft["warnings"])


def test_the_phone_draft_shows_each_paragraph_with_its_chinese():
    rows = [{"orig": "Hi Ana!", "zh": "你好 Ana！", "same": False},
            {"orig": "https://example.com/x", "zh": "https://example.com/x", "same": True},
            {"orig": "Payment after publication.", "zh": "", "same": False, "failed": True}]
    text = mobile.draft_text({"id": 3, "who": "Ana"}, rows, False, "", "无", [])
    assert "Hi Ana!\n〔中文〕你好 Ana！" in text
    assert "https://example.com/x\n\n" in text and "〔中文〕https" not in text
    assert "Payment after publication.\n【这一段没能生成可信的中文对照】" in text


def test_the_send_confirmation_shows_the_original_and_the_chinese():
    text = mobile.confirm_text({"id": 3, "channel": "email", "body": "Hi Ana!"}, "a@example.com", "Re: Hi", "你好 Ana！")
    assert "要发出的原文：\nHi Ana!" in text and "中文对照：\n你好 Ana！" in text


def revise_rules(body: str, **ctx) -> set[str]:
    ctx.setdefault("playbook", PLAYBOOK)
    return {v.rule for v in c.check(body, c.Context(**ctx)) if v.action == "revise"}


@pytest.mark.parametrize("feedback", ["把 Trustpilot 那句去掉", "去掉 5% 佣金那段", "不提 PayPal", "这里为什么写了 CPS?",
                                      "把 Giveaway 拿掉", "价格改成两条 150 美元"])
def test_owner_feedback_that_removes_or_changes_something_is_not_a_claim_to_keep(feedback):
    assert "owner-wording-kept" not in found("Thanks, here is the plan.", owner_texts=[feedback])


@pytest.mark.parametrize("text", [
    "I understand you'd like a deposit, but every creator is paid in full after the video is published.",
    "Entiendo, pero el pago se hace siempre después de publicar el video.",
    "前払いは行っておりません。", "선금은 어렵습니다.", "割引コードはご用意しておりません。",
    "We'll share the brief, which has the LootBar banner at the end.",
    "En el Task Center adjunta el enlace del video y una captura del canal.",
    "Joining is completely free, and after publication the payment goes to your balance.",
    "Ganhe com um cupom grátis para um sorteio.",
    "You get 20% of the net profit, and 100% of the refund is guaranteed.",
])
def test_wordings_the_playbook_allows_are_not_flagged(text):
    assert not revise_rules(text, our_texts=["You get 20% of the net profit."])


def test_a_fixed_fee_with_a_profit_share_is_not_a_pure_cps_opening():
    """Every fixed fee closed in 2026-10 came with 20% of net profit (手册第二节, 第十三节)."""
    body = "We can offer 60 USD for the video plus 20% of the net profit on every order through your link."
    assert "cps-opens-at-default" not in revise_rules(body, kol_texts=["My rate is $200 per video."])
    assert "cps-opens-at-default" in revise_rules("You'd get 20% of the net profit on every order.",
                                                  kol_texts=["Sounds interesting, tell me more."])
    turned_down = "100 USD per video is more than I can get approved. You'd get 20% of the net profit instead."
    assert "cps-opens-at-default" in revise_rules(turned_down, kol_texts=["price 100 bucks a video?"])


def test_a_competitor_named_counts_as_pushback():
    body = "We can offer you 20% of the net profit on every order through your link."
    assert "cps-opens-at-default" not in revise_rules(
        body, kol_texts=["Mi contrato con Eneba de fijo más comisión terminó en septiembre."])


def test_the_default_affiliate_offer_is_not_a_card_shown_too_early():
    body = "What's your rate for a video? Plus our affiliate default: 5% of new users' orders and 1% of the rest."
    assert "no-sweetener-before-quote" not in revise_rules(body, kol_texts=["Hi!"])


@pytest.mark.parametrize("text", ["No worries, we can pay 50% upfront.", "Не переживайте, предоплату сделаем.",
                                  "No problem, I'll set up a promo code for you."])
def test_a_reassurance_is_not_a_refusal(text):
    assert revise_rules(text) & {"payment-after-publication", "no-discount-code-promise"}


@pytest.mark.parametrize("raw", [
    {"max_revisions": "abc", "rules": []},
    {"vocab": ["x"], "rules": []},
    {"vocab": {"negation": 5}, "rules": []},
    {"rules": [{"id": "x", "title_zh": "x", "action": "revise", "check": "patterns", "fix": "f", "patterns": 5}]},
    {"rules": [{"id": "x", "title_zh": "x", "action": "revise", "check": "profit_share_cap", "fix": "f",
                "cap": "fifty"}], "vocab": {"profit": "profit"}},
    {"rules": [{"id": "x", "title_zh": "x", "action": "revise", "check": "reminder", "fix": "f"}]},
    "just text",
])
def test_any_malformed_rule_file_is_a_rules_error(raw):
    with pytest.raises(c.RulesError):
        c.parse(raw)


def test_a_rule_file_that_is_not_yaml_mapping_is_a_rules_error(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(c.RulesError):
        c.load(path)


def test_a_long_run_of_digits_does_not_hang_the_check():
    import time
    start = time.monotonic()
    c.check("Hi. " + "1 " * 6000, c.Context(kol_texts=["2, " * 6000], our_texts=["3 " * 6000]))
    assert time.monotonic() - start < 2


def test_a_failed_rewrite_keeps_the_draft_and_flags_it(monkeypatch):
    calls = fake_model(monkeypatch, [BAD], fail_from=2)
    draft, pairs = asyncio.run(app.checked_draft(CTX, notes="回他"))
    assert len(calls) == 2 and draft["reply_body"] == BAD and pairs["rows"][1]["orig"] == BAD.split("\n\n")[1]
    assert draft["compliance"]["ok"] is False and draft["compliance"]["revisions"] == 0
    assert any(w.startswith("合规自动改写失败") for w in draft["warnings"])
    assert any(w.startswith("⚠️ 合规检查没通过") for w in draft["warnings"])


def test_a_rule_that_crashes_never_stops_the_draft(monkeypatch):
    fake_model(monkeypatch, [GOOD])

    def boom(*a, **k):
        raise RuntimeError("bad rule")
    monkeypatch.setattr(c, "check", boom)
    draft, _ = asyncio.run(app.checked_draft(CTX))
    assert draft["reply_body"] == GOOD and draft["compliance"]["checked"] is False
    assert any("检查出错" in w for w in draft["warnings"])


def test_the_side_by_side_belongs_to_the_final_text(monkeypatch):
    fake_model(monkeypatch, [BAD, GOOD])
    draft, pairs = asyncio.run(app.checked_draft(CTX))
    assert [r["orig"] for r in pairs["rows"]] == app.replyflow.split_paragraphs(GOOD)
    assert "Payment is made in full" in pairs["rows"][1]["zh"]  # the fake translation echoes its paragraph


def test_a_translation_stays_a_translation_when_rewritten(monkeypatch):
    calls = fake_model(monkeypatch, [BAD, GOOD])
    asyncio.run(app.checked_draft(CTX, mode="translate", notes="我们视频发布后付款"))
    assert calls[1].startswith("MODE: comply translate") and "OWNER_NOTES>>\n我们视频发布后付款" in calls[1]


def test_owner_feedback_goes_into_every_rewrite(monkeypatch):
    calls = fake_model(monkeypatch, [BAD, GOOD])
    asyncio.run(app.checked_draft(CTX, previous="old text", feedback="语气热情一点"))
    assert "OWNER_FEEDBACK>>\n语气热情一点" in calls[1] and "PREVIOUS_DRAFT>>\n" + BAD in calls[1]


PAY = "payment-after-publication"
BANNER = "no-extra-banner-promise"


def title(rule_id: str) -> str:
    return next(r["title_zh"] for r in c.load().rules if r["id"] == rule_id)


def test_what_the_owner_asked_for_is_never_rewritten_away(monkeypatch):
    """2026-10-29: the owner's word beats the playbook. A flagged sentence the owner asked for stays; only what the
    AI put in on its own goes back."""
    keeps_upfront = "Hi!\n\nWe can pay 50% upfront.\n\n" + app.SIGNATURE
    calls = fake_model(monkeypatch, [BAD, keeps_upfront], verdicts=[({PAY}, [])])
    draft, _ = asyncio.run(app.checked_draft(CTX, previous="old", feedback="告诉他可以先付一半"))
    assert draft["reply_body"] == keeps_upfront
    rewrite = calls[1]
    assert f"Rule {BANNER}" in rewrite and f"Rule {PAY}" not in rewrite
    assert draft["compliance"]["ok"] and draft["compliance"]["waived"] == [title(PAY)]
    assert f"已按你的意见保留：{title(PAY)}" in draft["warnings"]
    assert not any("合规检查没通过" in w for w in draft["warnings"])


def test_a_draft_that_skipped_the_owners_feedback_is_sent_back_for_it(monkeypatch):
    calls = fake_model(monkeypatch, [GOOD, GOOD], verdicts=[((), ["主人要求价格改成两条 150 美元，草稿没改"]), ((), [])])
    draft, _ = asyncio.run(app.checked_draft(CTX, previous="old", feedback="价格改成两条 150 美元"))
    assert len(calls) == 2 and calls[1].startswith("MODE: comply")
    assert "OWNER_NOT_DONE>>\n- 主人要求价格改成两条 150 美元，草稿没改" in calls[1]
    assert draft["compliance"]["ok"] and draft["compliance"]["missed"] == []


def test_feedback_still_missed_after_the_rewrites_is_said_plainly(monkeypatch):
    fake_model(monkeypatch, [GOOD], verdicts=[((), ["语气没变热情"])])
    draft, _ = asyncio.run(app.checked_draft(CTX, previous="old", feedback="语气热情一点"))
    assert draft["compliance"]["ok"] is False and draft["compliance"]["missed"] == ["语气没变热情"]
    assert any(w.startswith("⚠️ 你的意见还有没做到的（自动改写 2 次后）") for w in draft["warnings"])


def test_earlier_rounds_of_feedback_still_hold(monkeypatch):
    calls = fake_model(monkeypatch, [BAD, GOOD])
    asyncio.run(app.checked_draft(CTX, notes="回他", previous="old", feedback="第三轮", history=["第一轮", "第二轮"]))
    assert "OWNER_EARLIER_FEEDBACK>>\nRound 1:\n第一轮\n\nRound 2:\n第二轮" in calls[0]
    assert "OWNER_EARLIER_FEEDBACK>>" in calls[1]  # the rewrite keeps them too
    assert '["回他", "第一轮", "第二轮", "第三轮"]' in calls.checks[0]


def test_when_the_owner_check_fails_nothing_is_rewritten(monkeypatch):
    calls = fake_model(monkeypatch, [BAD, GOOD], check_fails=True)
    draft, _ = asyncio.run(app.checked_draft(CTX, previous="old", feedback="告诉他可以先付一半"))
    assert len(calls) == 1 and draft["reply_body"] == BAD
    assert draft["compliance"]["ok"] is False and draft["compliance"]["revisions"] == 0
    assert any(w.startswith("核对“是否照你的意见改”时出错") for w in draft["warnings"])
    assert any(w.startswith("规则检查提示（是你要求的就不用管）") for w in draft["warnings"])
    assert not any("合规检查没通过" in w for w in draft["warnings"])


def test_the_owner_check_replaces_the_wording_regex(monkeypatch):
    """The regex check of the owner's wording was the rule tripping most often (11 of 27 rewrites by 2026-10-29):
    with the owner check running it never sends a draft back on its own."""
    fake_model(monkeypatch, ["Hi!\n\nThanks for your reply.\n\n" + app.SIGNATURE])
    draft, _ = asyncio.run(app.checked_draft(CTX, notes="强调我们是全网最低价"))
    assert draft["compliance"]["ok"] and draft["compliance"]["revisions"] == 0


def test_the_wording_regex_is_never_put_to_the_owner_check(monkeypatch):
    calls = fake_model(monkeypatch, ["Hi!\n\nThanks for your reply.\n\n" + app.SIGNATURE], verdicts=[({"owner-wording-kept"}, [])])
    draft, _ = asyncio.run(app.checked_draft(CTX, notes="强调我们是全网最低价"))
    assert "owner-wording-kept" not in calls.checks[0] and draft["compliance"]["waived"] == []


def test_a_waiver_is_only_reported_for_what_the_final_draft_still_says(monkeypatch):
    fake_model(monkeypatch, [BAD, GOOD], verdicts=[({PAY}, [])])
    draft, _ = asyncio.run(app.checked_draft(CTX, previous="old", feedback="告诉他可以先付一半"))
    assert draft["reply_body"] == GOOD and draft["compliance"]["waived"] == []
    assert not any(w.startswith("已按你的意见保留") for w in draft["warnings"])


def test_the_page_marks_hand_edits_with_the_same_words_the_prompts_explain():
    page = (Path(app.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert f'const HAND_EDIT = "{replyflow.HAND_EDIT}";' in page
    system = replyflow.draft_system(owner="Me", owner_role="r", company="C", boundary="b", playbook="p", signature="Me")
    assert replyflow.HAND_EDIT in system and replyflow.HAND_EDIT in replyflow.OWNER_CHECK_SYSTEM


def test_a_phone_draft_keeps_the_owners_notes_as_their_words(monkeypatch):
    """Notes typed on the phone count in every later round, as feedback does, and a fresh draft starts over."""
    async def context(case):
        return CTX

    async def drafted(ctx, **kwargs):
        return {"reply_body": GOOD, "warnings": []}, {"ok": True, "rows": []}

    async def pushed(*args, **kwargs):
        return None
    monkeypatch.setattr(app, "case_context", context)
    monkeypatch.setattr(app, "checked_draft", drafted)
    monkeypatch.setattr(app, "store_and_push_draft", pushed)
    app.remember_case_feedback(9101, ["an old round"])
    asyncio.run(app.draft_case({"id": 9101}, notes="告诉他可以先付一半"))
    assert app.case_feedback(9101) == ["告诉他可以先付一半"]
    asyncio.run(app.draft_case({"id": 9101}, strategy={"title_zh": "t", "approach_zh": "a"}))
    assert app.case_feedback(9101) == []


def test_a_draft_without_owner_words_is_not_checked_against_them(monkeypatch):
    calls = fake_model(monkeypatch, [BAD, GOOD])
    asyncio.run(app.checked_draft(CTX, strategy={"title_zh": "t", "approach_zh": "a"}))
    assert calls.checks == [] and len(calls) == 2


def test_the_owner_verdict_only_takes_flags_it_was_shown():
    allowed, missed = replyflow.owner_verdict(
        {"flags": [{"id": PAY, "owner_asked": True}, {"id": "made-up", "owner_asked": True},
                   {"id": BANNER, "owner_asked": "yes"}], "missed": ["a", "a", " ", 5, "b <ip_reminder>x</ip_reminder>"]},
        {PAY, BANNER})
    assert allowed == {PAY} and missed == ["a", "b"]


def test_every_prompt_puts_the_owner_first():
    for make in (replyflow.draft_system, replyflow.compose_system, replyflow.chat_system):
        system = make(owner="Me", owner_role="r", company="C", boundary="b", playbook="p", signature="Me")
        assert "Who decides, highest first" in system and "Me wins" in system
        assert "Unless Me's own words ask otherwise" in system
    strategy = replyflow.strategy_system(owner="Me", owner_role="r", company="C", boundary="b", playbook="p")
    assert "person_zh" in strategy and "not a default first move" in strategy


def test_check_only_flags_without_rewriting(monkeypatch):
    calls = fake_model(monkeypatch, [GOOD])
    draft, _ = asyncio.run(app.enforce_rules(CTX, {"reply_body": BAD, "notes_zh": "重新生成了中文对照",
                                                   "needs_approval_zh": "", "warnings": []}, rewrites=0))
    assert calls == [] and draft["reply_body"] == BAD and draft["compliance"]["ok"] is False


@pytest.mark.parametrize("notes,body,zh", [
    ("她是法国人，粉丝 20w，问她报价", "Hi Marie! Could you tell us your rate?", "您好 Marie！能告诉我们您的报价吗？"),
    ("可以给他粉丝搞个 giveaway", "Nous pouvons organiser un concours pour vos abonnés.", "我们可以为您的订阅者组织一次抽奖。"),
    ("预算最多 300，先别说，问他报价", "Could you share your rate for one video?", "您能告诉我们一条视频的报价吗？"),
    ("我们打包可以给到 3k", "We can offer up to $3K for the package.", "打包我们最多可以给到 3K 美元。"),
    ("我们的 giveaway 奖品很丰厚", "Les lots de notre concours sont généreux.", "我们抽奖的奖品很丰厚。"),
])
def test_background_asks_and_in_house_numbers_are_not_claims_to_keep(notes, body, zh):
    """Seen on 2026-10-27: notes like these failed every rewrite, because any number or English word counted."""
    assert "owner-wording-kept" not in found(body, owner_texts=[notes], zh=zh)


@pytest.mark.parametrize("notes,body,zh", [
    ("我们是全网最低价", "LootBar offers some of the best prices on the market.", "LootBar 提供市场上最优惠的价格之一。"),
    ("我们在trustpilot有5w条评论", "Thanks!", "谢谢！"),
    ("强调 LootBar 很安全", "Nuestra plataforma es segura.", "我们的平台很安全。"),
])
def test_selling_points_and_claims_about_us_must_still_be_kept(notes, body, zh):
    assert "owner-wording-kept" in found(body, owner_texts=[notes], zh=zh)


def test_every_prompt_carries_the_hard_rules():
    brief = c.prompt_brief()
    assert brief.count("\n") + 1 == len(c.load().rules) and "纯 CPS 先报默认档" in brief
    args = {"owner": "Eclair", "owner_role": "BD", "company": "LootBar", "boundary": "b", "playbook": "",
            "hard_rules": brief}
    for system in (replyflow.strategy_system(**args), replyflow.draft_system(**args, signature="E"),
                   replyflow.chat_system(**args, signature="E")):
        assert "Hard rules" in system and "纯 CPS 先报默认档" in system
    assert "Hard rules" not in replyflow.draft_system(**{**args, "hard_rules": ""}, signature="E")


@pytest.mark.parametrize("notes,kept", [
    ("预算最多 300，先别说，但我们的佣金远高于同行", ["但我们的佣金远高于同行"]),
    ("我们保证数据保密", ["我们保证数据保密"]),
    ("我们不提供保底", ["我们不提供保底"]),
    ("接受 120 美元一条，要求两条打包", ["接受 120 美元一条"]),
    ("CPS 佣金 12%", ["CPS 佣金 12%"]),
    ("他报价 500 美元，太贵", []),
    ("预算 300 美元", []),
])
def test_what_counts_as_a_claim_clause_by_clause(notes, kept):
    rule = next(r for r in c.load().rules if r["id"] == "owner-wording-kept")
    assert c.owner_claims(notes, rule)[1] == kept


def test_a_price_the_owner_gave_must_be_the_one_in_the_draft():
    notes = ["接受 120 美元一条，要求两条打包"]
    assert "owner-wording-kept" in found("We can do 100 USD per video.", owner_texts=notes, zh="每条 100 美元。")
    assert "owner-wording-kept" not in found("We can do 120 USD per video.", owner_texts=notes, zh="每条 120 美元。")


def test_k_in_a_word_is_not_thousands():
    assert c.amounts_in("20 K-pop fans, a 3K budget") == [20.0, 3.0, 3000.0]
