"""First use: each colleague writes their own signature and describes, in their own words, how they negotiate.

There is no shared team playbook: everyone bargains a little differently. The description is kept as one section,
"## 我的谈判方式", at the top of the colleague's own playbook file (copied from the template at install), so the
strategies and drafts follow it and the template's sections below stay as a frame. Pure text helpers only; app.py
reads and writes the files and remembers in settings.json that the setup was saved."""

from __future__ import annotations

import re

PREFS_HEADING = "## 我的谈判方式"
MAX_SIGNATURE = 500
MAX_PREFERENCES = 4000
# The heading on a line of its own (trailing spaces from hand editing allowed); "## 我的谈判方式二" is another section.
_PREFS = re.compile(r"^## 我的谈判方式[ \t]*$", re.MULTILINE)
_NEXT = re.compile(r"^#{1,2} ", re.MULTILINE)  # the section ends at the next "#" or "##" heading
_SECTION = re.compile(r"^## ", re.MULTILINE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _normalize(playbook: str) -> str:
    return (playbook or "").lstrip("﻿").replace("\r\n", "\n")


def _sections(playbook: str) -> list[tuple[int, int]]:
    """Start and end of every preferences section (heading included); a hand-edited file may hold more than one."""
    found = []
    for m in _PREFS.finditer(playbook):
        following = _NEXT.search(playbook, m.end())
        found.append((m.start(), following.start() if following else len(playbook)))
    return found


def get_preferences(playbook: str) -> str:
    playbook = _normalize(playbook)
    return "\n\n".join(b for b in (playbook[s:e][len(PREFS_HEADING):].strip() for s, e in _sections(playbook)) if b)


def clean_preferences(text: str) -> str:
    """The colleague's text, kept inside its own section: their own "#"/"##" headings become "###"."""
    text = _CONTROL.sub("", (text or "").replace("\r\n", "\n")).strip()[:MAX_PREFERENCES]
    return re.sub(r"^#{1,2}(?=\s)", "###", text, flags=re.MULTILINE)


def set_preferences(playbook: str, preferences: str) -> str:
    """The playbook with its preferences section(s) replaced by one, placed before the first section, or removed when
    the preferences are empty."""
    playbook = _normalize(playbook)
    for start, end in reversed(_sections(playbook)):
        was_last = end == len(playbook)
        playbook = playbook[:start] + playbook[end:]
        if was_last:
            playbook = playbook.rstrip("\n") + "\n"
    prefs = clean_preferences(preferences)
    if not prefs:
        return playbook
    section = f"{PREFS_HEADING}\n\n{prefs}\n\n"
    first = _SECTION.search(playbook)
    if first:
        return playbook[:first.start()] + section + playbook[first.start():]
    return playbook.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n"


def clean_signature(text: str) -> str:
    lines = [line.strip() for line in _CONTROL.sub("", (text or "").replace("\r\n", "\n")).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
