"""The time a mail reached our server, for mails whose Date header is wrong.

A sender whose clock or time zone is set wrong writes a Date header hours off (seen 2026-10-27: a header 7 hours
after the mail had arrived), and the page showed that time. mcp-email-server returns only the Date header, but it
orders and filters its lists by the server's receipt time (IMAP INTERNALDATE). That is enough to find the receipt
time without raw IMAP access:
- a list page comes newest receipt first, so no mail arrived after the mail above it or after the list was fetched:
  a Date header later than that is suspect;
- since/before searches for the sender's mails narrow a suspect's receipt time down to a minute.
A false suspect (the mail above it has a Date header that is too early) only costs the searches: shown() keeps the
Date header unless it is later than the receipt time found."""

from __future__ import annotations

import email.utils
from datetime import datetime, timedelta, timezone

TOLERANCE = timedelta(minutes=10)  # ordinary clock drift and delivery delays stay as the sender wrote them
STEP = timedelta(minutes=1)        # the page shows hours and minutes
NEAR = timedelta(days=1)           # how far back of its bound a suspect's arrival is looked for first
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PAGE = 100                         # the server's largest page


def parse(value) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def stamp(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat()


def shown(header, received):
    """The date to show: the Date header, or the receipt time when the header is later than that."""
    h, r = parse(header), parse(received)
    return stamp(r) if h and r and h > r + TOLERANCE else header


def suspects(emails: list[dict], fetched_at: datetime, known: dict) -> list[tuple[dict, datetime]]:
    """(mail, latest time it can have arrived) for the mails of one list page, newest receipt first, whose Date
    header lies after that time. known maps Message-IDs to receipt times already looked up.
    Only the mail right above counts: a header that is too early must not make every mail below it a suspect."""
    above, out = None, []
    for e in emails:
        mid, header = e.get("message_id"), parse(e.get("date"))
        exact = parse(known.get(mid)) if mid else None
        bound = min(fetched_at, above) if above else fetched_at
        if exact is None and header and mid and header > bound + TOLERANCE:
            out.append((e, bound))
        above = exact or header or above
    return out


async def find(call, mailbox: str, email_id: str, sender: str | None, bound: datetime,
               fetched_at: datetime) -> datetime | None:
    """When the mail arrived, to the minute (rounded down), or None when it is not in the folder any more.
    call is a MailBridge.call; every search is limited to the sender's mails. The day before bound is searched
    first; bound comes from another sender's Date header, so when the mail is not there, everything up to the
    fetch is."""
    address = email.utils.parseaddr(sender or "")[1]

    async def arrived(since: datetime, before: datetime) -> bool:
        page = 1
        while True:
            args = {"mailbox": mailbox, "since": stamp(since), "before": stamp(before), "page": page,
                    "page_size": PAGE}
            if address:
                args["from_address"] = address
            data = await call("list_emails_metadata", **args)
            if any(str(e.get("email_id")) == email_id for e in data.get("emails", [])):
                return True
            if page * PAGE >= (data.get("total") or 0):
                return False
            page += 1

    near = bound + TOLERANCE  # the mail above may have a Date header a little early
    for lo, hi in ((near - NEAR, near), (EPOCH, fetched_at + TOLERANCE)):
        if await arrived(lo, hi):
            break
    else:
        return None
    while hi - lo > STEP:
        mid = lo + (hi - lo) / 2
        if await arrived(mid, hi):
            lo = mid
        else:
            hi = mid
    return lo.replace(microsecond=0)
