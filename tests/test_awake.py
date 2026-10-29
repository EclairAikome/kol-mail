"""Awake after work (awake.py): when KOL Mail is on duty, holding the computer awake meanwhile, and noticing a sleep."""

import asyncio
import ctypes
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import awake  # noqa: E402
from autoreply import TZ, Config  # noqa: E402


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


ON = Config(email_enabled=True, offhours_mode="ai")


# ---------------------------------------------------------------- on duty

def test_on_duty_only_off_hours_with_off_hours_replies_on_for_some_channel():
    assert awake.on_duty(ON, at("2026-10-28T20:00"))  # a Wednesday evening
    assert not awake.on_duty(ON, at("2026-10-28T11:00"))  # at work
    assert awake.on_duty(ON, at("2026-10-31T11:00"))  # Saturday
    assert awake.on_duty(Config(telegram_enabled=True, offhours_mode="template"), at("2026-10-28T20:00"))
    assert not awake.on_duty(Config(email_enabled=True, offhours_mode="off"), at("2026-10-28T20:00"))
    assert not awake.on_duty(Config(offhours_mode="ai"), at("2026-10-28T20:00"))  # no channel on
    assert not awake.on_duty(None, at("2026-10-28T20:00"))  # autoreply.json broken: nothing is answered


def test_a_sleep_counts_as_missed_duty_only_when_it_covered_off_hours():
    evening = at("2026-10-28T18:00")  # the off period's start
    assert awake.missed_period(ON, at("2026-10-28T12:00"), at("2026-10-28T13:30")) is None  # lunch
    assert awake.missed_period(ON, at("2026-10-28T17:30"), at("2026-10-29T10:30")) == evening  # the night
    assert awake.missed_period(ON, at("2026-10-28T19:00"), at("2026-10-28T19:05")) == evening
    assert awake.missed_period(ON, at("2026-10-28T17:55"), at("2026-10-28T18:01")) == evening  # its last minute
    assert awake.missed_period(None, at("2026-10-28T19:00"), at("2026-10-29T09:00")) is None


def test_it_is_held_awake_from_two_hours_before_closing_time():
    """A computer left idle at 17:00 would be asleep by the time the duty starts at 18:00."""
    assert not awake.keep_awake(ON, at("2026-10-28T15:30"))
    assert awake.keep_awake(ON, at("2026-10-28T16:30")) and awake.keep_awake(ON, at("2026-10-28T23:00"))
    assert not awake.keep_awake(Config(email_enabled=True, offhours_mode="off"), at("2026-10-28T23:00"))


# ---------------------------------------------------------------- noticing a sleep

class Clocks:
    """(with sleep, without sleep): awake time moves both, sleep only the first."""

    def __init__(self):
        self.value = (1000.0, 500.0)

    def __call__(self):
        return self.value

    def awake(self, seconds):
        self.value = (self.value[0] + seconds, self.value[1] + seconds)

    def asleep(self, seconds):
        self.value = (self.value[0] + seconds, self.value[1])


def awake_for(w, c, seconds, now):
    """Looks every 20 s for `seconds` of awake time from `now`: what they said, besides (False, None)."""
    said = []
    for n in range(int(seconds // 20)):
        c.awake(20)
        result = w.look(now + timedelta(seconds=20 * (n + 1)))
        if result != (False, None):
            said.append(result)
    return said


def test_a_sleep_is_noticed_at_once_and_told_once_the_computer_stayed_awake():
    c = Clocks()
    w = awake.SleepWatch(c)
    assert awake_for(w, c, 60, at("2026-10-28T20:59")) == []
    c.asleep(11 * 3600)
    c.awake(20)
    assert w.look(at("2026-10-29T08:00")) == (True, None)  # scan at once, say nothing yet
    assert awake_for(w, c, 280, at("2026-10-29T08:00")) == []
    ((woke, told),) = awake_for(w, c, 100, at("2026-10-29T08:04:40"))
    assert not woke and told[1] == at("2026-10-29T08:00")
    assert abs((told[0] - at("2026-10-28T21:00")).total_seconds()) < 60
    assert awake_for(w, c, 600, at("2026-10-29T08:07")) == []  # told once


def test_power_nap_wakes_between_sleeps_make_one_stretch():
    c = Clocks()
    w = awake.SleepWatch(c)
    c.asleep(3600)
    c.awake(20)
    assert w.look(at("2026-10-28T22:00"))[0]
    assert awake_for(w, c, 60, at("2026-10-28T22:00")) == []  # Power Nap: a minute awake
    c.asleep(7200)
    c.awake(20)
    assert w.look(at("2026-10-29T00:01"))[0]
    ((_, told),) = awake_for(w, c, 400, at("2026-10-29T00:01"))
    assert told == (at("2026-10-28T21:00"), at("2026-10-29T00:01"))


def test_a_pause_the_second_clock_does_not_see_counts_too():
    """Windows' Modern Standby pauses programs while the clock without sleep goes on: the beat came hours late."""
    c = Clocks()
    w = awake.SleepWatch(c)
    c.awake(9 * 3600)
    woke, _ = w.look(at("2026-10-29T08:00"))
    assert woke and w.pending[0] == at("2026-10-28T23:00:20")


def test_short_stops_and_clocks_that_cannot_be_read_say_nothing():
    c = Clocks()
    w = awake.SleepWatch(c)
    c.asleep(awake.LEAST - 30)
    c.awake(20)
    assert w.look(at("2026-10-28T22:00")) == (False, None)
    c.awake(20 + awake.LEAST - 30)  # a late beat, a busy moment
    assert w.look(at("2026-10-28T22:02")) == (False, None)
    c.value = None
    assert w.look(at("2026-10-28T22:03")) == (False, None)
    c.value = (99999.0, 1.0)
    assert w.look(at("2026-10-28T22:04")) == (False, None)  # no earlier reading to compare with


def test_this_system_has_both_clocks():
    first = awake.clocks()
    if first is None:
        pytest.skip(f"no pair of clocks on {sys.platform}")
    second = awake.clocks()
    assert second[0] >= first[0] and second[1] >= first[1]
    assert abs((second[0] - first[0]) - (second[1] - first[1])) < 1  # awake: both went on alike


# ---------------------------------------------------------------- holding the computer awake

class Proc:
    def __init__(self, args, **kw):
        self.args, self.code, self.stopped = args, None, False

    def poll(self):
        return self.code

    def terminate(self):
        self.stopped, self.code = True, 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def test_on_a_mac_caffeinate_holds_it_on_mains_power_and_goes_with_kol_mail():
    started = []
    k = awake.KeepAwake("darwin", popen=lambda args, **kw: started.append(Proc(args)) or started[-1])
    assert k.hold(True) is None and k.holding
    assert started[0].args == ["/usr/bin/caffeinate", "-s", "-w", str(os.getpid())]
    assert k.hold(True) is None
    assert len(started) == 1  # still running: not a second one
    started[0].code = 1  # it went away
    assert "意外退出了（退出码 1）" in k.hold(True)  # said, and started again
    assert len(started) == 2 and k.holding and k.hold(True) is None
    k.hold(False)
    assert started[1].stopped and not k.holding and k.proc is None
    k.hold(False)
    assert len(started) == 2


def test_a_mac_without_caffeinate_says_so():
    def missing(args, **kw):
        raise FileNotFoundError(2, "No such file", args[0])
    k = awake.KeepAwake("darwin", popen=missing)
    assert "没法让电脑保持清醒" in k.hold(True) and not k.holding


class Kernel32:
    def __init__(self, mains=1, works=True):
        self.calls, self.mains, self.works = [], mains, works
        self.SetThreadExecutionState = lambda flags: self.calls.append(flags) or (1 if self.works else 0)
        self.GetSystemPowerStatus = self.power

    def power(self, ref):
        ref._obj.ACLineStatus = self.mains
        return 1


def test_on_windows_the_request_is_renewed_while_on_duty_and_cleared_after():
    """A sleep someone asks for may end the request: it is asked again at every beat."""
    k32 = Kernel32()
    k = awake.KeepAwake("win32", kernel32=k32)
    assert k.hold(True) is None and k.holding
    k.hold(True)
    assert k32.calls == [awake.ES_CONTINUOUS | awake.ES_SYSTEM_REQUIRED] * 2
    k.hold(False)
    assert k32.calls[-1] == awake.ES_CONTINUOUS and not k.holding
    k.hold(False)
    assert len(k32.calls) == 3  # nothing to clear any more


def test_on_windows_a_laptop_on_battery_is_left_to_sleep():
    k32 = Kernel32(mains=0)
    k = awake.KeepAwake("win32", kernel32=k32)
    assert k.hold(True) is None and not k.holding and k32.calls == []
    k32.mains = 255  # unknown: as a desktop PC
    k.hold(True)
    assert k.holding and k32.calls == [awake.ES_CONTINUOUS | awake.ES_SYSTEM_REQUIRED]
    k32.mains = 0  # unplugged while on duty
    k.hold(True)
    assert not k.holding and k32.calls[-1] == awake.ES_CONTINUOUS


def test_on_windows_a_refused_request_shows_on_the_page():
    k = awake.KeepAwake("win32", kernel32=Kernel32(works=False))
    assert "SetThreadExecutionState" in k.hold(True) and not k.holding


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows power request")
def test_on_windows_the_real_request_can_be_set_and_cleared():
    k = awake.KeepAwake()
    try:
        assert k.hold(True) is None
        assert k.holding == awake.on_mains(ctypes.WinDLL("kernel32"))
    finally:
        assert k.hold(False) is None and not k.holding


# ---------------------------------------------------------------- the app's side

@pytest.fixture
def phone(monkeypatch):
    told = []

    async def tell_owner(text, *args, **kw):
        told.append(text)
        return []

    monkeypatch.setattr(app, "tell_owner", tell_owner)
    monkeypatch.setitem(app.state, "slept", None)
    monkeypatch.setitem(app.slept_told, "period", None)
    return told


def sleeps(*stretches):
    async def run():
        for start, end in stretches:
            app.after_sleep(ON, at(start), at(end))
        await asyncio.sleep(0.05)  # the pushes are tasks of their own
    asyncio.run(run())


def test_a_sleep_that_cost_the_duty_is_told_on_the_page_and_the_phone(phone):
    sleeps(("2026-10-28T12:00", "2026-10-28T13:00"))  # lunch: no duty lost
    assert app.state["slept"] is None and phone == []
    sleeps(("2026-10-28T21:55", "2026-10-29T10:40"))  # woken at work
    assert app.state["slept"]["text"] == "电脑 10-28 21:55 到 10-29 10:40 睡着了，这段时间没能值守"
    (text,) = phone
    assert "10-28 21:55 到 10-29 10:40" in text and "请看一下电脑上" in text and text.endswith(awake.advice())


def test_the_phone_hears_of_it_once_per_off_period_and_only_for_a_long_sleep(phone):
    sleeps(("2026-10-28T18:28", "2026-10-28T18:30"),  # a lid closed for two minutes: the page only
           ("2026-10-28T19:00", "2026-10-28T23:00"),  # told, woken still off hours
           ("2026-10-29T01:00", "2026-10-29T03:00"))  # the same night again: the page only
    (text,) = phone
    assert "10-28 19:00 到 10-28 23:00" in text and "接着值守" in text
    assert "10-29 01:00 到 10-29 03:00" in app.state["slept"]["text"]
    sleeps(("2026-10-29T20:00", "2026-10-29T23:00"))  # the next evening is another period
    assert len(phone) == 2


def test_the_awake_loop_holds_it_scans_after_waking_and_keeps_going(monkeypatch, phone):
    class Watch:
        looks = iter([(True, None), (False, (at("2026-10-28T19:00"), at("2026-10-28T23:00"))), RuntimeError("x"),
                      (False, None)])

        def __init__(self, beat):
            assert beat == app.AWAKE_BEAT

        def look(self, now):
            item = next(self.looks)
            if isinstance(item, Exception):
                raise item
            return item

    held = []
    monkeypatch.setattr(app.awake, "SleepWatch", Watch)
    monkeypatch.setattr(app.awake, "KeepAwake", lambda: type("K", (), {
        "holding": True, "hold": lambda self, wanted: held.append(wanted)})())
    monkeypatch.setattr(app.awake, "keep_awake", lambda cfg, now: True)
    monkeypatch.setattr(app.awake, "on_duty", lambda cfg, now: True)
    monkeypatch.setattr(app, "load_autoreply_config", lambda: ON)
    beats = []

    async def sleep(seconds):
        beats.append(seconds)
        if len(beats) > 4:
            raise asyncio.CancelledError

    async def run():
        app.scan_now.clear()
        real = asyncio.sleep
        asyncio.sleep = sleep
        try:
            with pytest.raises(asyncio.CancelledError):
                await app.awake_loop()
        finally:
            asyncio.sleep = real  # back, for the push task
        await asyncio.sleep(0.05)

    monkeypatch.setitem(app.state, "kept_awake", None)
    asyncio.run(run())
    assert beats == [app.AWAKE_BEAT] * 5 and held == [True, True, True]  # a failing look did not stop it
    assert app.scan_now.is_set() and len(phone) == 1 and app.state["kept_awake"] == awake.duty_note()


def test_the_status_reports_the_duty_and_the_sleep():
    assert {"awake_error", "kept_awake", "slept"} <= set(app.state)
    assert app.ERROR_FIELDS["awake_error"] == "下班值守"  # a failure to hold it reaches the maintainer too


def test_only_the_real_service_holds_the_computer_awake():
    """A dev copy beside the real service, and the login page before any mailbox, must not keep anyone awake."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.lifespan)))
    guarded = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.If) and ast.unparse(n.test) == "not DEV and (not LOGGED_OUT)")
    assert "awake_loop()" in ast.unparse(guarded) and ast.unparse(tree).count("awake_loop()") == 1
