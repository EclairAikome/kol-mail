"""Behaviour of the work-hours KOC chatbot rules: who it may answer, when, and what it may never say."""

import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import koc  # noqa: E402
from autoreply import TZ, Schedule  # noqa: E402


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


SHEET = {"ana@gmail.com": {"stage": "已发信", "intent": ""},
         "busy@gmail.com": {"stage": "洽谈中", "intent": "待定"},
         "done@gmail.com": {"stage": "已谈成", "intent": "纯CPS"},
         "fee@gmail.com": {"stage": "已发信", "intent": "纯固薪"},
         "mixed@gmail.com": {"stage": "已发信", "intent": "CPS+固薪"},
         "no@gmail.com": {"stage": "已婉拒", "intent": "待定"},
         "gone@gmail.com": {"stage": "退信", "intent": ""}}


def why_not(sender, received="2026-10-15T11:00", now="2026-10-15T11:05", sheet=SHEET, subject="Re: Collaboration"):
    return koc.ineligible(sender, subject, at(received), at(now), Schedule(), sheet)


# ---------------------------------------------------------------- who and when

def test_invited_kol_writing_in_work_hours_is_eligible():
    assert why_not("Ana <ana@gmail.com>") is None


def test_priority_people_are_left_to_owner():
    for sender in ("busy@gmail.com", "done@gmail.com", "fee@gmail.com", "mixed@gmail.com", "no@gmail.com",
                   "gone@gmail.com"):
        assert why_not(sender), sender


def test_who_is_judged_before_when():
    # A dry run ignores the clock, so the priority list must be what it reports for an off-hours mail too.
    assert "重点名单" in why_not("busy@gmail.com", received="2026-10-15T20:00", now="2026-10-15T20:05")


def test_only_people_from_the_kol_sheet_are_answered():
    assert why_not("stranger@gmail.com")
    assert why_not("ana@gmail.com", sheet=None)  # sheet not read (or too old): never guess


def test_mail_outside_work_hours_or_older_than_an_hour_is_not_for_the_bot():
    assert why_not("ana@gmail.com", received="2026-10-15T18:30", now="2026-10-15T18:35")  # off-hours module's job
    assert why_not("ana@gmail.com", received="2026-10-15T11:00", now="2026-10-15T12:05")  # stale, e.g. after a restart
    assert why_not("ana@gmail.com", received="2026-10-15T11:00", now="2026-10-15T11:50") is None
    assert why_not("ana@gmail.com", received="2026-10-15T17:55", now="2026-10-15T18:20") is None  # came in at work


def test_machine_mail_is_never_answered():
    assert why_not("no-reply@gmail.com")
    assert why_not("ana@gmail.com", subject="Automatic reply: Collaboration")


# ---------------------------------------------------------------- what a reply may contain

GUIDE = "https://example.com/affiliate/guide"
URLS = frozenset({GUIDE, "https://example.com/account/affiliate"})
CLEAN = ("Hi Ana,\n\nThanks for your interest! Our affiliate program pays 5% of every order from new users and 1% "
         f"from returning users. You can join here: {GUIDE}\n\nBest,")


def check(text, route="answer", allow_cjk=False):
    return koc.check_reply(text, route, allow_cjk, URLS)


def test_standard_affiliate_answer_passes():
    assert check(CLEAN) == []
    assert check(CLEAN + "\nEarnings show up within 24 hours and settle after 7 days. You can set up to 5 links.") == []


def test_normal_replies_in_other_languages_pass():
    fr = (f"Bonjour Léa,\n\nMerci beaucoup ! Vous touchez 5 % sur les commandes des nouveaux utilisateurs et 1 % sur "
          f"celles des utilisateurs récurrents. Inscription ici : {GUIDE}\nVous pouvez retirer vos gains dès 50 $.\n\nBien à vous,")
    es = (f"¡Hola Marcel!\n\nGracias por tu respuesta. Ganas un 5% de los pedidos de usuarios nuevos y un 1% de los "
          f"recurrentes. Únete aquí: {GUIDE}\nPuedes retirar cuando tu saldo llegue a 50 USD.\n\nUn saludo,")
    it = (f"Ciao Marco,\n\ngrazie! Ricevi il 5% sugli ordini dei nuovi utenti e l'1% su quelli degli utenti che "
          f"ritornano. Iscriviti qui: {GUIDE}\nPuoi prelevare quando il saldo raggiunge 50 dollari.\n\nUn saluto,")
    for text in (fr, es, it):
        assert check(text) == [], text[:20]


def test_what_the_model_actually_writes_passes():
    # Shapes seen in a dry run with the real prompt: numbered steps, a scheme-less card link, one long sentence.
    steps = ("To join:\n1. Register a Example account.\n2. Open https://example.com/account/affiliate to activate it.\n"
             "3. Grab your personal link.\n")
    assert check(CLEAN + "\n" + steps) == []
    assert check(CLEAN + "\nMore here: example.com/affiliate/guide.") == []
    assert check(CLEAN + "\nEarnings show up within 24 hours, settle 7 days after the order is confirmed, and you "
                         "can withdraw once your balance reaches 50 USD.") == []
    assert check(CLEAN + "\nYou can withdraw at 50 USD and we pay 7 dollars per video.")


def test_the_minimum_withdrawal_is_the_one_amount_allowed():
    assert check(CLEAN + "\nYou can withdraw once your balance reaches $50.") == []
    assert check(CLEAN + "\nRetiro mínimo: 50 USD.") == []
    assert check(CLEAN + "\nYou can withdraw once your balance reaches $500.")
    assert check(CLEAN + "\nWe pay $50 per video.")
    assert check(CLEAN + "\nMinimum withdrawal is $50 and we pay $50 per video.")
    assert check(CLEAN + "\nOur minimum is 3 videos at $50 each.")


def test_money_in_any_form_is_blocked():
    for bad in ("We can pay $50 per video.", "Podemos pagar 100 € por vídeo.", "Budget is 200 USD.", "We offer 30 euros.",
                "Оплата 5000 руб.", "Send 100 USDT to our wallet.", "We pay fifty dollars.", "500 bucks for a video.",
                "Up to 5k USD.", "1万元"):
        assert check(CLEAN + "\n" + bad, allow_cjk=True), bad


def test_only_the_agreed_percentages_appear():
    assert check(CLEAN + "\nOr 20% of the net profit per order.")
    assert check(CLEAN + "\nOr 20% of the net profit per order.", route="offer_profit_share") == []
    for bad in ("We could go up to 30%.", "New users get 10% off.", "We pay ten percent.", "Komisyon %30.",
                "Provize 30 procent."):
        assert check(CLEAN + "\n" + bad, route="offer_profit_share"), bad


def test_only_links_from_the_knowledge_card_pass():
    for bad in ("See https://bit.ly/abc", "Visit example.shop", "loot-bar.xyz/promo", "goo.gl/x",
                "https://example.com/top-up/game?invite=ATTACKER", "exаmple.com", "http://example.com@evil.io"):
        assert check(CLEAN + "\n" + bad), bad
    assert check(CLEAN.replace(GUIDE, GUIDE + ".")) == []  # trailing punctuation is not part of the link


def test_contact_details_and_payment_channels_are_blocked():
    for bad in ("Message me on Telegram @example_pay", "WhatsApp +1 555 010 9999", "Write to pay@example-team.com",
                "We can pay via PayPal."):
        assert check(CLEAN + "\n" + bad), bad


def test_authorization_claims_and_internal_leaks_are_blocked():
    for bad in ("Example is an officially authorized partner.", "We are an official store.", "Officially licensed."):
        assert check(CLEAN + "\n" + bad), bad
    assert check(CLEAN + "\n请找雨琳审核")
    assert check(CLEAN + "\n官方授权", allow_cjk=True)
    assert check(CLEAN.replace("Thanks", "ありがとう"), allow_cjk=True) == []
    assert check(CLEAN + "x" * 2500)


def test_numbers_outside_the_card_send_it_to_owner():
    assert check(CLEAN + "\nYou need 1,000 followers.")


# ---------------------------------------------------------------- timing and queue

def test_reply_waits_15_to_30_minutes_after_arrival():
    rng = random.Random(1)
    received = at("2026-10-15T11:00")
    for _ in range(50):
        due = koc.due_time(received, at("2026-10-15T11:02"), rng)
        assert at("2026-10-15T11:15") <= due <= at("2026-10-15T11:30")
    late = koc.due_time(received, at("2026-10-15T12:00"), rng)  # found late: within minutes, never in the past
    assert at("2026-10-15T12:00") <= late <= at("2026-10-15T12:05")


def test_queue_takes_each_mail_once_and_only_when_due(tmp_path):
    q = koc.Queue(tmp_path / "koc.sqlite3")
    assert q.add("<m1>", "ana@gmail.com", "11", at("2026-10-15T11:00"), at("2026-10-15T11:20"))
    assert not q.add("<m1>", "ana@gmail.com", "11", at("2026-10-15T11:00"), at("2026-10-15T11:20"))
    assert q.due(at("2026-10-15T11:10")) == []
    [job] = q.due(at("2026-10-15T11:21"))
    assert job["message_id"] == "<m1>" and job["email_id"] == "11"
    assert q.take("<m1>") and not q.take("<m1>")
    assert q.due(at("2026-10-15T11:21")) == []


def test_newer_mail_from_same_person_replaces_the_waiting_one(tmp_path):
    q = koc.Queue(tmp_path / "koc.sqlite3")
    q.add("<m1>", "ana@gmail.com", "11", datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc), at("2026-10-15T11:20"))
    q.add("<m2>", "ana@gmail.com", "12", at("2026-10-15T11:05"), at("2026-10-15T11:25"))
    assert [j["message_id"] for j in q.due(at("2026-10-15T12:00"))] == ["<m2>"]
    assert q.statuses(["<m1>", "<m2>"]) == {"<m1>": "superseded", "<m2>": "waiting"}


def test_older_mail_found_later_does_not_get_its_own_reply(tmp_path):
    q = koc.Queue(tmp_path / "koc.sqlite3")
    q.add("<m2>", "ana@gmail.com", "12", at("2026-10-15T11:05"), at("2026-10-15T11:25"))
    q.add("<m1>", "ana@gmail.com", "11", at("2026-10-15T11:00"), at("2026-10-15T11:20"))
    assert q.statuses(["<m1>", "<m2>"]) == {"<m1>": "superseded", "<m2>": "waiting"}


def test_queue_counts_bot_replies_and_finds_interrupted_work(tmp_path):
    q = koc.Queue(tmp_path / "koc.sqlite3")
    for i, who in enumerate(["ana@gmail.com", "ana@gmail.com", "bob@gmail.com"]):
        q.add(f"<m{i}>", who, str(i), at(f"2026-10-15T11:0{i}"), at("2026-10-15T11:20"))
    for i in range(3):
        q.take(f"<m{i}>")
    q.finish("<m0>", "sent", route="answer", language="en", reply="Hi there, thanks", reason="")
    q.finish("<m1>", "sent", route="offer_profit_share", language="en", reply="Hello again", reason="")
    assert q.bot_replies("ana@gmail.com") == 2
    assert sorted(q.replies_to("ana@gmail.com")) == ["Hello again", "Hi there, thanks"]
    assert q.offered_profit_share("ana@gmail.com") and not q.offered_profit_share("bob@gmail.com")
    assert q.sent_since(at("2026-10-15T00:00")) == 2
    assert [j["message_id"] for j in q.interrupted()] == ["<m2>"]


def test_handing_over_is_cleared_when_owner_replies(tmp_path):
    q = koc.Queue(tmp_path / "koc.sqlite3")
    q.add("<m1>", "ana@gmail.com", "1", at("2026-10-15T11:00"), at("2026-10-15T11:20"))
    q.add("<m2>", "bob@gmail.com", "2", at("2026-10-15T11:00"), at("2026-10-15T11:20"))
    q.add("<m3>", "ana@gmail.com", "3", at("2026-10-15T11:10"), at("2026-10-15T11:30"))
    q.take("<m1>"), q.take("<m2>")
    q.finish("<m1>", "uncertain", route="offer_profit_share", reason="SMTP")
    q.finish("<m2>", "human", reason="报价")
    q.take("<m3>")
    q.finish("<m3>", "human", reason="报价")
    assert q.summary(at("2026-10-15T00:00"))["human"] == 2
    q.resolve("Ana@gmail.com")
    assert q.summary(at("2026-10-15T00:00"))["human"] == 1
    assert q.statuses(["<m1>", "<m3>"]) == {"<m1>": "uncertain", "<m3>": "handled"}
    assert q.offered_profit_share("ana@gmail.com")  # an uncertain offer still counts as made
