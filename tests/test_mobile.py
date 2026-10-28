"""Phone approval: one case per incoming message, a newer message supersedes, two taps can never both send,
and only the paired owner can drive the bot."""

import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mobile  # noqa: E402
from autoreply import TZ  # noqa: E402


def store(tmp_path):
    return mobile.Cases(tmp_path / "m.sqlite3", tz=TZ)


def test_one_case_per_message_and_newer_supersedes(tmp_path):
    c = store(tmp_path)
    first = c.open_case("email", "Ana@Gmail.com", "<m1>", who="Ana")
    assert first and c.open_case("email", "ana@gmail.com", "<m1>") is None
    c.move(first, ("new",), "strategies")
    second = c.open_case("email", "ana@gmail.com", "<m2>")
    assert c.get(first)["status"] == "superseded" and c.get(second)["status"] == "new"
    other = c.open_case("email", "bob@gmail.com", "<m3>")
    assert c.get(second)["status"] == "new" and other


def test_status_moves_are_compare_and_set(tmp_path):
    c = store(tmp_path)
    case = c.open_case("email", "ana@gmail.com", "<m1>")
    c.move(case, ("new",), "strategies")
    c.move(case, ("strategies",), "drafted")
    assert c.move(case, ("drafted",), "confirming")
    assert c.move(case, ("confirming",), "sending")
    assert not c.move(case, ("confirming",), "sending")  # the second tap on 确认发送 does nothing
    assert not c.move(case, mobile.OPEN, "skipped")       # nor can a late 不回了 undo a send


def test_messages_map_back_to_their_case(tmp_path):
    c = store(tmp_path)
    a = c.open_case("email", "ana@gmail.com", "<m1>")
    b = c.open_case("telegram", "42", "tg:42:7")
    c.remember_message(a, [100, 101])
    c.remember_message(b, [102])
    assert c.by_message(101)["id"] == a and c.by_message(102)["id"] == b and c.by_message(999) is None
    assert c.latest_open()["id"] in (a, b)


def test_stuck_cases_after_a_crash(tmp_path):
    c = store(tmp_path)
    a = c.open_case("email", "ana@gmail.com", "<m1>")
    b = c.open_case("email", "bob@gmail.com", "<m2>")
    c.update(b, status="sending")
    assert {x["id"] for x in c.stuck()} == {a, b}


def test_count_since_uses_real_times(tmp_path):
    c = store(tmp_path)
    c.open_case("email", "ana@gmail.com", "<m1>")
    assert c.count_since("email", datetime.now(TZ) - timedelta(hours=1)) == 1
    assert c.count_since("email", datetime.now(TZ) + timedelta(hours=1)) == 0


def test_pairing_code_is_single_use_and_expires():
    p = mobile.Pairing()
    code = p.new()
    assert not p.take("/start 000000" if code != "000000" else "/start 111111")
    assert not p.take(f"/start{code}")
    assert p.take(f"/start {code}")
    assert not p.take(f"/start {code}")
    code = p.new()
    p.expires = time.time() - 1
    assert not p.take(f"/start {code}")


def test_long_texts_split_under_the_telegram_limit():
    text = "\n".join("第 %d 行 " % i + "字" * 80 for i in range(200))
    parts = mobile.split_text(text)
    assert len(parts) > 1 and all(len(p) <= mobile.TEXT_LIMIT for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")


def test_send_button_only_when_side_by_side_is_complete():
    flat = lambda rows: [d for row in rows for _, d in row]
    ver = mobile.version("draft text")
    assert f"p:7:{ver}" in flat(mobile.draft_buttons(7, True, ver))
    assert not any(d.startswith("p:") for d in flat(mobile.draft_buttons(7, False, ver)))
    assert mobile.version("draft text") != mobile.version("draft text, edited")


# ---------------------------------------------------------------- Telegram out of reach (a colleague's VPN, 2026-10-28)

def call_error(handler) -> mobile.BotError:
    async def go():
        bot = mobile.Bot("123456:" + "a" * 35)
        await bot.client.aclose()
        bot.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await bot.call("getUpdates")
        except mobile.BotError as e:
            return e
        finally:
            await bot.close()
    return asyncio.run(go())


def test_telegram_errors_say_whether_trying_again_can_help():
    def unreachable(request):
        raise httpx.ConnectError("no route to host", request=request)
    e = call_error(unreachable)
    assert e.transient and str(e) == "连不上 Telegram：ConnectError"
    assert call_error(lambda r: httpx.Response(502, json={"ok": False, "error_code": 502,
                                                          "description": "Bad Gateway"})).transient
    assert call_error(lambda r: httpx.Response(504, text="<html>Gateway Time-out</html>")).transient
    assert call_error(lambda r: httpx.Response(429, json={"ok": False, "error_code": 429,
                                                          "description": "Too Many Requests: retry after 5"})).transient
    e = call_error(lambda r: httpx.Response(403, json={"ok": False, "error_code": 403,
                                                      "description": "Forbidden: bot was blocked by the user"}))
    assert not e.transient and "blocked" in str(e)
    # not Telegram answering at all: a VPN's or proxy's own page, even with 200 or 403
    assert call_error(lambda r: httpx.Response(403, text="<html>Access denied by proxy</html>")).transient
    assert call_error(lambda r: httpx.Response(200, json=None)).transient
    assert call_error(lambda r: httpx.Response(200, json=[])).transient


class FakeBot:
    def __init__(self, failures=()):
        self.failures = list(failures)  # raised one per send before the sends work
        self.sent: list[tuple] = []

    async def send(self, chat_id, text, buttons=None, reply_to=None):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((chat_id, text, buttons, reply_to))
        return [500 + len(self.sent)]


UNREACHABLE = mobile.BotError("连不上 Telegram：ConnectError", transient=True)


def test_pushes_held_while_telegram_was_unreachable_go_out_once_it_answers(tmp_path):
    c = store(tmp_path)
    waiting = c.open_case("email", "ana@gmail.com", "<m1>")
    c.move(waiting, ("new",), "strategies")
    answered = c.open_case("email", "bob@gmail.com", "<m2>")
    c.move(answered, ("new",), "handled")
    long_ago = time.time() - mobile.HOLD_SECONDS - 1  # a weekend without Telegram
    c.hold(42, f"📩 #{waiting} 新邮件", mobile.strategy_buttons(waiting, 2), waiting, None, now=long_ago)
    c.hold(42, f"📩 #{answered} 新邮件", mobile.strategy_buttons(answered, 2), answered, None)  # done meanwhile
    c.hold(42, f"#{answered} 已经在电脑上回复了。", None, answered, None)  # a note still matters
    c.hold(42, "收到，#1 处理中…", None, waiting, None, now=long_ago)  # a stale note does not
    bot = FakeBot([UNREACHABLE])
    dropped = []
    assert asyncio.run(mobile.resend_held(bot, 42, c, note=lambda *a: dropped.append(a))) == 0
    assert len(c.held()) == 4 and bot.sent == [] and dropped == []  # still out of reach: everything stays
    assert asyncio.run(mobile.resend_held(bot, 42, c, note=lambda *a: dropped.append(a))) == 2
    # the case still waits for the owner, so its card goes however long Telegram was away
    assert [s[1] for s in bot.sent] == [f"📩 #{waiting} 新邮件", f"#{answered} 已经在电脑上回复了。"]
    assert [d for row in bot.sent[0][2] for _, d in row] == [f"s:{waiting}:0", f"s:{waiting}:1", f"x:{waiting}"]
    assert c.held() == [] and c.by_message(501)["id"] == waiting  # a tap on the late push finds its case
    assert len(dropped) == 2


def test_a_held_draft_that_was_rewritten_meanwhile_is_not_sent(tmp_path):
    c = store(tmp_path)
    case = c.open_case("email", "ana@gmail.com", "<m1>")
    c.update(case, status="drafted", body="first draft")
    c.hold(42, "✍️ first draft", mobile.draft_buttons(case, True, mobile.version("first draft")), case, None)
    c.update(case, body="second draft")
    c.hold(42, "✍️ second draft", mobile.draft_buttons(case, True, mobile.version("second draft")), case, None)
    bot = FakeBot()
    assert asyncio.run(mobile.resend_held(bot, 42, c)) == 1 and [s[1] for s in bot.sent] == ["✍️ second draft"]


def test_held_pushes_never_go_to_a_phone_paired_since(tmp_path):
    c = store(tmp_path)
    c.hold(42, "for the old chat", None, None, None)
    bot = FakeBot()
    assert asyncio.run(mobile.resend_held(bot, 77, c)) == 0 and c.held() == [] and bot.sent == []


def test_a_push_that_fails_every_resend_is_given_up(tmp_path):
    c = store(tmp_path)
    c.hold(42, "Telegram keeps timing out on this one", None, None, None)
    bot = FakeBot([mobile.BotError("连不上 Telegram：ReadTimeout", transient=True)] * mobile.HOLD_TRIES)
    for _ in range(mobile.HOLD_TRIES - 1):
        asyncio.run(mobile.resend_held(bot, 42, c))
        assert len(c.held()) == 1
    asyncio.run(mobile.resend_held(bot, 42, c))
    assert c.held() == [] and bot.sent == []


def test_a_held_push_telegram_refuses_for_good_is_dropped_not_retried_forever(tmp_path):
    c = store(tmp_path)
    c.hold(42, "too long for Telegram", None, None, None)
    c.hold(42, "fine", None, None, None)
    bot = FakeBot([mobile.BotError("Bad Request: message is too long")])
    assert asyncio.run(mobile.resend_held(bot, 42, c)) == 1
    assert [s[1] for s in bot.sent] == ["fine"] and c.held() == []


def test_only_the_newest_pushes_are_held(tmp_path):
    c = store(tmp_path)
    for i in range(mobile.HOLD_MAX + 5):
        c.hold(42, f"push {i}", None, None, None)
    held = c.held()
    assert len(held) == mobile.HOLD_MAX and held[-1]["text"] == f"push {mobile.HOLD_MAX + 4}"


def test_a_push_that_meets_no_network_is_held_and_one_telegram_refuses_is_not(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "cases", store(tmp_path))
    monkeypatch.setattr(app, "owner_chat", lambda: 42)
    monkeypatch.setitem(app.state, "mobile_error", None)
    bot = FakeBot([UNREACHABLE, mobile.BotError("Forbidden: bot was blocked by the user")])
    monkeypatch.setattr(app, "current_bot", lambda: bot)
    assert asyncio.run(app.tell_owner("📩 #3 新邮件", [[("方案 1", "s:3:0")]], 3)) == []
    [held] = app.cases.held()
    assert held["text"] == "📩 #3 新邮件" and held["case_id"] == 3 and held["chat"] == 42
    assert "自动补发" in app.state["mobile_error"]
    assert asyncio.run(app.tell_owner("#4 note")) == []
    assert len(app.cases.held()) == 1 and app.state["mobile_error"].startswith("手机审稿推送失败")


def test_pushes_that_wait_show_on_the_status_bar_until_they_are_out(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "cases", store(tmp_path))
    monkeypatch.setattr(app, "owner_chat", lambda: 42)
    monkeypatch.setitem(app.state, "mobile_error", None)
    monkeypatch.setitem(app.bot_state, "resending", True)
    app.cases.hold(42, "📩 #3 新邮件", None, None, None)
    asyncio.run(app.resend_pushes(FakeBot([UNREACHABLE])))  # the poll works, the send does not
    assert app.state["mobile_error"] == app.HELD_NOTE.format(1) and "自动补发" in app.state["mobile_error"]
    assert not app.bot_state["resending"]
    asyncio.run(app.resend_pushes(FakeBot()))
    assert app.state["mobile_error"] is None and app.cases.held() == []
