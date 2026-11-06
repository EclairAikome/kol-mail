"""The playbook learns from its own user: what they changed in an AI draft before sending it, what they told the AI,
and how the KOL answered. Nothing is trained. A review now and then (at most once a day, when the drafts are idle)
gives a model the playbook and the recent edits and asks for a few small changes (hunks) to the playbook text; the
code below decides which of them may be written at once, which wait for the user's yes, and which are thrown away.

Rules this module holds, whatever a model returns:
- The playbook stays the one source of truth and is only ever changed in place, hunk by hunk: an anchor that occurs
  exactly once is replaced, followed by new text, or deleted. A whole rewrite is never accepted (a model that
  rewrites a long text loses parts of it).
- "我的谈判方式" and "零、两条红线" are the user's own: an automatic change never touches them.
- A change that touches a red line (payment timing, bank transfer, PayPal, discount codes, order-level data, official
  authorization, the profit share cap, any changed percentage) waits for the user, as does one backed only by how a
  negotiation ended, and one that would grow the playbook past its budget.
- Evidence must point at the user's own words or edits; a change backed only by what a KOL wrote is thrown away.

Pure text helpers only: app.py reads and writes the files, episodes.py keeps the records, replyflow.py holds the
prompts."""

from __future__ import annotations

import difflib
import re
import unicodedata
from datetime import date

import onboarding

MAX_HUNKS = 6
NEW_TEXT_MAX = 1500
AUTO_ANCHOR_MAX = 400       # characters an automatic change may replace or delete (a sentence, a few lines)
AUTO_ANCHOR_LINES = 3
KOL_TEXT_MAX = 600          # characters of each KOL message a review reads
BUDGET = 1.10              # an automatic change may grow the playbook to 110% of its baseline
EVIDENCE_MIN = 4           # characters of an evidence quote that must be found in the cited texts
CONTEXT = 30               # characters kept on each side of a change, to find it again for an undo
EDIT_RATIO_CONTRAST = 0.03
RATIO_CHARS = 8000          # above this the edit ratio is worked out sentence by sentence
EDIT_RATIO_HAND_WRITTEN = 0.7
LINE_MAX = 30              # characters of one line of the update notice
OPS = ("replace", "insert_after", "delete")
KINDS = ("correction", "case", "rule")

# The hard rules whose breaking, even once and on purpose, makes an episode an exception that is never learned from.
REDLINE_RULES = frozenset({
    "payment-after-publication", "no-bank-transfer", "no-paypal-unasked", "no-discount-code-promise",
    "no-order-level-visibility", "no-official-authorization-claim", "profit-share-cap",
})
# A hunk whose text matches this goes to the user for a yes, whatever the model said about it. Matched on the
# NFKC-folded, lower-cased text (full-width ％ and letters count as plain ones).
REDLINE_WORDS = re.compile(
    r"预付|定金|订金|首付|提前.{0,3}(付|打款|结)|先(付|打款|结)|发布前|上线前|deposit|upfront|up-front|in advance|"
    r"advance payment|prepa(y|id)|pay (half|part)|"
    r"银行|转账|电汇|汇款|bank|wire|paypal|\bwise\b|payoneer|usdt|crypto|加密货币|"
    r"折扣码|优惠码|折扣链接|discount|coupon|promo ?code|"
    r"每笔订单|完整流水|订单明细|order[- ]level|"
    r"官方授权|官方合作|官方认可|official|authori[sz]|"
    r"上限|封顶|最高到|\bcap\b|红线|独家|排他|exclusiv|保底|guarantee|百分之|[一二三四五六七八九十半]成")
# Numbers, money, percentages and links in an automatic change: always the owner's to approve (a word list never
# catches every way of writing "30 percent", and a case is a precedent the drafts follow).
FIGURES = re.compile(r"\d|https?://|www\.|[$€£¥₽%]|百分之|[一二三四五六七八九十]成|percent")
# What a KOL wrote that reads like an instruction to the AI: a review whose batch holds one writes nothing on its own.
INSTRUCTION_LIKE = re.compile(r"ignore|disregard|instruction|system prompt|playbook|your rules|规则|手册|忽略|指令|提示词",
                              re.IGNORECASE)
HEADING_LINE = re.compile(r"(?:^|\n)#{1,6}\s")
LINE_START = re.compile(r"\s*(?:[-*+]\s|\||\d+[.、]\s?)")


# ---------------------------------------------------------------- sections

def is_protected(heading: str) -> bool:
    """The sections only the user changes, through 手册更新 or by hand: their own way of negotiating and the red lines."""
    title = heading.lstrip("#").strip()
    return title.startswith("我的谈判方式") or bool(re.match(r"零[、.．]", title))


def is_case_library(heading: str) -> bool:
    """'## 十三、案例库（...）' or '## 案例库': a heading that merely mentions the case library is not it."""
    return bool(re.match(r"(?:[一二三四五六七八九十]+[、.．]\s*)?案例库", heading.lstrip("#").strip()))


CASES_LEFT_OUT = "（起草时略去：方案已在出策略时参考过案例库。不要引用或编造案例细节。）"


def without_case_library(playbook: str) -> str:
    """The playbook for the draft, chat and compose calls: the case library's heading stays, with one line saying
    why it is empty, so a line elsewhere that points at it ("细节见第十三节") does not point at nothing."""
    for heading, start, end in reversed(onboarding.sections(playbook)):
        if is_case_library(heading):
            playbook = playbook[:start] + f"{heading}\n\n{CASES_LEFT_OUT}\n\n" + playbook[end:].lstrip("\n")
    return playbook


def short_heading(heading: str) -> str:
    """'## 二、利润分成方案' -> '二、利润分成方案'; '' (the opening text) -> '开头'."""
    title = heading.lstrip("#").strip()
    return re.split(r"[（(]", title)[0].strip() or "开头"


# ---------------------------------------------------------------- hunks

def clean_hunks(raw, *, limit: int = MAX_HUNKS) -> list[dict]:
    """The model's hunks as plain dicts with every field, at most `limit` of them; anything malformed is left out."""
    out = []
    for h in raw if isinstance(raw, list) else []:
        if not isinstance(h, dict):
            continue
        op = str(h.get("op") or "").strip()
        if op not in OPS:
            continue
        ids = []
        for x in h.get("evidence_episode_ids") or []:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        out.append({
            "section": str(h.get("section") or "").strip()[:80],
            "op": op,
            "anchor": str(h.get("anchor") or "").replace("\r\n", "\n"),
            "new_text": "" if op == "delete" else str(h.get("new_text") or "").replace("\r\n", "\n"),
            "kind": str(h.get("kind") or "rule").strip() if str(h.get("kind") or "") in KINDS else "rule",
            "touches_rules": [str(r).strip() for r in h.get("touches_rules") or [] if str(r).strip()][:10],
            "evidence_episode_ids": ids[:20],
            "evidence_quote": str(h.get("evidence_quote") or "").strip()[:400],
            "why_zh": " ".join(str(h.get("why_zh") or "").split())[:120],
        })
    return out[:limit]


def anchor_count(playbook: str, anchor: str) -> int:
    return playbook.count(anchor) if anchor else 0


def hunk_section(playbook: str, hunk: dict) -> str:
    """The heading of the section the hunk's anchor is in (worked out here, never taken from the model)."""
    at = playbook.find(hunk["anchor"]) if hunk.get("anchor") else -1
    return onboarding.section_of(playbook, at) if at >= 0 else ""


def folded(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def touches_redline(hunk: dict, playbook: str = "", figures: bool = True) -> bool:
    """A change a person has to approve: the model says it touches a hard rule, its text names a red-line subject,
    or (figures) it writes or removes a number, an amount, a percentage or a link."""
    if hunk.get("touches_rules"):
        return True
    new, old = folded(hunk.get("new_text", "")), folded(hunk.get("anchor", ""))
    removes = hunk["op"] != "insert_after"
    if REDLINE_WORDS.search(new) or (removes and REDLINE_WORDS.search(old)):
        return True
    return figures and bool(FIGURES.search(new) or (removes and FIGURES.search(old)))


def structure_problem(before: str, after: str, *, allow_protected: bool = False) -> str:
    """Why a change applied to the playbook is not allowed after all ("" when it is): its headings must stay exactly
    as they were (a hunk that glues a heading onto the line above would take the section with it), and unless the
    owner made the change, the protected sections must stay word for word."""
    old = [(h, before[s:e]) for h, s, e in onboarding.sections(before)]
    new = [(h, after[s:e]) for h, s, e in onboarding.sections(after)]
    if [h for h, _ in old] != [h for h, _ in new]:
        return "改动会动到手册的标题或分节"
    if not allow_protected and [t for h, t in old if is_protected(h)] != [t for h, t in new if is_protected(h)]:
        return "“我的谈判方式”和“零、两条红线”只能由您本人改"
    return ""


def shape_problem(playbook: str, hunk: dict, *, allow_protected: bool = False) -> str:
    """Why this hunk cannot be applied at all ("" when it can)."""
    if not hunk.get("anchor", "").strip():
        return "没有给出原文"
    n = anchor_count(playbook, hunk["anchor"])
    if n == 0:
        return "原文在手册里找不到"
    if n > 1:
        return f"原文在手册里出现了 {n} 次，定位不唯一"
    if hunk["op"] != "delete" and not hunk.get("new_text", "").strip():
        return "没有给出新内容"
    if len(hunk.get("new_text", "")) > NEW_TEXT_MAX:
        return "新内容太长（只允许局部修改，不允许整段重写）"
    if HEADING_LINE.search("\n" + hunk.get("new_text", "")) or HEADING_LINE.search("\n" + hunk["anchor"]):
        return "改动碰到了标题（不允许改手册结构或整节重写）"
    if hunk["op"] == "replace" and len(hunk["anchor"]) > 3 * NEW_TEXT_MAX:
        return "一次替换的原文太长（不允许整段重写）"
    if not allow_protected and is_protected(hunk_section(playbook, hunk)):
        return "“我的谈判方式”和“零、两条红线”只能由您本人改"
    return ""


def apply_hunk(playbook: str, hunk: dict) -> tuple[str, dict]:
    """(the playbook with the hunk applied, the undo record). The record holds the changed spot with a little
    context on each side, before and after, so the change can be found and undone later even if the text around it
    moved (revert_hunk). ValueError when the hunk does not apply (shape_problem says why)."""
    anchor = hunk["anchor"]
    if anchor_count(playbook, anchor) != 1:
        raise ValueError("原文定位不唯一或找不到")
    start = playbook.index(anchor)
    end = start + len(anchor)
    if hunk["op"] == "replace":
        new = hunk["new_text"]
    elif hunk["op"] == "insert_after":
        text = hunk["new_text"].strip("\n")
        if anchor.endswith("\n"):  # the anchor took its line break along: the new line goes on the next line
            new = anchor + text + "\n"
        elif playbook[end:end + 1] in ("\n", ""):  # the anchor ends a line
            new = anchor + "\n" + text
        elif LINE_START.match(text):  # a new list item or table row never starts in the middle of a line
            raise ValueError("插入位置在一行中间，新内容却是新的一行")
        else:  # a sentence added inside a paragraph
            new = anchor + text
    else:
        new = ""
        # a deleted line takes its line break along, so no empty line is left behind
        if (start == 0 or playbook[start - 1] == "\n") and playbook[end:end + 1] == "\n":
            end += 1
            anchor = playbook[start:end]
    pre = playbook[max(0, start - CONTEXT):start]
    post = playbook[end:end + CONTEXT]
    result = playbook[:start] + new + playbook[end:]
    return result, {"before": pre + playbook[start:end] + post, "after": pre + new + post}


def revert_hunk(playbook: str, record: dict) -> str | None:
    """The playbook with one applied hunk undone, or None when its spot is no longer there exactly once (the text
    around it was changed since): then only a rollback of the whole version can undo it."""
    after, before = record.get("after", ""), record.get("before", "")
    if not after or playbook.count(after) != 1:
        return None
    return playbook.replace(after, before, 1)


def size_ok(playbook_after: str, baseline: int) -> bool:
    return not baseline or len(playbook_after) <= baseline * BUDGET


# ---------------------------------------------------------------- evidence

def _squash(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


QUOTE_SHARE = 0.8  # of a quote that must be in one text, in one piece, for the quote to count as from it


def found_in(quote: str, texts: list[str]) -> bool:
    """The quote, or nearly all of it in one piece, is in one of the texts (spacing and case ignored)."""
    q = _squash(quote)
    if len(q) < EVIDENCE_MIN:
        return False
    for t in texts:
        t = _squash(t)
        if not t:
            continue
        if q in t:
            return True
        m = difflib.SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
        if m.size >= max(EVIDENCE_MIN, len(q) * QUOTE_SHARE):
            return True
    return False


def human_texts(ep: dict) -> list[str]:
    """What the user said or did for this reply: notes, feedback, Chinese corrections, the owner check's misses, the
    sentences they changed by hand (as sent)."""
    texts = [ep.get("notes") or ""]
    texts += [str(x) for x in ep.get("feedback_log") or []]
    for e in ep.get("zh_edits") or []:
        if isinstance(e, dict):
            texts += [str(e.get("zh_after") or "")]
    texts += [str(m) for m in (ep.get("compliance") or {}).get("missed") or []]
    for line in sentence_diff(ep.get("ai_body") or "", ep.get("sent_body") or "", context=0):
        if line.startswith(("+ ", "- ")):
            texts.append(line[2:])
    strategy = ep.get("strategy_options") or []
    chosen, rec = ep.get("chosen"), ep.get("recommended")
    if isinstance(chosen, int) and chosen != rec and 0 <= chosen < len(strategy):
        texts.append(" ".join(str(v) for v in strategy[chosen].values()))
    return [t for t in texts if t and t.strip()]


QUOTE_MIN = 6       # characters of an evidence quote (spacing ignored): "with" is no evidence
RELATED_MIN = 3     # characters a quote must share with the change it backs


def related(quote: str, hunk: dict) -> bool:
    """The quote and the change say something in common: a run of a few characters is in both."""
    q, t = _squash(quote), _squash(hunk.get("new_text", "") + " " + hunk.get("anchor", ""))
    if not q or not t:
        return False
    m = difflib.SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    return m.size >= RELATED_MIN


def evidence_kind(hunk: dict, episodes: dict[int, dict]) -> str:
    """'human' when the quote is in the user's words or edits of a cited episode and has to do with the change,
    'unrelated' when it is the user's but says nothing about the change, 'outcome' when the cited episodes only show
    how the negotiation ended, 'kol' when the quote is only in what the KOL wrote, 'none' otherwise."""
    cited = [episodes[i] for i in hunk.get("evidence_episode_ids") or [] if i in episodes]
    if not cited:
        return "none"
    quote = hunk.get("evidence_quote", "")
    if len(_squash(quote)) < QUOTE_MIN:
        return "none"
    if any(found_in(quote, [ep.get("kol_text") or ""]) for ep in episodes.values()):
        return "kol"  # also when the owner's reply repeats it: the words came from the KOL first
    if any(found_in(quote, human_texts(ep)) for ep in cited):
        return "human" if related(quote, hunk) else "unrelated"
    if any(found_in(quote, [ep.get("kol_text") or ""]) for ep in cited):
        return "kol"
    if any(ep.get("outcome") or ep.get("stance_next") for ep in cited):
        return "outcome"
    return "none"


# ---------------------------------------------------------------- deciding

def judge(playbook: str, hunks: list[dict], *, source: str, baseline: int,
          episodes: dict[int, dict] | None = None, allow_protected: bool = False) -> list[dict]:
    """Each hunk with its verdict: state "apply" (written now), "pending" (waits for the user) or "rejected", the
    reason, and the section it is in. source: "auto" (the review), "update" (the user's own words, 手册更新), "sop".
    Hunks are judged in order against the playbook as the earlier applied ones leave it."""
    current, out = playbook, []
    suspicious = any(INSTRUCTION_LIKE.search(ep.get("kol_text") or "") for ep in (episodes or {}).values())
    for h in hunks:
        h = {**h, "section": short_heading(hunk_section(current, h)) if h.get("anchor") else h.get("section", "")}
        problem = shape_problem(current, h, allow_protected=allow_protected)
        if not problem and source in ("auto", "sop") and h["op"] != "insert_after" and (
                len(h["anchor"]) > AUTO_ANCHOR_MAX or h["anchor"].strip().count("\n") >= AUTO_ANCHOR_LINES):
            problem = "一次改掉的原文太多（自动修改只能改一句或几行）"
        if problem:
            out.append({**h, "state": "rejected", "reason": problem})
            continue
        state, reason = "apply", ""
        if source == "auto":
            kind = evidence_kind(h, episodes or {})
            if kind in ("kol", "none"):
                out.append({**h, "state": "rejected",
                            "reason": "证据只来自达人原文" if kind == "kol" else "证据对不上您的修改或意见"})
                continue
            if kind == "outcome":
                state, reason = "pending", "只有谈判结果支撑，需要您确认"
            elif kind == "unrelated":
                state, reason = "pending", "依据和改动内容对不上，需要您确认"
            elif suspicious:
                state, reason = "pending", "这批来信里有像是给 AI 下指令的话，需要您确认"
        elif source == "sop":
            state, reason = "pending", "SOP 变化建议，手册和 SOP 不一致时以您的规则为准，需要您确认"
        if state == "apply" and source != "update" and touches_redline(h, current):
            state, reason = "pending", "涉及红线或硬规则，需要您确认"
        try:
            after, record = apply_hunk(current, h)
        except ValueError as e:
            out.append({**h, "state": "rejected", "reason": str(e)})
            continue
        problem = structure_problem(current, after, allow_protected=allow_protected)
        if problem:
            out.append({**h, "state": "rejected", "reason": problem})
            continue
        if state == "apply" and source != "update" and not size_ok(after, baseline):
            state, reason = "pending", "手册会超出体积预算（基线的 110%），需要您确认，或先把旧案例移到归档"
        if state == "apply":
            current = after
            out.append({**h, "state": "apply", "reason": "", "applied": record})
        else:
            out.append({**h, "state": state, "reason": reason})
    return out


def apply_all(playbook: str, judged: list[dict]) -> str:
    """The playbook with every hunk judged "apply" applied, in order (judge already checked they apply)."""
    for h in judged:
        if h.get("state") == "apply":
            playbook, h["applied"] = apply_hunk(playbook, h)
    return playbook


# ---------------------------------------------------------------- the review's input

SENTENCE = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE.split((text or "").replace("\r\n", "\n")) if s and s.strip()]


def sentence_diff(ai: str, sent: str, context: int = 1) -> list[str]:
    """The changed sentences of ai -> sent, "- " removed and "+ " added, with `context` unchanged sentences
    ("  ") on each side of every change."""
    a, b = sentences(ai), sentences(sent)
    out: list[str] = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out += ["  " + s for s in a[i1:i2]]
            else:
                out += ["- " + s for s in a[i1:i2]] + ["+ " + s for s in b[j1:j2]]
        out.append("...")
    return out[:-1] if out else out


def edit_ratio(ai: str, sent: str) -> float:
    """How much of the AI's text the user changed before sending: 0 untouched, 1 all new."""
    a, b = (ai or "").strip(), (sent or "").strip()
    if not a:
        return 1.0 if b else 0.0
    if len(a) + len(b) > RATIO_CHARS:  # character by character is quadratic: 50k characters took 49 s
        return round(1 - difflib.SequenceMatcher(None, sentences(a), sentences(b), autojunk=False).ratio(), 4)
    return round(1 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


def has_contrast(ep: dict) -> bool:
    """The user corrected the AI on this reply in some way the review can learn from."""
    return bool((ep.get("edit_ratio") or 0) >= EDIT_RATIO_CONTRAST or ep.get("feedback_log") or ep.get("zh_edits")
                or (ep.get("compliance") or {}).get("missed")
                or (isinstance(ep.get("chosen"), int) and isinstance(ep.get("recommended"), int)
                    and ep["chosen"] != ep["recommended"]))


def learnable(ep: dict) -> bool:
    """Not an exception the user allowed once, and an AI draft the user edited rather than a text they wrote."""
    return (not ep.get("exception") and bool((ep.get("ai_body") or "").strip())
            and (ep.get("edit_ratio") or 0) <= EDIT_RATIO_HAND_WRITTEN)


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def contrast_keys(episodes: list[dict]) -> set:
    """What counts towards the review's trigger: one per episode with an edit of its own, but a batch of replies
    changed the same day by the same one comment counts once."""
    keys = set()
    for ep in episodes:
        if not has_contrast(ep):
            continue
        own = ((ep.get("edit_ratio") or 0) >= EDIT_RATIO_CONTRAST or ep.get("zh_edits")
               or (ep.get("compliance") or {}).get("missed")
               or (isinstance(ep.get("chosen"), int) and isinstance(ep.get("recommended"), int)
                   and ep["chosen"] != ep["recommended"]))
        if own:
            keys.add(("ep", ep["id"]))
        else:
            keys.add(("fb", ep.get("day", ""), tuple(sorted(_norm(f) for f in ep.get("feedback_log") or []))))
    return keys


def review_input(playbook: str, episodes: list[dict], section) -> tuple[str, str]:
    """(the PLAYBOOK part, the EPISODES part) of the review's user message. The protected sections are marked; each
    episode gives its sentence diff, the user's words (a comment repeated the same day is given once), Chinese
    corrections, the strategies, the result; what the KOL wrote goes in a section of its own, as untrusted data."""
    marked = []
    for heading, start, end in onboarding.sections(playbook):
        if is_protected(heading):
            marked.append(f"{heading}  ← 受保护：自动修改不能碰这一节")
    head = "受保护的节：" + ("；".join(marked) if marked else "无")
    seen: dict[tuple, int] = {}
    blocks, kol = [], []
    for ep in episodes:
        lines = [f"### 第 {ep['id']} 封（{ep.get('day', '')}，{ep.get('channel', '')}，{ep.get('language', '')}）"]
        diff = sentence_diff(ep.get("ai_body") or "", ep.get("sent_body") or "")
        if diff:
            lines += ["AI 稿 → 实发稿（- 删掉，+ 改成）：", *diff]
        if (ep.get("notes") or "").strip():
            lines.append(f"起草前的要点：{_norm(ep['notes'])[:600]}")
        for f in ep.get("feedback_log") or []:
            key = (ep.get("day", ""), _norm(f))
            if key in seen:
                lines.append(f"修改意见：（同第 {seen[key]} 封，同一天同一句，只算一次）")
            else:
                seen[key] = ep["id"]
                lines.append(f"修改意见：{_norm(f)[:600]}")
        for e in ep.get("zh_edits") or []:
            if isinstance(e, dict):
                lines.append(f"改中文：{_norm(e.get('zh_before', ''))[:300]} → {_norm(e.get('zh_after', ''))[:300]}")
        for m in (ep.get("compliance") or {}).get("missed") or []:
            lines.append(f"AI 没照意见做到：{_norm(m)[:200]}")
        opts = ep.get("strategy_options") or []
        if opts and isinstance(ep.get("chosen"), int):
            titles = [str(o.get("title_zh") or "") for o in opts if isinstance(o, dict)]
            rec, chosen = ep.get("recommended"), ep["chosen"]
            if 0 <= chosen < len(titles):
                lines.append(f"策略：推荐「{titles[rec] if isinstance(rec, int) and 0 <= rec < len(titles) else '?'}」，"
                             f"选了「{titles[chosen]}」")
        result = ep.get("outcome") or ""
        if ep.get("stance_next"):
            result += ("，" if result else "") + f"对方下一封的态度：{ep['stance_next']}"
        if result:
            lines.append(f"结果：{result}")
        blocks.append("\n".join(lines))
        if (ep.get("kol_text") or "").strip():
            kol.append(f"### 第 {ep['id']} 封回的那封来信\n{ep['kol_text'][:KOL_TEXT_MAX]}")
    parts = [section("PLAYBOOK", f"{head}\n\n{playbook}"), section("EPISODES", "\n\n".join(blocks))]
    if kol:
        parts.append(section("KOL_TEXT", "\n\n".join(kol)))
    return parts[0], "\n\n".join(parts[1:])


# ---------------------------------------------------------------- what changed, in a line each

def clip(text: str, n: int = LINE_MAX) -> str:
    text = _norm(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def notice_lines(hunks: list[dict], limit: int = 8) -> list[str]:
    """One short line per change for the update notice: '二、利润分成：起步改报 25%'."""
    lines = []
    for h in hunks[:limit]:
        if not h.get("section"):  # a line that says it all already (a hand edit, an undo)
            lines.append(clip(h.get("why_zh") or h.get("op") or ""))
            continue
        sec = short_heading(h["section"])
        sec = re.sub(r"^([一二三四五六七八九十零]+、)(.{0,4}).*$", r"\1\2", sec)
        lines.append(clip(f"{sec}：{h.get('why_zh') or h.get('op')}"))
    if len(hunks) > limit:
        lines.append(f"……还有 {len(hunks) - limit} 处")
    return lines


SOURCE_ZH = {"auto": "自动", "update": "手册更新", "sop": "SOP 同步", "approve": "您批准的", "revert": "撤销",
             "rollback": "整版回滚", "manual": "手动修改", "archive": "案例归档", "expiry": "过期处理",
             "mixed": "多处来源"}


def notice_title(n: int, source: str, days: list[str]) -> str:
    """'手册更新了 3 处（自动 · 来自您 11-04、11-05 的修改）'."""
    origin = SOURCE_ZH.get(source, source)
    if source == "auto" and days:
        origin += f" · 来自您 {'、'.join(sorted(set(days))[-4:])} 的修改"
    return f"手册更新了 {n} 处（{origin}）"


def manual_lines(old: str, new: str, limit: int = 6) -> list[str]:
    """For a change the user made by hand: which sections changed, one line each."""
    def by_heading(text):
        return {h: text[s:e] for h, s, e in onboarding.sections(text)}
    a, b = by_heading(old), by_heading(new)
    changed = [h for h in b if a.get(h) != b[h]] + [h for h in a if h not in b]
    lines = [clip(f"{short_heading(h)}：{'删掉了这一节' if h not in b else '有改动'}") for h in changed[:limit]]
    head_old = old[:onboarding.sections(old)[0][1]] if onboarding.sections(old) else old
    head_new = new[:onboarding.sections(new)[0][1]] if onboarding.sections(new) else new
    if head_old != head_new and len(lines) < limit:
        lines.insert(0, "开头：有改动")
    return lines or ["有改动"]


# ---------------------------------------------------------------- dated lines that run out

DATED = re.compile(r"[（(]([^（）()]{0,80}?)至\s*(?:(\d{4})\s*[-/年])?\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})(?![\d%％])\s*日?"
                   r"([^（）()]{0,40})[）)]")
YEAR = re.compile(r"(\d{4})\s*[-/年]")
START = re.compile(r"(?:(\d{4})\s*[-/年])?\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})")


def end_date(m: re.Match, today: date) -> date:
    """The end date of a DATED match. Without its own year: the start date's year, a year later when it would fall
    before the start (2025-12-01 起，至 01-05); with no year anywhere, the nearest such date (活动至 1月5日 written in
    December is next January)."""
    month, day = int(m.group(3)), int(m.group(4))
    if m.group(2):
        return date(int(m.group(2)), month, day)
    start = START.search(m.group(1))
    if start and start.group(1):
        begin = date(int(start.group(1)), int(start.group(2)), int(start.group(3)))
        until = date(begin.year, month, day)
        return until if until >= begin else date(begin.year + 1, month, day)
    until = date(today.year, month, day)
    return date(today.year + 1, month, day) if (today - until).days > 183 else until


def dated_lines(playbook: str, today: date) -> list[dict]:
    """Every line with an inline end date that has passed, e.g. '（2026-11-03 起，至 11-30）': {line, until}."""
    out = []
    for line in playbook.replace("\r\n", "\n").split("\n"):
        for m in DATED.finditer(line):
            try:
                until = end_date(m, today)
            except ValueError:
                continue
            if until < today:
                out.append({"line": line.rstrip(), "until": until.isoformat()})
                break
    return out


# ---------------------------------------------------------------- the case library's archive

TABLE_ROW = re.compile(r"^\|.*\|\s*$")


def case_rows(playbook: str) -> tuple[int, int, list[tuple[int, int]]]:
    """(start, end) of the case library section and the (start, end) of each data row of its table."""
    for heading, start, end in onboarding.sections(playbook):
        if is_case_library(heading):
            rows, seen_header, pos = [], 0, start
            for line in playbook[start:end].split("\n"):
                line_end = pos + len(line) + 1
                if TABLE_ROW.match(line):
                    seen_header += 1
                    if seen_header > 2:  # the header and the |---| line stay
                        rows.append((pos, min(line_end, end)))
                pos = line_end
            return start, end, rows
    return -1, -1, []


def archive_cases(playbook: str, n: int) -> tuple[str, list[str]]:
    """(the playbook without the first n case rows, those rows) — the oldest cases, moved to the archive file that
    no prompt reads."""
    _, _, rows = case_rows(playbook)
    take = rows[:max(0, n)]
    if not take:
        return playbook, []
    moved = [playbook[s:e].rstrip("\n") for s, e in take]
    for s, e in reversed(take):
        playbook = playbook[:s] + playbook[e:]
    return playbook, moved


# ---------------------------------------------------------------- SOP snapshots

def text_diff(old: str, new: str, context: int = 1, limit: int = 12000) -> str:
    """The changed lines of a document between two snapshots ("" when nothing changed)."""
    a, b = (old or "").replace("\r\n", "\n").split("\n"), (new or "").replace("\r\n", "\n").split("\n")
    lines = [line for line in difflib.unified_diff(a, b, "上一版", "这一版", n=context, lineterm="")]
    return "\n".join(lines)[:limit]
