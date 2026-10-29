"""After work KOL Mail answers KOLs and pushes their messages to the phone (off-hours replies, 手机审稿): the computer
has to be awake for that. A sleeping computer runs nothing at all, so this is the next best thing:

- While KOL Mail is on duty (off hours, with off-hours replies on for some channel), and from LEAD before it, so that a
  computer left idle in the late afternoon is not asleep by closing time, KeepAwake holds the system awake as long as
  it runs on mains power (a laptop on battery is left to sleep, or it would be flat by the morning). The screen still
  turns off and locks as usual.
  On a Mac (caffeinate -s) this holds even against a sleep someone asks for, the Apple menu or closing the lid: the
  Mac goes on in the background with its screen off (a "dark wake", as powerd keeps a PreventSystemSleep assertion
  on mains power) until it is unplugged, and then sleeps. On Windows only sleeping by itself is prevented: a sleep
  someone asks for (the Start menu, the power button, the lid) still happens.
- SleepWatch notices afterwards that KOL Mail could not run for a while: from the clock that goes on during sleep,
  against the beat it is looked at by, and against a second clock that stops during sleep. Windows' Modern Standby
  pauses programs while that second clock goes on, so the first one counts too. A wall-clock jump (a time zone, a
  corrected clock) changes neither. A Mac's Power Nap wakes it for a minute now and then: stops with such short wakes
  between them make one stretch, told only once the computer has stayed awake for a while.

app.py (awake_loop) checks for mail at once after a stop, and tells the owner on the page when one kept KOL Mail from
its duty, and on the phone once per off period, for a stop of TELL_LEAST or more. No network here, and nothing that
needs a screen."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

SETTLE = 300.0     # awake this long (seconds, not counting sleep) before a sleep is told: Power Nap wakes are shorter
LEAST = 120.0      # shorter stops are not worth a word
TELL_LEAST = 1800.0  # nor worth a message on the phone below this
LEAD = timedelta(hours=2)  # held awake from this long before closing time
DUTY_STEP = timedelta(minutes=10)  # how finely a sleep is checked for off hours it covered

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

# What the owner reads: on the status bar while on duty, and after a sleep that cost the duty, what to do instead.
DUTY_NOTE = {"darwin": "下班值守中（插着电时电脑不会睡，点睡眠或合盖也会在后台接着值守）",
             "win32": "下班值守中（插着电时电脑不会自己睡）"}
ADVICE = {"darwin": "下班让电脑插着电就好：插着电时点“睡眠”或合上盖子，KOL Mail 也会在后台接着值守；没插电才会真的睡着。",
          "win32": "下班锁屏就好，别点“睡眠”、别合盖，插着电：KOL Mail 会让电脑熄屏后接着值守。"}


def duty_note(platform: str = sys.platform) -> str:
    return DUTY_NOTE.get(platform, "下班值守中")


def advice(platform: str = sys.platform) -> str:
    return ADVICE.get(platform, "下班时让电脑开着、插着电。")


# ---------------------------------------------------------------- on duty

def on_duty(cfg, moment: datetime) -> bool:
    """Whether KOL Mail has work to do after hours at `moment`: off hours, off-hours replies on, some channel on.
    cfg: autoreply.Config, or None when autoreply.json is broken (then nothing is answered)."""
    return bool(cfg and cfg.offhours_mode != "off" and (cfg.email_enabled or cfg.telegram_enabled)
                and cfg.schedule.off_period_start(moment) is not None)


def keep_awake(cfg, moment: datetime) -> bool:
    """On duty now or within LEAD."""
    return on_duty(cfg, moment) or on_duty(cfg, moment + LEAD)


def missed_period(cfg, start: datetime, end: datetime) -> datetime | None:
    """The off period (its start, as Schedule.off_period_start says) of the first time between start and end when
    KOL Mail was due on duty, looked at every DUTY_STEP; None when it was not due at all."""
    moment = start
    while True:
        if on_duty(cfg, moment):
            return cfg.schedule.off_period_start(moment)
        if moment >= end:
            return None
        moment = min(moment + DUTY_STEP, end)


# ---------------------------------------------------------------- noticing a sleep

_kernel32 = None


def clocks() -> tuple[float, float] | None:
    """(seconds that go on during sleep, seconds that stop during it), both from some fixed point; None where the
    system offers no such pair or it cannot be read. Only differences of the two matter."""
    try:
        return _clocks()
    except (OSError, AttributeError, ValueError):
        return None


def _clocks() -> tuple[float, float] | None:
    if sys.platform == "darwin":  # the same raw ticks, the second without sleep (man clock_gettime)
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW), time.clock_gettime(time.CLOCK_UPTIME_RAW)
    if sys.platform == "win32":
        global _kernel32
        if _kernel32 is None:
            _kernel32 = ctypes.WinDLL("kernel32")
            _kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            _kernel32.QueryUnbiasedInterruptTime.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
        unbiased = ctypes.c_ulonglong()  # 100 ns steps, without sleep and hibernation
        if not _kernel32.QueryUnbiasedInterruptTime(ctypes.byref(unbiased)):
            return None
        return _kernel32.GetTickCount64() / 1000, unbiased.value / 1e7
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME), time.monotonic()
    return None


class SleepWatch:
    """Looked at every `beat` seconds (look): says when the computer has just woken, and hands over each finished
    stretch of sleep once it has stayed awake SETTLE seconds. Stops with shorter wakes between them make one stretch.

    A stop is the longer of: how much more the first clock went on than the second (sleep, hibernation), and how much
    more the first clock went on than the beat (a pause that the second clock does not see, as Modern Standby's).
    A loop blocked for minutes would count too; KOL Mail's loop is not blocked that long."""

    def __init__(self, read=clocks, beat: float = 20.0, settle: float = SETTLE, least: float = LEAST):
        self.read, self.beat, self.settle, self.least = read, beat, settle, least
        self.last = read()
        self.pending: tuple[datetime, datetime] | None = None  # the stretch not told yet: fell asleep, woke last
        self.awake_since = 0.0  # the clock without sleep when the computer last woke

    def look(self, now: datetime) -> tuple[bool, tuple[datetime, datetime] | None]:
        """(it has just woken, a stretch of sleep to tell about now)."""
        read = self.read()
        last, self.last = self.last, read
        if read is None or last is None:
            return False, None
        went = read[0] - last[0]
        stopped = max(went - (read[1] - last[1]), went - self.beat)
        if stopped >= self.least:
            start = self.pending[0] if self.pending else now - timedelta(seconds=stopped)
            self.pending, self.awake_since = (start, now), read[1]
            return True, None
        if self.pending and read[1] - self.awake_since >= self.settle:
            told, self.pending = self.pending, None
            return False, told
        return False, None


# ---------------------------------------------------------------- not falling asleep

class KeepAwake:
    """The system's "stay awake" request, held while hold(True) says so.

    macOS: /usr/bin/caffeinate -s -w <this process>: valid on mains power only (see the top for what that holds
    against), and gone with this process however it ends. Windows: SetThreadExecutionState(ES_CONTINUOUS |
    ES_SYSTEM_REQUIRED) while on mains power, asked again at every hold(): a sleep someone asks for may end it. It
    belongs to the calling thread, so hold() is always called from the same one (the event loop's). Elsewhere:
    nothing."""

    def __init__(self, platform: str = sys.platform, popen=subprocess.Popen, kernel32=None):
        self.platform, self.popen = platform, popen
        self.kernel32 = kernel32
        self.proc = None       # macOS: the caffeinate process
        self.held = False      # Windows: the request is set
        self.holding = False   # either way: the computer will not sleep by itself now

    def hold(self, wanted: bool) -> str | None:
        """Hold or release; what went wrong, for the page, else None."""
        try:
            if self.platform == "darwin":
                return self._mac(wanted)
            if self.platform == "win32":
                self._windows(wanted)
        except OSError as e:
            self.holding = False
            return f"下班值守没法让电脑保持清醒：{e}"
        return None

    def _mac(self, wanted: bool) -> str | None:
        ended = self.proc.poll() if self.proc is not None else None
        problem = None
        if wanted and (self.proc is None or ended is not None):
            if ended is not None:  # it should run as long as this process: something stopped it
                problem = f"下班值守的 caffeinate 意外退出了（退出码 {ended}），已重新启动"
            self.proc = self.popen(["/usr/bin/caffeinate", "-s", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif not wanted and self.proc is not None:
            if ended is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
        self.holding = wanted
        return problem

    def _windows(self, wanted: bool):
        k = self.kernel32 or ctypes.WinDLL("kernel32")
        self.kernel32 = k
        want = wanted and on_mains(k)
        if want or self.held:
            k.SetThreadExecutionState.restype = ctypes.c_uint32
            k.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
            if not k.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0)):
                raise OSError("SetThreadExecutionState failed")
            self.held = want
        self.holding = want


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def on_mains(kernel32) -> bool:
    """Windows: on mains power, or no battery to tell (a desktop PC says 1, an unknown state 255; 0 is battery)."""
    status = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return True
    return status.ACLineStatus != 0
