"""The hard negotiation rules of rules/negotiation_rules.yaml, checked on every draft before the owner sees it.

check() takes the outgoing text and what the conversation already holds (the KOL's words, our earlier mails, the
owner's notes, the Chinese back-translation) and returns the rules it breaks. Rules with action "revise" are sent
back to the model by app.py with their fix; "review" ones only tell the owner in needs_approval_zh. Everything here
is plain text matching, no model call, so the same draft always gets the same verdict."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RULES_FILE = Path(__file__).resolve().parent / "rules" / "negotiation_rules.yaml"
ACTIONS = ("revise", "review")
EVIDENCE_MAX = 160
IMAGE_EXT = re.compile(r"\.(png|jpe?g|webp|gif|heic|bmp)\b", re.IGNORECASE)
URL = re.compile(r"https?://[^\s<>()\[\]{}\"'，。、；：！？【】]+", re.IGNORECASE)
HAN_RUN = re.compile(r"[㐀-䶿一-鿿]+")
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9.-]{2,}")
CLOCK_OR_DATE = re.compile(r"\d{1,2}:\d{2}|\d{1,4}[-/年月]\d{1,2}(?:[-/月日]\d{0,2})?")


class RulesError(Exception):
    pass


@dataclass
class Violation:
    rule: str
    title_zh: str
    action: str
    evidence: list[str]
    fix: str = ""


@dataclass
class Context:
    """What a draft is checked against. kol_texts and our_texts are whole messages; quotes are cut here."""
    kol_texts: list[str] = field(default_factory=list)       # the KOL's messages, latest first
    our_texts: list[str] = field(default_factory=list)       # what we sent them before
    owner_texts: list[str] = field(default_factory=list)     # the owner's notes and feedback for this draft
    zh: str = ""                                             # the Chinese back-translation of the draft
    playbook: str = ""
    subject: str = ""
    signature: str = ""
    needs_approval: str = ""
    kol_attachments: list[str] = field(default_factory=list)
    materials: list[dict] = field(default_factory=list)
    outgoing_attachments: bool = False


@dataclass
class Rules:
    rules: list[dict]
    vocab: dict[str, re.Pattern]
    max_revisions: int


# ---------------------------------------------------------------- loading

_cache: dict = {"mtime": None, "path": None, "rules": None}


REGEX_KEYS = ("near", "exempt", "kol_mentions", "unless_owner", "trigger", "reminder", "games", "markers",
              "instruction", "about_us", "internal", "secret", "terms", "their_terms")
NUMBER_KEYS = ("cap", "min_coverage", "number_tolerance")


def _compile(pattern, where: str) -> re.Pattern:
    if not isinstance(pattern, str):
        raise RulesError(f"{where} 应该是一条正则（字符串）")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise RulesError(f"{where} 的正则写错了：{e}") from e


def _number(value, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise RulesError(f"{where} 应该是数字") from None


def parse(raw) -> Rules:
    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list):
        raise RulesError("规则文件里没有 rules 列表")
    if not isinstance(raw.get("vocab") or {}, dict):
        raise RulesError("vocab 应该是“名字: 正则”的对照")
    vocab = {str(k): _compile(v, f"vocab.{k}") for k, v in (raw.get("vocab") or {}).items()}
    rules, seen = [], set()
    for i, r in enumerate(raw["rules"]):
        if not isinstance(r, dict) or not r.get("id"):
            raise RulesError(f"第 {i + 1} 条规则没有 id")
        rid = str(r["id"])
        if rid in seen:
            raise RulesError(f"规则 {rid} 重复了")
        seen.add(rid)
        if r.get("action") not in ACTIONS:
            raise RulesError(f"规则 {rid} 的 action 只能是 revise 或 review")
        if r.get("check") not in CHECKS:
            raise RulesError(f"规则 {rid} 的 check「{r.get('check')}」没有对应的检查")
        if r["action"] == "revise" and not str(r.get("fix") or "").strip():
            raise RulesError(f"规则 {rid} 要自动改写，但没写 fix")
        rule = {**r, "id": rid, "title_zh": str(r.get("title_zh") or rid)}
        for key in ("patterns", "allow"):
            if not isinstance(r.get(key) or [], list):
                raise RulesError(f"规则 {rid} 的 {key} 应该是正则列表")
            rule[key] = [_compile(p, f"规则 {rid} 的 {key}") for p in r.get(key) or []]
        for key in REGEX_KEYS:
            if r.get(key):
                rule[key] = _compile(r[key], f"规则 {rid} 的 {key}")
        for key in NUMBER_KEYS:
            if key in r:
                rule[key] = _number(r[key], f"规则 {rid} 的 {key}")
        if r["check"] == "patterns" and not rule["patterns"]:
            raise RulesError(f"规则 {rid} 没有 patterns")
        if r["check"] == "reminder" and not (rule.get("trigger") and rule.get("reminder")):
            raise RulesError(f"规则 {rid} 要写 trigger 和 reminder")
        rules.append(rule)
    needed = {v for r in rules for v in NEEDS.get(r["check"], ())}
    needed |= {"negation", "refusal", "comfort"} if any(r.get("negation_ok") for r in rules) else set()
    missing = sorted(needed - set(vocab))
    if missing:
        raise RulesError(f"vocab 里缺少 {', '.join(missing)}")
    revisions = _number(raw.get("max_revisions", 2), "max_revisions")
    if revisions < 0 or revisions != int(revisions):
        raise RulesError("max_revisions 应该是 0 或正整数")
    return Rules(rules, vocab, int(revisions))


def load(path: Path | None = None) -> Rules:
    """The rules, read again whenever the file changes."""
    path = path or RULES_FILE
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as e:
        raise RulesError(f"读不到规则文件 {path.name}：{e}") from e
    if _cache["rules"] is not None and _cache["mtime"] == mtime and _cache["path"] == path:
        return _cache["rules"]
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise RulesError(f"规则文件 {path.name} 读不了：{e}") from e
    try:
        rules = parse(raw)
    except RulesError:
        raise
    except Exception as e:  # noqa: BLE001  whatever a hand edit did, the drafts go on without the check
        raise RulesError(f"规则文件 {path.name} 格式不对：{e}") from e
    _cache.update(mtime=mtime, path=path, rules=rules)
    return rules


# ---------------------------------------------------------------- text helpers

QUOTE_START = re.compile(r"^\s*(On .{4,200}wrote:|El .{4,200}escribió:|Em .{4,200}escreveu:|Le .{4,200}a écrit\s*:|"
                         r"Am .{4,200}schrieb|.{0,200}(написал|пишет)\(?а?\)?:|-{2,}\s*Original Message|"
                         r"From: .+|发件人[:：]|在.{2,60}写道[:：])", re.IGNORECASE)


def own_words(text: str) -> str:
    """A message without the history it quotes."""
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if QUOTE_START.match(line):
            break
        if not line.lstrip().startswith(">"):
            out.append(line)
    return "\n".join(out)


def without_signature(body: str, signature: str) -> str:
    body = (body or "").replace("\r\n", "\n")
    sig = (signature or "").strip()
    if sig and sig in body:
        body = body[:body.rindex(sig)]
    return body


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？])\s+|\n+", text or "")
    return [p.strip(" \t•-*") for p in parts if p.strip(" \t•-*")]


def shown(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EVIDENCE_MAX else text[:EVIDENCE_MAX - 1] + "…"


def numbers_in(text: str) -> list[float]:
    """Numbers as a reader means them: "53 000", "53,465" and "1.000" are thousands, "4,8" is 4.8."""
    found = []
    for token in re.findall(r"\d+(?:[   .,]\d+)*", CLOCK_OR_DATE.sub(" ", text or "")):
        groups = re.split(r"[   .,]", token)
        seps = re.findall(r"[   .,]", token)
        if len(groups) > 1 and all(len(g) == 3 for g in groups[1:]) and not (len(seps) == 1 and seps[0] == "."
                                                                               and len(groups[0]) > 3):
            found.append(float("".join(groups)))
        elif len(groups) == 2:
            found.append(float(groups[0] + "." + groups[1]))
        else:
            found.extend(float(g) for g in groups)
    return found


def owner_numbers(text: str) -> list[float]:
    """Numbers in the owner's Chinese: 5w and 5万 are 50000, 3k is 3000."""
    out = []
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([wW万kK千]?)", CLOCK_OR_DATE.sub(" ", text or "")):
        out.append(float(value) * {"w": 1e4, "W": 1e4, "万": 1e4, "k": 1e3, "K": 1e3, "千": 1e3}.get(unit, 1))
    return out


def bigrams(text: str) -> set[str]:
    return {run[i:i + 2] for run in HAN_RUN.findall(text or "") for i in range(len(run) - 1)}


def _violation(rule: dict, evidence: list[str]) -> Violation:
    unique = list(dict.fromkeys(shown(e) for e in evidence if e and e.strip()))
    return Violation(rule["id"], rule["title_zh"], rule["action"], unique[:3], str(rule.get("fix") or "").strip())


# ---------------------------------------------------------------- the checks; each returns evidence or []

QUOTED = re.compile(r'"[^"\n]{1,120}"|“[^”\n]{1,120}”|«[^»\n]{1,120}»|「[^」\n]{1,120}」|『[^』\n]{1,120}』')
MONEY = re.compile(r"[$€£₽¥]\s?\d[\d.,\s]{0,12}\d|[$€£₽¥]\s?\d|\d[\d.,\s]{0,12}(usd|us\$|dollars?|eur|euros?|€|gbp|£|"
                   r"руб\w*|₽|rub|r\$|brl|reais|yen|円|원|usdt)\b", re.IGNORECASE)


def refuses(s: str, v: dict) -> bool:
    """The sentence says no ("we can't", "не сможем", "but ... after publication"), reassurances left aside."""
    plain = v["comfort"].sub(" ", s) if v.get("comfort") else s
    return bool((v.get("negation") and v["negation"].search(plain)) or (v.get("refusal") and v["refusal"].search(plain)))


def check_patterns(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    if rule.get("skip_with_outgoing_attachments") and ctx.outgoing_attachments:
        return []
    if rule.get("kol_mentions") and not any(rule["kol_mentions"].search(own_words(t)) for t in ctx.kol_texts):
        return []
    if rule.get("unless_owner") and any(rule["unless_owner"].search(t) for t in ctx.owner_texts):
        return []
    hits = []
    for s in sentences(body):
        bare = QUOTED.sub(" ", s)
        if not any(p.search(bare) for p in rule["patterns"]):
            continue
        if rule.get("exempt") and rule["exempt"].search(bare):
            continue
        if rule.get("near") and not rule["near"].search(bare):
            continue
        if rule.get("negation_ok") and refuses(bare, v):
            continue
        hits.append(s)
    return hits


def check_reminder(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    """The trigger shows up (e.g. the link set-up page) but the reminder that must go with it does not."""
    if rule["reminder"].search(body):
        return []
    return [s for s in sentences(body) if rule["trigger"].search(s)]


def check_paypal_unasked(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    if any("paypal" in own_words(t).lower() for t in ctx.kol_texts):
        return []
    return [s for s in sentences(body) if "paypal" in s.lower()]


def _percents(s: str) -> list[float]:
    return [float(x.replace(",", ".")) for x in re.findall(r"(\d{1,3}(?:[.,]\d+)?)\s?%", s)]


def _profit_percents(s: str, v: dict) -> list[float]:
    """Percentages standing right next to a profit word: "20% of the net profit", "60% de la ganancia", "순이익의 60%"
    (not the 100% of a refund in the same sentence)."""
    out = []
    for clause in re.split(r"[;,，；]|\b(?:and|but|и|но|y|pero|e|mas|et)\b", s, flags=re.IGNORECASE):
        if v["profit"].search(clause or ""):
            out += [float(x.replace(",", ".")) for x in re.findall(r"(\d{1,3}(?:[.,]\d+)?)\s?%", clause)]
    out += [float(a) for a, _ in re.findall(r"\b(\d{2})\s?/\s?(\d{2})\b[^.\n]{0,15}split", s, re.IGNORECASE)]
    return out


def check_profit_share_cap(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    cap = float(rule.get("cap", 50))
    return [s for s in sentences(body) if any(p > cap for p in _profit_percents(s, v))]


def _offers_profit_share(text: str, v: dict) -> list[str]:
    return [s for s in sentences(text) if _profit_percents(s, v)]


def check_cps_opens_at_default(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    offers = [s for s in _offers_profit_share(body, v) if not refuses(s, v)]
    if not offers:
        return []
    if any(_offers_profit_share(t, v) for t in ctx.our_texts):
        return []  # the profit share was on the table already: this is the escalation, not the opening
    if any(v["pushback"].search(own_words(t)) for t in ctx.kol_texts):
        return []
    return offers


def check_sweetener_before_quote(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    if any(v["price_quote"].search(own_words(t)) for t in ctx.kol_texts + ctx.our_texts):
        return []
    if not v["fee_talk"].search(body):
        return []
    return [s for s in sentences(body) if v["additive"].search(s) and v["sweetener"].search(s)
            and not v["default_cps"].search(s) and not refuses(s, v)]


def check_confirm_amount(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    """An amount accepted in so many words ("Deal, 300 USD works for us") that the owner never gave: neither in their
    notes nor in what we offered before."""
    approved = {n for t in ctx.owner_texts for n in owner_numbers(t)} | \
        {n for t in ctx.our_texts for n in numbers_in(own_words(t))}
    out = []
    for s in sentences(body):
        if not v["acceptance"].search(s) or refuses(s, v):
            continue
        amounts = [m.group(0) for m in MONEY.finditer(s)]
        if any(not set(numbers_in(a)) & approved for a in amounts):
            out.append(s)
    return out


def _normal_url(url: str) -> str:
    url = url.rstrip(".,;:!?)»”’'\"").split("#")[0]
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower().removeprefix('www.')}{slash}{path}".rstrip("/")


def check_unlisted_links(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    known = {_normal_url(u) for t in [ctx.playbook, ctx.subject, *ctx.kol_texts, *ctx.our_texts, *ctx.owner_texts]
             for u in URL.findall(t or "")}
    out = []
    for raw in URL.findall(body):
        url = raw.rstrip(".,;:!?)»”’'\"")
        if any(p.search(url) for p in rule["allow"]):
            continue
        if _normal_url(url) not in known:
            out.append(url)
    return out


def check_giveaway_games(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    """A giveaway offered in a conversation about a game that cannot have one. The game is read from the subject,
    the KOL's latest message and the draft itself, so a game named weeks ago does not count."""
    offers = [s for s in sentences(body) if v["giveaway"].search(s) and not refuses(s, v)]
    if not offers or not rule.get("games"):
        return []
    latest = own_words(ctx.kol_texts[0]) if ctx.kol_texts else ""
    games = rule["games"].findall("\n".join([ctx.subject, latest, body]))
    return [f"{s}（游戏：{games[0]}）" for s in offers] if games else []


def check_reasking_received(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    sent_pictures = any(IMAGE_EXT.search(n or "") for n in ctx.kol_attachments) or \
        any(IMAGE_EXT.search(str(m.get("what") or "")) or "照片" in str(m.get("what") or "") for m in ctx.materials)
    if not sent_pictures:
        return []
    return [s for s in sentences(body) if any(p.search(s) for p in rule["patterns"])]


def owner_claims(text: str, rule: dict) -> tuple[list[str], list[str]]:
    """(phrases quoted in a foreign language, which must appear word for word; Chinese claim clauses). A claim is a
    selling point (a marker such as 最 or 远高于), something said about us (about_us) or a price or rate we give
    (terms: "120 美元一条", "佣金 12%"). The rest of the owner's notes is instructions ("把这句去掉", "为什么写了
    CPS？") and background ("粉丝 20w", "她是法国人"), not words to keep, and neither is what stays in-house: a clause
    that says not to tell (secret) or one about our budget or bottom line (internal)."""
    quoted, claims = [], []
    for q in re.findall(r"[“\"「『]([^”\"」』\n]{3,})[”\"」』]", text or ""):
        (claims if HAN_RUN.search(q) else quoted).append(q.strip())
    for sentence in re.split(r"(?<=[。！？!?\n；;])", text or ""):
        if rule.get("instruction") and rule["instruction"].search(sentence.strip()) \
                and not re.search(r"[，,]", sentence):
            continue
        for clause in re.split(r"[，,、]+", sentence):
            clause = clause.strip(" “”\"「」『』。！!；;\n")
            if not HAN_RUN.search(clause) or any(rule.get(k) and rule[k].search(clause)
                                                 for k in ("instruction", "internal", "secret")):
                continue
            if any(rule.get(k) and rule[k].search(clause) for k in ("markers", "about_us")):
                claims.append(clause)
            elif rule.get("terms") and rule["terms"].search(clause) \
                    and not (rule.get("their_terms") and rule["their_terms"].search(clause)):
                claims.append(clause)
    return quoted, list(dict.fromkeys(claims))


def names_in(text: str) -> list[str]:
    """The Latin words of a claim that no translation changes: names and codes such as LootBar, Trustpilot, CPS or
    TikTok (a capital or a digit in them). Lower-case words ("giveaway", "brief") come out in the KOL's language."""
    return [w for w in LATIN.findall(text or "") if any(ch.isupper() or ch.isdigit() for ch in w)]


THOUSANDS = re.compile(r"(\d+(?:[.,]\d+)?)\s?(?:k(?![-\w])|mil\b|тыс)", re.IGNORECASE)


def amounts_in(text: str) -> list[float]:
    """numbers_in, plus "3K", "50 mil" and "50 тыс." read as thousands."""
    return numbers_in(text) + [float(n.replace(",", ".")) * 1000 for n in THOUSANDS.findall(text or "")]


def check_owner_wording(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    plain = " ".join(body.split()).casefold()
    zh_plain = ctx.zh.casefold()
    zh_grams = bigrams(ctx.zh)
    body_numbers = amounts_in(body)
    tolerance = float(rule.get("number_tolerance", 0.1))
    need = float(rule.get("min_coverage", 0.2))
    missing = []
    for text in ctx.owner_texts:
        quoted, claims = owner_claims(text, rule)
        missing += [f"原话「{q}」" for q in quoted if " ".join(q.split()).casefold() not in plain]
        for clause in claims:
            nums = owner_numbers(clause)
            lost_number = any(not any(abs(b - n) <= tolerance * max(abs(n), 1) for b in body_numbers) for n in nums)
            lost_word = any(w.casefold() not in plain and w.casefold() not in zh_plain for w in names_in(clause))
            grams = bigrams(clause)
            weak = bool(rule.get("markers") and rule["markers"].search(clause) and grams
                        and len(grams & zh_grams) / len(grams) < need)
            if lost_number or lost_word or weak:
                missing.append(f"主人说的「{clause}」")
    return missing


WITHDRAWAL = re.compile(r"withdraw|minimum|вывод|минимальн|retir|saque|retrait|auszahl|提现|最低", re.IGNORECASE)


def check_amounts_listed(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    """Amounts we put forward that the owner was not told about. Their own quote, figures from our earlier mails
    and the withdrawal minimum are not new offers."""
    said = set(numbers_in(ctx.needs_approval)) | {n for t in ctx.our_texts + ctx.kol_texts
                                                  for n in numbers_in(own_words(t))}
    out = []
    for amount in [m.group(0).strip() for s in sentences(body) if not WITHDRAWAL.search(s) for m in MONEY.finditer(s)]:
        values = numbers_in(amount)
        if values and not any(x in said for x in values):
            out.append(amount)
    return out


def check_coupon_count_listed(rule: dict, body: str, ctx: Context, v: dict) -> list[str]:
    if re.search(r"券|coupon|月卡", ctx.needs_approval or "", re.IGNORECASE):
        return []
    return [s for s in sentences(body) if any(p.search(s) for p in rule["patterns"])]


CHECKS = {
    "patterns": check_patterns,
    "reminder": check_reminder,
    "paypal_unasked": check_paypal_unasked,
    "profit_share_cap": check_profit_share_cap,
    "cps_opens_at_default": check_cps_opens_at_default,
    "sweetener_before_quote": check_sweetener_before_quote,
    "confirm_amount": check_confirm_amount,
    "unlisted_links": check_unlisted_links,
    "giveaway_games": check_giveaway_games,
    "reasking_received": check_reasking_received,
    "owner_wording": check_owner_wording,
    "amounts_listed": check_amounts_listed,
    "coupon_count_listed": check_coupon_count_listed,
}
NEEDS = {  # vocab entries each check reads
    "profit_share_cap": ("profit",),
    "cps_opens_at_default": ("profit", "pushback", "negation", "refusal", "comfort"),
    "sweetener_before_quote": ("price_quote", "fee_talk", "additive", "sweetener", "default_cps", "negation",
                               "refusal", "comfort"),
    "confirm_amount": ("acceptance", "negation", "refusal", "comfort"),
    "giveaway_games": ("giveaway", "negation", "refusal", "comfort"),
}


# ---------------------------------------------------------------- the verdict

def check(body: str, ctx: Context, rules: Rules | None = None) -> list[Violation]:
    rules = rules or load()
    text = without_signature(body, ctx.signature)
    found = []
    for rule in rules.rules:
        evidence = CHECKS[rule["check"]](rule, text, ctx, rules.vocab)
        if evidence:
            found.append(_violation(rule, evidence))
    return found


def revision_brief(violations: list[Violation]) -> str:
    """What the model is told to fix, one block per broken rule."""
    blocks = []
    for n, v in enumerate((v for v in violations if v.action == "revise"), 1):
        quotes = "; ".join(f'"{e}"' for e in v.evidence)
        blocks.append(f"{n}. Rule {v.rule}. Found: {quotes}\n   Fix: {v.fix}")
    return "\n".join(blocks)


def prompt_brief(rules: Rules | None = None) -> str:
    """Every rule in one line, for the model to keep while it writes: a draft that knows the rules up front does not
    have to break one before it is told (the playbook on a colleague's machine may be the empty template)."""
    rules = rules or load()
    return "\n".join(f"- {r['title_zh']}" for r in rules.rules)


def summary_zh(violations: list[Violation]) -> str:
    return "；".join(f"{v.title_zh}（{' / '.join(v.evidence)}）" for v in violations)


def review_notes(violations: list[Violation]) -> list[str]:
    notes = []
    for v in violations:
        if v.action != "review":
            continue
        if v.rule == "amounts-listed-for-owner":
            notes.append(f"草稿里写了金额 {'、'.join(v.evidence)}：审批前只能当提议，请确认金额")
        else:
            notes.append(f"{v.title_zh}：{' / '.join(v.evidence)}")
    return notes


def context_for(ctx: dict, *, signature: str = "", playbook: str = "", owner_texts: list[str] | None = None,
                zh: str = "", needs_approval: str = "", outgoing_attachments: bool = False) -> Context:
    """A Context from app.py's reply context (msg, earlier, ours, materials, subject)."""
    msg = ctx.get("msg") or {}
    theirs = [msg] + list(ctx.get("earlier") or [])
    return Context(
        kol_texts=[str(m.get("body") or "") for m in theirs],
        our_texts=[str(m.get("body") or "") for m in ctx.get("ours") or []],
        owner_texts=[t for t in owner_texts or [] if t and t.strip()],
        zh=zh, playbook=playbook, subject=str(msg.get("subject") or ctx.get("subject") or ""), signature=signature,
        needs_approval=needs_approval,
        kol_attachments=[str(n) for m in theirs for n in m.get("attachments") or []],
        materials=list(ctx.get("materials") or []), outgoing_attachments=outgoing_attachments)
