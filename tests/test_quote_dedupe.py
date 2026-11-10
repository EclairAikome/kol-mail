"""0.10: a conversation's mails quote each other, so the prompt of a long conversation said its first mails over and
over. Each mail now keeps its own words; of what it quotes, only what no other mail of the prompt says stays. The
rule a test here holds it to: nothing leaves the prompt that is not still in it somewhere else."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import replyflow  # noqa: E402

ME, KOL = "Jordan <me@example.com>", "Marcos <marcos@example.org>"


def quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.split("\n"))


def chain(count: int) -> list[dict]:
    """A conversation of `count` mails, oldest first: each answers the one before and quotes it whole (which quotes
    the one before that, and so on), the way Gmail does."""
    mails, previous = [], None
    for n in range(1, count + 1):
        ours = n % 2 == 0
        own = (f"Hi {'Marcos' if ours else 'Jordan'},\n\nThis is what mail number {n} says: the offer stands at "
               f"{100 * n} dollars for {n} streams, and point {n} of the plan is settled.\n\nBest,\n{'Jordan' if ours else 'Marcos'}")
        body = own
        if previous:
            who = ME if not ours else KOL
            body += f"\n\nOn Mon, Nov {n}, 2026 at 10:0{n % 10} AM {who} wrote:\n\n" + quote(previous["body"])
        previous = {"date": f"2026-11-{n:02d}", "sender": ME if ours else KOL, "recipients": ["x@example.org"],
                    "subject": "Re: March streams", "body": body, "own": own}
        mails.append(previous)
    return mails


def sentences(text: str) -> set[str]:
    return set(re.findall(r"mail number \d+ says", text))


def test_a_long_conversation_says_each_mail_once():
    mails = chain(12)[::-1]  # newest first, as the prompt has them
    before = sum(len(m["body"]) for m in mails)
    pruned = replyflow.without_repeats(mails)
    after = sum(len(m["body"]) for m in pruned)
    assert after < before / 4  # twelve mails that each carried all the earlier ones
    whole = "\n".join(m["body"] for m in pruned)
    assert sentences(whole) == {f"mail number {n} says" for n in range(1, 13)}  # nothing is lost
    for m, original in zip(pruned, mails):
        assert m["body"].startswith(original["own"])              # its own words whole and first
        assert m["body"].count("mail number") == 1                # and nobody else's
        assert m["date"] == original["date"]
        assert original["body"] == chain(12)[int(original["date"][-2:]) - 1]["body"]  # the mails given are not changed
    assert pruned[0]["body"].count(replyflow.QUOTE_OMITTED) == 1    # one line where the whole chain was


def test_a_quoted_mail_the_prompt_does_not_hold_stays_quoted():
    """The history a draft reads is the latest few mails. What an earlier one said survives only in the quotes:
    it must stay, once."""
    mails = chain(8)[::-1][:3]  # the prompt holds mails 8, 7 and 6; they quote 5 down to 1
    pruned = replyflow.without_repeats(mails)
    whole = "\n".join(m["body"] for m in pruned)
    assert sentences(whole) == {f"mail number {n} says" for n in range(1, 9)}
    for n in range(1, 6):  # each of the five mails only quotes kept: once, in the newest mail that quotes it
        assert whole.count(f"mail number {n} says") == 1
    assert "mail number 5 says" in pruned[0]["body"]
    assert "the offer stands at 100 dollars for 1 streams" in pruned[0]["body"]
    # each surviving quote still stands under the line that says who wrote it and when
    lines = pruned[0]["body"].split("\n")
    for n in range(1, 6):
        at = next(i for i, x in enumerate(lines) if f"mail number {n} says" in x)
        header = next(x for x in reversed(lines[:at]) if "wrote:" in x)
        assert f"Nov {n + 1}, 2026" in header, (n, header)  # the mail that quoted it was written the day after
    assert not any("Nov 8, 2026" in x and x.lstrip().startswith(">") for x in lines)  # a header of nothing kept is gone


def test_a_rewrapped_quote_is_still_a_repeat():
    own = "We can do five sponsored streams in March for four hundred dollars, paid after the last one is live."
    ours = {"body": f"Hi Marcos,\n\n{own}\n\nBest,\nJordan"}
    wrapped = ("> Hi Marcos,\n>\n> We can do five sponsored streams in March\n> for four hundred dollars, paid after the "
               "last one\n> is live.\n>\n> Best,\n> Jordan")
    theirs = {"body": f"Deal!\n\nOn Thu, 6 Nov 2026 at 15:53, Jordan <me@example.com> wrote:\n{wrapped}"}
    out = replyflow.without_repeats([theirs, ours])[0]["body"]
    assert out.startswith("Deal!") and "four hundred" not in out and replyflow.QUOTE_OMITTED in out
    assert "On Thu, 6 Nov 2026 at 15:53, Jordan <me@example.com> wrote:" in out  # who was quoted, and when


def test_only_what_is_said_elsewhere_goes_even_inside_a_repeated_quote():
    fee = "The fee is 400 dollars for the whole month of March, five streams, paid after the last one."
    ours = {"body": f"Hi,\n\n{fee}\n\nJordan"}
    theirs = {"body": "See my note below.\n\nOn Nov 6, 2026 Jordan <me@example.com> wrote:\n"
                      f"> Hi,\n>\n> {fee}\n> PS from Marcos's agent: we need 700.\n>\n> Jordan"}
    out = replyflow.without_repeats([theirs, ours])[0]["body"]
    assert "we need 700" in out and "The fee is 400 dollars" not in out
    assert out.endswith("> PS from Marcos's agent: we need 700.\n>\n> Jordan")  # a repeat shorter than its notice stays
    tiny = {"body": "Yes.\n\nOn Nov 6, 2026 Jordan <me@example.com> wrote:\n> Hi,\n> Jordan"}
    assert replyflow.without_repeats([tiny, ours])[0] is tiny
    short = {"body": "Ok.\n\nOn Nov 6, 2026 Jordan <me@example.com> wrote:\n> 1400$\n> for all of it"}
    assert "1400$" in replyflow.without_repeats([short, {"body": "My rate is 400$ flat."}])[0]["body"]  # 400$ is not 1400$


def test_a_quoted_line_is_a_repeat_only_where_one_paragraph_says_it():
    """Found in review: with every line break read as a space, a quoted line could "repeat" words stitched from
    two paragraphs of another mail, here into the opposite of what that mail says."""
    ours = {"body": "Terms:\nwe never pay a deposit\n\nthe fee is paid after the video goes live on the channel\nBest"}
    theirs = {"body": "ok\n\nOn Mon, Jan 5, 2026 at 10:00 AM Bob <b@example.org> wrote:\n"
                      "> a deposit the fee is paid after the video goes live on the channel\n> and that was agreed last week already"}
    assert "a deposit the fee is paid" in replyflow.without_repeats([theirs, ours])[0]["body"]
    # a line wrapped differently inside one paragraph is still the same words
    ours = {"body": "Terms:\n\nthe fee is paid after the video goes live\non the channel, and never before that day\n\nBest"}
    theirs = {"body": "ok\n\nOn Mon, Jan 5, 2026 at 10:00 AM Bob <b@example.org> wrote:\n"
                      "> the fee is paid after the video\n> goes live on the channel, and never before that day"}
    assert "goes live" not in replyflow.without_repeats([theirs, ours])[0]["body"]


def test_a_mark_inside_a_line_is_part_of_what_it_says():
    ours = {"body": "The link is http://example.org/a b and then we pay 300 dollars after the video is live OK"}
    theirs = {"body": "see\n\nOn Mon, Jan 5, 2026 at 10:00 AM Bob <b@example.org> wrote:\n"
                      "> The link is http://example.org/a>b and then we pay 300 dollars after the video is live OK"}
    assert "example.org/a>b" in replyflow.without_repeats([theirs, ours])[0]["body"]
    assert replyflow._words("> > Hello   World ->\n>there") == "hello world -> there"


def test_words_the_prompt_cuts_off_do_not_count_as_said():
    """Found in review: an earlier mail is shown up to EARLIER_LIMIT characters. What it says after that is not
    in the prompt, so a quote of it elsewhere must stay."""
    clause = "The exclusivity runs until March 9 and the price for it is 1800 dollars in total, as we agreed."
    long_own = "Background.\n\n" + "filler words that go on and on. " * 300 + "\n\n" + clause
    assert long_own.index(clause) > replyflow.EARLIER_LIMIT
    earlier = {"date": "d1", "subject": "s", "body": long_own}
    newest = {"date": "d2", "subject": "s", "body": "Fine.\n\nOn Mon, Jan 5, 2026 at 10:00 AM Bob <b@example.org> wrote:\n> " + clause}
    ctx = {"channel": "email", "msg": newest, "zh": "", "earlier": [earlier], "ours": [], "materials": []}
    assert "\n".join(replyflow.context_sections("b", ctx)).count(clause) == 1
    # the same through a newer mail's quote that lies past its own cut
    quoting = {"date": "d2", "subject": "s", "body": "x " * 3100 + "\n\nOn Mon, Jan 5, 2026 at 10:00 AM Bob <b@example.org> wrote:\n> " + clause}
    older = {"date": "d1", "subject": "s", "body": "Yes.\n\nOn Sun, Jan 4, 2026 at 9:00 AM Bob <b@example.org> wrote:\n> " + clause}
    out = replyflow.without_repeats([quoting, older], [replyflow.EARLIER_LIMIT, replyflow.EARLIER_LIMIT])
    shown = "\n".join(m["body"][:replyflow.EARLIER_LIMIT] for m in out)
    assert clause in shown
    assert clause not in replyflow.without_repeats([quoting, older])[1]["body"]  # shown whole, once is enough


def test_where_the_quote_starts():
    split = replyflow.split_quote
    assert split("Hi\n\nOn Mon, Nov 3, 2026 at 5:35 PM Jordan <me@example.com> wrote:\n> old")[0] == "Hi"
    assert split("Hola\n\nEl lun, 3 nov 2026 a las 17:35, Jordan (<me@example.com>) escribió:\n> viejo")[0] == "Hola"
    assert split("Привет\n\nпн, 3 нояб. 2026 г. в 17:35, Jordan <me@example.com> написал:\n> старое")[0] == "Привет"
    assert split("Hi\n\n-----Original Message-----\nFrom: Jordan\nold")[0] == "Hi"
    outlook = "Thanks.\n\nFrom: Jordan <me@example.com>\nSent: Monday, November 3, 2026 5:35 PM\nTo: Marcos\nSubject: Re: x\n\nold words"
    assert split(outlook) == ("Thanks.", outlook[len("Thanks.\n\n"):])
    assert split("您好\n\n发件人: Jordan <me@example.com>\n发送时间: 2026年11月3日\n收件人: Marcos\n主题: x\n\n旧的")[0] == "您好"
    assert split("Fine.\n\n> a quoted line\n> another one")[0] == "Fine."
    # left whole: an answer between the quoted lines, a mail that starts with its quote, ordinary words that look alike
    inline = "On Mon, Nov 3, 2026 Jordan <me@example.com> wrote:\n> price?\n\n400 dollars.\n\n> when?\n\nMarch."
    for body in (inline, "Hi\n\n" + inline, "> quoted first\n\nthen my answer below", "As I wrote: no prepayment.\n\nThanks",
                 "From: the team\nwith love", "Costs:\n> 5 streams means more\nand that is my point", "", "just words"):
        assert split(body) == (body, ""), body
    assert split(None) == ("", "")


def test_the_prompt_sections_use_it_for_mail_and_leave_chats_alone():
    mails = chain(6)[::-1]
    ctx = {"channel": "email", "msg": mails[0], "zh": "", "earlier": [mails[2], mails[4]], "ours": [mails[1], mails[3], mails[5]],
           "materials": []}
    kept = [dict(m) for m in mails]
    parts = replyflow.context_sections("b", ctx)
    whole = "\n".join(parts)
    assert whole.count("mail number 1 says") == 1 and sentences(whole) == {f"mail number {n} says" for n in range(1, 7)}
    assert [p.split(">>")[0] for p in parts] == ["<<b:KOL_EMAIL", "<<b:KOL_EARLIER_EMAIL", "<<b:KOL_EARLIER_EMAIL",
                                                 "<<b:OUR_EARLIER_EMAIL", "<<b:OUR_EARLIER_EMAIL", "<<b:OUR_EARLIER_EMAIL"]
    assert mails == kept                                       # the context a later call reuses is untouched
    assert replyflow.context_sections("b", ctx) == parts       # and the same words come out again (the cached prefix)
    chat = {"channel": "telegram", "msg": {"date": "d", "subject": "s", "body": "> a line that starts like a quote\n> and one more"},
            "earlier": [{"date": "", "subject": "t", "body": "> a line that starts like a quote\n> and one more"}], "ours": []}
    assert "\n".join(replyflow.context_sections("b", chat)).count("starts like a quote") == 2
    compose = {"to": ["marcos@example.org"], "earlier": [mails[0], mails[2]], "ours": [mails[1]], "language": "auto"}
    assert "\n".join(replyflow.compose_sections("b", compose)).count("mail number 1 says") == 1
    system = " ".join(replyflow.strategy_system("Me", "BD", "C", "b", "pb").split())
    assert "the quote is replaced by a line saying so" in system and "elsewhere in these sections" in replyflow.QUOTE_OMITTED
