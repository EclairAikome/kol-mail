"""Phone approval: one case per incoming message, a newer message supersedes, two taps can never both send,
and only the paired owner can drive the bot."""

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

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
