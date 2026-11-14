"""Which creator a mail is about, when one address writes for several.

An agency or MCN often answers for more than one creator. We invite A and B at their own addresses, each forwards the
invitation to the same manager, and from then on the manager writes about A and about B from one address, mostly
under the subject of the invitation (a template, the same for both), not always with the creator on Cc. Filed by
sender and subject, as every other conversation is, A's mails and B's ran together into one conversation, and a draft
about A read the manager's mails about B.

So every mail is filed under the creator it is about, its "person". For most mails that is simply the other side
(app.counterpart). For a mail from an address that writes for others, it is found out in this order:

1. The owner moved the mail to a creator by hand. That is final.
2. Among the mail's sender, To and Cc there is exactly one creator we invited under its subject: someone we wrote to
   alone, with a first mail (not a reply), under that subject. That is the manager copying the creator, or our reply
   to both. When there are two and one of them is the sender, the sender.
3. It answers or quotes a mail whose person is known (In-Reply-To, then References from the nearest back); a creator
   we invited is preferred to anyone else found there. That is the manager answering our reply about A.
4. Its text names exactly one creator invited under its subject by address, other than the sender: the invitation
   the creator forwarded, quoted under the manager's first answer.
   When 3 and 4 name two different creators (the manager hit "reply" on the latest mail, about B, to write about
   A), nothing is learned: a wrong answer kept here would be inherited by every mail that answers this one.
5. Otherwise the other side, as before. What no rule can tell stays under the manager's address, and the owner can
   move it.

Rules 1 and 2 need only what every folder listing has. Rules 3 and 4 need the mail's headers and text: their answer
is learned when the mail is read (translated, opened, prefetched) and kept per mailbox in a small file (Store), so the
list and the conversation can use it without reading the mail again.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path

KEEP = 20000       # learned answers kept; the owner's own are kept first
RETRY_AFTER = 60   # seconds before a file that could not be read is read again
OWN_MAX = 4        # people on one of our mails beyond which it is a group mail, never filed under one creator by rule 2
ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ID = re.compile(r"<[^<>\s]+>")


def invited(sent: Iterable[dict], recipients_of: Callable[[dict], list[str]], subject_of: Callable[[dict], str],
            is_first: Callable[[dict], bool]) -> dict[str, set[str]]:
    """{normalised subject: the addresses we wrote a first mail to, alone, under it} from the Sent folder."""
    out: dict[str, set[str]] = {}
    for meta in sent:
        to = recipients_of(meta)
        if len(to) == 1 and is_first(meta):
            out.setdefault(subject_of(meta), set()).add(to[0])
    return out


def by_people(sender: str, people: set[str], own: bool, invited_here: set[str]) -> str:
    """Rule 2: the one invited creator on the mail, or ""."""
    if own and len(people) > OWN_MAX:
        return ""
    hits = people & invited_here
    if len(hits) == 1:
        return next(iter(hits))
    if len(hits) > 1 and not own and sender in hits:
        return sender
    return ""


def ids_nearest_first(in_reply_to: str, references: str) -> list[str]:
    """The Message-IDs a mail answers or quotes, the nearest first: In-Reply-To, then References from its end."""
    out: list[str] = []
    for mid in _ID.findall(in_reply_to or "") + list(reversed(_ID.findall(references or ""))):
        if mid not in out:
            out.append(mid)
    return out


def by_refs(refs: list[str], person_of_id: Callable[[str], str], invited_any: set[str]) -> str:
    """Rule 3: the person of the nearest mail this one answers, an invited creator before anyone else; or ""."""
    found = [p for p in (person_of_id(r) for r in refs) if p]
    return next((p for p in found if p in invited_any), found[0] if found else "")


def by_text(body: str, invited_here: set[str], sender: str, me: str) -> str:
    """Rule 4: the one invited creator whose address the text names, or ""."""
    named = {a.lower().rstrip(".") for a in ADDRESS.findall(body or "")} - {sender, me}
    hits = named & invited_here
    return next(iter(hits)) if len(hits) == 1 else ""


class Store:
    """Message-ID (lower case) -> (person, how). how is "hand" for the owner's own choice, else the rule that
    learned it ("refs", "text"). A file that cannot be read is put aside; if even that fails, nothing is written
    over it."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, list[str]] | None = None
        self.readonly = False
        self.retry_at = 0.0  # a file held by another program: read again after this (time.monotonic)

    def _load(self) -> dict[str, list[str]]:
        if self.retry_at and time.monotonic() >= self.retry_at:
            self.data, self.readonly, self.retry_at = None, False, 0.0
        if self.data is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
                self.data = {str(k): [str(v[0]), str(v[1])] for k, v in raw.items()
                             if isinstance(v, list) and len(v) == 2}
            except FileNotFoundError:
                self.data = {}
            except ValueError:
                self.data = {}
                try:
                    os.replace(self.path, self.path.with_suffix(".bad"))
                except OSError:
                    self.readonly = True
            except OSError:  # locked by a virus scanner, a bad ACL: mails go by the other rules meanwhile, the list
                self.data, self.readonly = {}, True  # never fails over it, and nothing is written over the file
                self.retry_at = time.monotonic() + RETRY_AFTER
        return self.data

    def get(self, message_id: str) -> tuple[str, str] | None:
        if not message_id:
            return None
        found = self._load().get(message_id.lower())
        return (found[0], found[1]) if found else None

    def hand(self, message_id: str) -> str:
        found = self.get(message_id)
        return found[0] if found and found[1] == "hand" else ""

    def learned(self, message_id: str) -> str:
        found = self.get(message_id)
        return found[0] if found and found[1] != "hand" else ""

    def learn(self, message_id: str, person: str, how: str) -> bool:
        """Keep what a rule found ("" forgets it). The owner's choice is never replaced. True when it changed."""
        if not message_id or self.hand(message_id):
            return False
        return self._put(message_id.lower(), [person, how] if person else None)

    def set_hand(self, message_id: str, person: str) -> bool:
        """The owner's choice ("" gives the mail back to the rules)."""
        if not message_id:
            return False
        return self._put(message_id.lower(), [person, "hand"] if person else None)

    def _put(self, key: str, value: list[str] | None) -> bool:
        data = self._load()
        if data.get(key) == value or (value is None and key not in data):
            return False
        if value is None:
            data.pop(key, None)
        else:
            data.pop(key, None)
            data[key] = value  # the newest last: what is dropped first is the oldest
        while len(data) > KEEP:
            oldest = next((k for k, v in data.items() if v[1] != "hand"), next(iter(data)))
            data.pop(oldest)
        self._write(data)
        return True

    def _write(self, data: dict):
        if self.readonly:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
