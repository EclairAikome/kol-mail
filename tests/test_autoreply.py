"""Behaviour of the auto-reply rules: when the owner is off work, which messages get a reply, and only once."""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autoreply  # noqa: E402
from autoreply import TZ, Schedule  # noqa: E402


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def test_weekday_office_hours_are_work_time():
    s = Schedule()
    assert s.is_work_time(at("2026-10-15T10:00"))      # Thursday, opening minute
    assert s.is_work_time(at("2026-10-15T17:59"))
    assert not s.is_work_time(at("2026-10-15T18:00"))  # closing minute is already off
    assert not s.is_work_time(at("2026-10-15T09:59"))
    assert not s.is_work_time(at("2026-10-17T12:00"))  # Saturday


def test_holiday_is_off_all_day():
    s = Schedule(holidays=frozenset({date(2026, 10, 19)}))
    assert not s.is_work_time(at("2026-10-19T11:00"))  # Monday holiday
    assert s.is_work_time(at("2026-10-20T11:00"))


def test_off_period_runs_from_last_closing_time():
    s = Schedule()
    friday_close = at("2026-10-16T18:00")
    assert s.off_period_start(at("2026-10-16T20:30")) == friday_close
    assert s.off_period_start(at("2026-10-18T03:00")) == friday_close   # whole weekend is one period
    assert s.off_period_start(at("2026-10-19T09:59")) == friday_close
    assert s.off_period_start(at("2026-10-19T08:00")) == friday_close
    assert s.off_period_start(at("2026-10-19T10:30")) is None            # working: no off period


def test_off_period_spans_holidays_and_early_morning():
    s = Schedule(holidays=frozenset({date(2026, 10, 19)}))
    assert s.off_period_start(at("2026-10-20T07:00")) == at("2026-10-16T18:00")
    # Thursday 09:00 belongs to the period that started Wednesday evening.
    assert Schedule().off_period_start(at("2026-10-15T09:00")) == at("2026-10-14T18:00")


def test_real_kol_email_gets_a_reply():
    assert autoreply.email_skip_reason("Ana Pérez <ana.gamer@gmail.com>", "Re: Collaboration with Example") is None


def test_machine_and_own_mail_is_never_answered():
    skip = autoreply.email_skip_reason
    assert skip("Me <me@example.com>", "Re: hi")
    assert skip("MAILER-DAEMON@amazonses.com", "Undelivered Mail Returned to Sender")
    assert skip("YouTube <no-reply@youtube.com>", "New comment")
    assert skip("noreply@notifications.example.com", "Weekly digest")
    assert skip("", "Re: hi")


def test_other_auto_replies_are_not_answered():
    skip = autoreply.email_skip_reason
    for subject in ("Automatic reply: Collaboration", "Out of Office: back Monday", "Auto-Reply: hello",
                    "Respuesta automática: Colaboración", "Réponse automatique : Partenariat",
                    "Risposta automatica: Collaborazione", "Resposta automática: Parceria",
                    "Abwesenheitsnotiz: Kooperation", "Автоматический ответ: Сотрудничество", "自动回复：合作"):
        assert skip("someone@gmail.com", subject), subject


def test_each_person_gets_one_reply_per_off_period(tmp_path):
    log = autoreply.ReplyLog(tmp_path / "replies.sqlite3")
    friday = at("2026-10-16T18:00")
    assert log.claim("email", "ana@gmail.com", friday, ref="<m1>")
    assert not log.claim("email", "ana@gmail.com", friday, ref="<m2>")       # second email same night
    assert log.claim("telegram", "ana@gmail.com", friday, ref="42")          # other channel is separate
    assert log.claim("email", "ana@gmail.com", at("2026-10-19T18:00"), ref="<m3>")  # next evening


def test_reply_log_answers_whether_someone_was_handled(tmp_path):
    log = autoreply.ReplyLog(tmp_path / "replies.sqlite3")
    friday = at("2026-10-16T18:00")
    assert not log.handled("email", "Ana@Gmail.com", friday)
    log.claim("email", "ana@gmail.com", friday, ref="<m1>")
    assert log.handled("email", "Ana@Gmail.com", friday)


def test_reply_log_survives_restart_and_records_outcome(tmp_path):
    path = tmp_path / "replies.sqlite3"
    friday = at("2026-10-16T18:00")
    autoreply.ReplyLog(path).claim("email", "ana@gmail.com", friday, ref="<m1>")
    log = autoreply.ReplyLog(path)
    assert not log.claim("email", "ana@gmail.com", friday, ref="<m2>")
    log.settle("email", "ana@gmail.com", friday, status="sent", detail="es")
    [row] = log.recent()
    assert (row["channel"], row["peer"], row["status"], row["detail"]) == ("email", "ana@gmail.com", "sent", "es")


def test_more_machine_senders_and_colleagues_are_skipped():
    skip = autoreply.email_skip_reason
    assert skip("no_reply@shop.example.com", "Your order")
    assert skip("do_not_reply@bank.example.com", "Statement")
    assert skip("Colleague <alice@example.com>", "Re: KOL list")
    assert skip("ana.gamer@gmail.com", "Re: Automatic reply: Collaboration")
    assert skip("ana.gamer@gmail.com", "AW: Abwesenheitsnotiz")
    assert skip("ana.gamer@gmail.com", "Re: Collaboration") is None


def test_utc_timestamps_are_judged_in_gmt8():
    s = Schedule()
    utc = timezone.utc
    assert s.is_work_time(datetime(2026, 10, 15, 2, 0, tzinfo=utc))       # 10:00 GMT+8
    assert not s.is_work_time(datetime(2026, 10, 15, 10, 0, tzinfo=utc))  # 18:00 GMT+8
    # Mail stamped exactly at closing time belongs to the new off period.
    assert s.reply_period(datetime(2026, 10, 16, 10, 0, tzinfo=utc), now=at("2026-10-16T19:00")) == at("2026-10-16T18:00")


def test_replies_per_period_are_counted_per_channel(tmp_path):
    log = autoreply.ReplyLog(tmp_path / "replies.sqlite3")
    friday = at("2026-10-16T18:00")
    for i in range(3):
        log.claim("email", f"kol{i}@gmail.com", friday, ref=str(i))
    log.claim("telegram", "42", friday, ref="t")
    assert log.count("email", friday) == 3
    assert log.count("telegram", friday) == 1
    assert log.count("email", at("2026-10-19T18:00")) == 0


def test_claim_can_be_released_when_nothing_was_sent(tmp_path):
    log = autoreply.ReplyLog(tmp_path / "replies.sqlite3")
    friday = at("2026-10-16T18:00")
    log.claim("email", "ana@gmail.com", friday, ref="<m1>")
    log.release("email", "ana@gmail.com", friday)
    assert log.claim("email", "ana@gmail.com", friday, ref="<m1>")      # next scan may try again
    log.settle("email", "ana@gmail.com", friday, status="sent")
    log.release("email", "ana@gmail.com", friday)                       # a settled reply is never released
    assert not log.claim("email", "ana@gmail.com", friday, ref="<m2>")


def test_claims_left_by_a_crash_are_found(tmp_path):
    log = autoreply.ReplyLog(tmp_path / "replies.sqlite3")
    friday = at("2026-10-16T18:00")
    log.claim("email", "ana@gmail.com", friday, ref="<m1>")
    log.claim("telegram", "42", friday, ref="7")
    log.settle("telegram", "42", friday, status="sent")
    assert [r["peer"] for r in log.unfinished()] == ["ana@gmail.com"]


def test_no_second_reply_within_twelve_hours_even_if_periods_shift(tmp_path):
    log = autoreply.ReplyLog(tmp_path / "replies.sqlite3")
    log.claim("email", "ana@gmail.com", at("2026-10-16T18:00"), ref="<m1>")
    assert log.replied_recently("email", "ANA@gmail.com", hours=12)
    assert not log.replied_recently("email", "bob@gmail.com", hours=12)
    assert not log.replied_recently("telegram", "ana@gmail.com", hours=12)


def test_malformed_config_is_rejected_not_half_applied(tmp_path):
    import pytest
    for bad in ({"templates": "oops"}, {"holidays": [20261019]}, {"templates": {"email": {"en": 3}}}):
        with pytest.raises(ValueError):
            autoreply.load_config(write_config(tmp_path, **bad))


def write_config(tmp_path, **overrides):
    import json
    cfg = {"email_enabled": True, "telegram_enabled": False, "holidays": ["2026-10-19"],
           "templates": {"email": {"en": "Hi there, off now.", "es": "Hola, fuera de horario."},
                         "telegram": {"en": "Off now."}}}
    cfg.update(overrides)
    path = tmp_path / "autoreply.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_config_sets_switches_and_holidays(tmp_path):
    cfg = autoreply.load_config(write_config(tmp_path))
    assert cfg.email_enabled and not cfg.telegram_enabled
    assert not cfg.schedule.is_work_time(at("2026-10-19T11:00"))


def test_reply_text_follows_sender_language_with_english_fallback(tmp_path):
    cfg = autoreply.load_config(write_config(tmp_path))
    assert cfg.reply_text("email", "es") == "Hola, fuera de horario."
    assert cfg.reply_text("email", "ES-mx") == "Hola, fuera de horario."
    assert cfg.reply_text("email", "ja") == "Hi there, off now."
    assert cfg.reply_text("telegram", "es") == "Off now."


def test_off_hours_reply_only_for_messages_that_arrived_off_hours():
    s = Schedule()
    friday = at("2026-10-16T18:00")
    assert s.reply_period(at("2026-10-16T19:00"), now=at("2026-10-16T19:03")) == friday
    assert s.reply_period(at("2026-10-18T09:00"), now=at("2026-10-19T09:30")) == friday   # weekend mail, Monday dawn
    assert s.reply_period(at("2026-10-16T17:50"), now=at("2026-10-16T18:05")) is None    # came in during work
    assert s.reply_period(at("2026-10-15T20:00"), now=at("2026-10-16T19:00")) is None    # last night's, already past
    assert s.reply_period(at("2026-10-16T19:00"), now=at("2026-10-19T10:01")) is None    # the owner is back at work


def test_switches_can_be_flipped_without_touching_the_rest(tmp_path):
    import json
    import pytest
    path = write_config(tmp_path)
    assert not autoreply.load_config(path).telegram_enabled
    autoreply.set_switch(path, "telegram_enabled", True)
    cfg = autoreply.load_config(path)
    assert cfg.telegram_enabled and cfg.email_enabled
    assert json.loads(path.read_text(encoding="utf-8"))["holidays"] == ["2026-10-19"]
    with pytest.raises(ValueError):
        autoreply.set_switch(path, "templates", True)


def test_missing_config_keeps_everything_off(tmp_path):
    cfg = autoreply.load_config(tmp_path / "nope.json")
    assert not cfg.email_enabled and not cfg.telegram_enabled


def test_mechanical_off_hours_reply_is_off_unless_chosen(tmp_path):
    assert autoreply.load_config(write_config(tmp_path)).offhours_mode == "off"
    assert autoreply.load_config(tmp_path / "nope.json").offhours_mode == "off"
    example = Path(__file__).resolve().parents[1] / "autoreply.example.json"
    assert autoreply.load_config(example).offhours_mode == "off"


def test_off_hours_mode_accepts_off_template_and_ai_only(tmp_path):
    import pytest
    for mode in ("off", "template", "ai"):
        assert autoreply.load_config(write_config(tmp_path, offhours_mode=mode)).offhours_mode == mode
    with pytest.raises(ValueError):
        autoreply.load_config(write_config(tmp_path, offhours_mode="auto"))


def test_template_reply_can_be_switched_on_and_off_again(tmp_path):
    import pytest
    path = write_config(tmp_path)
    autoreply.set_switch(path, "offhours_mode", "template")
    assert autoreply.load_config(path).offhours_mode == "template"
    autoreply.set_switch(path, "offhours_mode", "off")
    cfg = autoreply.load_config(path)
    assert cfg.offhours_mode == "off" and cfg.email_enabled  # the channel switches stay as they were
    with pytest.raises(ValueError):
        autoreply.set_switch(path, "offhours_mode", "auto")
