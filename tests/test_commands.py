"""Owner commands on the phone bot: only whole-message commands count, dates parse the way a person writes them,
and holidays are added and removed without touching the rest of autoreply.json."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autoreply  # noqa: E402
import mobile  # noqa: E402

TODAY = date(2026, 10, 19)


def test_commands_are_whole_messages():
    assert mobile.parse_command("/template") == ("template", "")
    assert mobile.parse_command("模板") == ("template", "")
    assert mobile.parse_command("/AI") == ("ai", "")
    assert mobile.parse_command("/status@KOLMail_bot") == ("status", "")
    assert mobile.parse_command("/holiday 10-25 11-02") == ("holiday", "10-25 11-02")
    assert mobile.parse_command("休假 12月24日") == ("holiday", "12月24日")
    assert mobile.parse_command("模板 语气再热情点") is None   # a comment on a draft, not a command
    assert mobile.parse_command("把第二段改短") is None


def test_dates_as_people_write_them():
    days = mobile.parse_dates("10-25 到 11-02", TODAY)
    assert days[0] == date(2026, 10, 25) and days[-1] == date(2026, 11, 2) and len(days) == 9
    assert mobile.parse_dates("2026-12-24", TODAY) == [date(2026, 12, 24)]
    assert mobile.parse_dates("12月24日", TODAY) == [date(2026, 12, 24)]
    assert mobile.parse_dates("01-03", TODAY) == [date(2027, 1, 3)]          # already past this year: next year
    assert mobile.parse_dates("12-30 01-02", TODAY)[-1] == date(2027, 1, 2)


@pytest.mark.parametrize("bad", ["", "下周", "11-02 10-25", "10-01 12-31", "1-2 1-3 1-4"])
def test_bad_dates_are_refused(bad):
    with pytest.raises(ValueError):
        mobile.parse_dates(bad, TODAY)


def test_holidays_change_and_nothing_else(tmp_path):
    path = tmp_path / "autoreply.json"
    path.write_text(json.dumps({"email_enabled": True, "holidays": ["2026-12-25"], "templates": {"email": {"en": "x"}}}),
                    encoding="utf-8")
    added = autoreply.change_holidays(path, [date(2026, 12, 24), date(2026, 12, 25)], add=True)
    assert added == [date(2026, 12, 24)]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["holidays"] == ["2026-12-24", "2026-12-25"] and raw["email_enabled"] is True
    autoreply.change_holidays(path, [date(2026, 12, 25)], add=False)
    assert json.loads(path.read_text(encoding="utf-8"))["holidays"] == ["2026-12-24"]
    assert autoreply.load_config(path).schedule.holidays == frozenset({date(2026, 12, 24)})


def test_menu_names_are_valid_telegram_commands():
    import re
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", c["command"]) for c in mobile.menu())
