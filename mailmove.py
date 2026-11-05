"""The company mailbox moved from Amazon WorkMail to Alibaba Mail (US site) on 2026-11-04.

Every install had WorkMail's servers: in mcp-email-server for each mailbox on the company domain, and in
settings.json mail.company_servers for the login screen. At start, app.py points both at Alibaba Mail. The company
kept the passwords, so the stored one is tried there as it is; one Alibaba Mail refuses is asked for in the page like
any other refused password (app.check_password).

The UIDs of the new server mean other messages than the old ones did: a mailbox that moves loses its folder indexes.
Its Message-IDs stay, so what was already announced or logged in KOL总表 is still known. Mails brought over from the
old mailbox later (Alibaba Mail's 邮箱搬家) may come in as new, and an old mailbox has years of mail no notice ever
saw: for a while after the cutover, a mail whose Date is clearly before it is remembered quietly instead. The
cutover is the company's (the MX record went to Alibaba Mail; its first mail came at 14:08 Singapore time, while
WorkMail still got a few until 18:33), not the day a computer got this version: a colleague who updates a week late
must still hear about the replies that came in meanwhile. (A first try of this move on 2026-11-02 was taken back:
then only the passwords had been reset, and the MX record still pointed at WorkMail.)"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

NEW_SERVERS = {"imap_host": "imap.us.alibabacloud.com", "imap_port": 993, "imap_security": "ssl",
               "smtp_host": "smtp.us.alibabacloud.com", "smtp_port": 465, "smtp_security": "ssl"}
CUTOVER = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
QUIET_UNTIL = CUTOVER + timedelta(days=30)  # after that, mail moved over late is ordinary new mail again
# Mails dated shortly before the cutover may never have been seen (the weekend before it, a computer that was off):
# those still count as new.
QUIET_BEFORE = CUTOVER - timedelta(days=4)


def is_old_host(host) -> bool:
    """Any Amazon WorkMail server (the company's was us-east-1)."""
    return str(host or "").strip().lower().endswith(".awsapps.com")


def on_old_servers(info: dict) -> bool:
    """An mcp-email-server account (account show) still on WorkMail."""
    return is_old_host((info.get("incoming") or {}).get("host")) or is_old_host((info.get("outgoing") or {}).get("host"))


def moved_preset(servers) -> dict | None:
    """mail.company_servers with Alibaba Mail in place of WorkMail, or None when it needs no change."""
    if not isinstance(servers, dict) or not (is_old_host(servers.get("imap_host")) or is_old_host(servers.get("smtp_host"))):
        return None
    return {**servers, **NEW_SERVERS}


# 邮箱搬家 puts the whole old mailbox into a folder named after the address ("me@example.com/INBOX",
# ".../Sent Items", ".../纯固薪"). KOL Mail and the triage task find mail by the top-level names, so the mails are
# brought back up: WorkMail's own folders into the matching ones of Alibaba Mail, every other folder into one of the
# same name at the top. Outbox and Drafts stay: WorkMail keeps nothing in the one, and Alibaba Mail refuses mails
# moved into its Draft folder (seen 2026-11-02), where old drafts are of no use anyway.
OLD_ROLES = {"inbox": "inbox", "sent items": "sent", "deleted items": "trash", "trash": "trash", "junk e-mail": "junk"}
LEFT_BEHIND = {"outbox", "drafts"}


def moved_tree(names: list[str], address: str, delim: str) -> str | None:
    """The folder 邮箱搬家 put the old mailbox into, when there is one."""
    if not delim:
        return None
    return next((n for n in names if n.casefold() == address.casefold()), None)


# Which folders go first, as a look moves at most FOLD_MAX mails: the deal folders and the owner's (small, and what
# the triage and the conversations need to know a KOL already worked with), then Sent, then INBOX, junk and trash
# last. A big old INBOX must not keep the rest away for days.
FOLD_ORDER = {None: 0, "sent": 1, "inbox": 2, "junk": 3, "trash": 3}


def fold_plan(names: list[str], address: str, delim: str, roles: dict) -> list[tuple[str, str]]:
    """(old folder, folder its mails go to) for every folder under the moved tree, in FOLD_ORDER and parents first
    within it. A WorkMail folder whose Alibaba Mail counterpart is unknown is left where it is."""
    root = moved_tree(names, address, delim)
    if not root:
        return []
    plan = []
    for name in (n for n in names if n.startswith(root + delim)):
        first, _, rest = name[len(root) + len(delim):].partition(delim)
        role = OLD_ROLES.get(first.casefold())
        if first.casefold() in LEFT_BEHIND:
            continue
        top = "INBOX" if role == "inbox" else roles.get(role) if role else first
        if not top:
            continue
        plan.append((FOLD_ORDER[role], name.count(delim), name, top + delim + rest if rest else top))
    return [(old, new) for *_, old, new in sorted(plan, key=lambda p: (p[0], p[1]))]


def quiet_old_mail(sent: datetime | None, now: datetime) -> bool:
    """True for a mail of a moved mailbox that was in the old one long before the cutover (its Date), while the
    cutover is recent."""
    if sent is None or sent.tzinfo is None or now > QUIET_UNTIL:
        return False
    return sent < QUIET_BEFORE
