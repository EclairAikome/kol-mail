"""KOL replies the mail server filed as spam go back to INBOX.

Alibaba Mail, where the company mailboxes moved on 2026-11-05, put 7 KOL replies into 垃圾邮件 within a week
(a signed contract, a rate offer, an agency asking for a deal), while other replies in the very same threads reached
INBOX. KOL Mail only watches INBOX and the deal folders, so none of them was announced, translated or logged
(found 2026-11-12; all 7 named one of our mails in References). Every scan of the company mailbox now reads the
headers of the newest junk mails and moves back those that answer a mail of ours: In-Reply-To / References name a
Message-ID of Sent, or one made on our own domain (a mail whose Sent copy failed to save still carries such an ID).

The sender alone is never enough: From is easily forged, and phishing posing as a KOL we talk to is exactly what the
filter should keep. A KOL's first mail that answers nothing of ours therefore stays in junk.

Each Message-ID is moved back at most once, and a mail the owner moved into junk in KOL Mail is never moved back:
both are kept in a small file, so putting a mail into junk again sticks.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

LOOK = 30          # newest mails of the junk folder looked at per scan
CONTENT_MAX = 8    # junk mails per scan whose headers are read (one IMAP call for all of them)
KEEP = 3000        # Message-IDs remembered

_ID = re.compile(r"<[^<>\s]+>")


def ids_in(*headers: str) -> set[str]:
    """The Message-IDs named in In-Reply-To / References, compared without case."""
    return {m.lower() for h in headers for m in _ID.findall(h or "")}


def our_ids(sent: list[dict]) -> set[str]:
    return {str(m["message_id"]).lower() for m in sent if m.get("message_id")}


def answers_us(content: dict, sent_ids: set[str], domain: str) -> bool:
    named = ids_in(content.get("in_reply_to") or "", content.get("references") or "")
    if named & sent_ids:
        return True
    tail = f"@{domain.lower()}>" if domain else ""
    return bool(tail) and any(i.endswith(tail) for i in named)


class Memory:
    """Message-IDs already moved back, or put into junk by the owner: never moved back (again)."""

    def __init__(self, path: Path):
        self.path = path
        self.ids: list[str] | None = None

    def _load(self) -> list[str]:
        if self.ids is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise ValueError("not a list")
                self.ids = [str(x) for x in data]
            except FileNotFoundError:
                self.ids = []
            except ValueError:  # garbled: kept aside for a look, never overwritten by the next add
                self.ids = []
                try:
                    os.replace(self.path, self.path.with_suffix(".bad"))
                except OSError:
                    pass
            except OSError:  # held by another program for a moment: read again next time, nothing written meanwhile
                raise
        return self.ids

    def __contains__(self, message_id: str) -> bool:
        return bool(message_id) and message_id.lower() in self._load()

    def add(self, message_ids: list[str]):
        ids = self._load()
        known = set(ids)
        ids.extend(m.lower() for m in message_ids if m and m.lower() not in known)
        del ids[:-KEEP]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ids), encoding="utf-8")
        os.replace(tmp, self.path)
