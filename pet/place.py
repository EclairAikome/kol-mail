"""Where the pet stands, kept so that it means the same place after a restart on a desk with several screens.

The Windows pet window is DPI-unaware (win.py): Windows hands it coordinates scaled to 96 DPI, and where screens are
scaled differently (a laptop at 150 % next to a monitor at 100 %) what such coordinates mean depends on the screen
the window is on at that moment. A place saved that way on the second screen meant somewhere else at the next start,
when the window starts out on the first one. So the place is kept in physical pixels, which mean the same whatever
the scaling, together with the screen she stood on: its name (\\\\.\\DISPLAY2), its rectangle and work area. The
anchor is her feet (bottom middle): on a screen scaled otherwise her size changes, her feet stay where they were.

At logon the second screen may come a little later than the pet, and the taskbar may not have claimed its strip yet:
the place is then only the nearest one possible, and win.py places her again from the saved place once the screens
change. A drag saves a new place, so she is never pulled back to an older one.

The pet writes data/pet-place.json itself (the app keeps only the 96-DPI x and y of pet.json, which older versions
read). Its "legacy" pair is the x and y saved with it, "previous" the pair saved the time before: pet.json, which
the app writes a moment later, holds one of the two (the previous one when that request got lost). When it holds
neither, an older version moved her since, and the file is ignored.

A screen's name (\\\\.\\DISPLAY1) is not who it is: after a restart or a replug two screens may swap names. So
her screen is first the one with the same rectangle and scaling, then the one of the same name, unless her feet
are now on another screen. Only pure functions and the file here, so the choice of screen is tested without
any screen."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

FILE = "pet-place.json"
VERSION = 1


@dataclass(frozen=True)
class Screen:
    device: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom in physical pixels
    work: tuple[int, int, int, int]  # the same without the taskbar
    dpi: int = 96
    primary: bool = False


def contains(r, x: int, y: int) -> bool:
    return r[0] <= x < r[2] and r[1] <= y < r[3]


def gap(r, x: int, y: int) -> int:
    """How far (x, y) is from the rectangle, 0 inside it."""
    return max(r[0] - x, 0, x - r[2] + 1) + max(r[1] - y, 0, y - r[3] + 1)


def holder(screens: list[Screen], x: int, y: int) -> Screen | None:
    return next((s for s in screens if contains(s.rect, x, y)), None)


def nearest(screens: list[Screen], x: int, y: int) -> Screen:
    return min(screens, key=lambda s: gap(s.rect, x, y))


def clamp(x: int, y: int, w: int, h: int, work) -> tuple[int, int]:
    """Inside the work area, as far as she fits."""
    return max(work[0], min(x, work[2] - w)), max(work[1], min(y, work[3] - h))


def signature(screens: list[Screen]) -> tuple:
    """Changes whenever a screen comes, goes, moves, changes its scaling or its taskbar."""
    return tuple((s.device, s.rect, s.work, s.dpi) for s in screens)


def describe(screens: list[Screen]) -> str:
    return "; ".join(f"{s.device} {','.join(map(str, s.rect))} work {','.join(map(str, s.work))} {s.dpi}dpi"
                     + (" primary" if s.primary else "") for s in screens) or "none"


def remember(rect, screens: list[Screen], legacy: tuple[int, int], previous=None) -> dict | None:
    """The place of a window at `rect` (physical pixels): her feet and the screen they stand on. previous: the
    legacy pair of the place saved before (or pet.json's x, y when there was none)."""
    if not screens:
        return None
    fx, fy = (rect[0] + rect[2]) // 2, rect[3]
    s = holder(screens, fx, fy - 1) or nearest(screens, fx, fy - 1)
    place = {"version": VERSION, "device": s.device, "screen": list(s.rect), "work": list(s.work), "dpi": s.dpi,
             "feet": [fx, fy], "legacy": [int(legacy[0]), int(legacy[1])]}
    if previous is not None and _ints(list(previous), 2):
        place["previous"] = list(previous)
    return place


def _ints(value, n: int) -> bool:
    return isinstance(value, list) and len(value) == n and all(
        isinstance(v, int) and not isinstance(v, bool) and abs(v) < 1_000_000 for v in value)


def valid(place, settings: dict) -> dict | None:
    """`place` when it is well formed and pet.json still holds the x and y saved with it, else None."""
    if not isinstance(place, dict) or place.get("version") != VERSION or not isinstance(place.get("device"), str):
        return None
    if not all(_ints(place.get(k), n) for k, n in (("screen", 4), ("work", 4), ("feet", 2), ("legacy", 2))):
        return None
    if "previous" in place and not _ints(place["previous"], 2) or "dpi" in place and not _ints([place["dpi"]], 1):
        return None
    if [settings.get("x"), settings.get("y")] not in (place["legacy"], place.get("previous")):
        return None
    return place


def target(place: dict, screens: list[Screen], size_on) -> tuple[int, int, Screen, str] | None:
    """(x, y) of her top left in physical pixels, the screen and how it was chosen; None without any screen.
    size_on(screen) is her size in physical pixels there."""
    if not screens:
        return None
    fx, fy = place["feet"]
    alike = [s for s in screens if list(s.rect) == place["screen"] and s.dpi == place.get("dpi", s.dpi)]
    exact = next((s for s in alike if s.device == place["device"]), alike[0] if alike else None)
    named = next((s for s in screens if s.device == place["device"]), None)
    there = holder(screens, fx, fy - 1)
    if exact:
        s, how = exact, "her screen" if exact.device == place["device"] else f"her screen, now named {exact.device}"
    elif named and there and there is not named:  # the name went to another screen; hers may be the one there
        s, how = there, f"the screen now at her place ({named.device} is another screen now)"
    elif named:  # same screen at another resolution or place in the arrangement: the same spot of its work area
        s, how = named, "her screen, resized or moved"
        ow, nw = place["work"], named.work
        rx = min(1.0, max(0.0, (fx - ow[0]) / max(1, ow[2] - ow[0])))
        ry = min(1.0, max(0.0, (fy - ow[1]) / max(1, ow[3] - ow[1])))
        fx, fy = nw[0] + round(rx * (nw[2] - nw[0])), nw[1] + round(ry * (nw[3] - nw[1]))
    elif there:
        s, how = there, "the screen now at her place"
    else:
        s, how = nearest(screens, fx, fy - 1), "the nearest screen (hers is not there)"
    w, h = size_on(s)
    x, y = fx - w // 2, fy - h
    if not contains(s.rect, x + w // 2, y + h // 2):  # her middle off that screen: just inside its work area
        x, y = clamp(x, y, w, h, s.work)
    return x, y, s, how


def load(folder: Path | None) -> dict | None:
    if folder is None:
        return None
    try:
        return json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(folder: Path | None, place: dict) -> None:
    """Atomic; a failure costs only the place (pet.json still has x and y)."""
    if folder is None:
        return
    path = folder / FILE
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(place) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print("pet place not saved:", repr(e), flush=True)
