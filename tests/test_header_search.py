"""Searching by sender, recipient or subject on a server that refuses text search (Alibaba Mail): mailserver
answers it from the headers instead."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mailserver  # noqa: E402

classic = pytest.importorskip("mcp_email_server.emails.classic")

MAILS = {
    "5": b"From: Creator One <creator1@example.com>\r\nTo: Owner <owner@example.org>\r\n"
         b"Subject: Re: Genshin Impact collab with LootBar | Creator Channel\r\n\r\n",
    "7": b"From: Owner <owner@example.org>\r\nTo: creator1@example.com\r\nCc: boss@example.org\r\n"
         b"Subject: Genshin Impact collab with LootBar | Creator Channel\r\n\r\n",
    "9": b"From: =?utf-8?b?5byg5LiJ?= <zhang@example.com>\r\nTo: owner@example.org\r\n"
         b"Subject: =?utf-8?b?5ZCI5L2c?=\r\n\r\n",
}


class FakeServer:
    """An IMAP session that refuses text search the way Alibaba does and answers date/flag search and FETCH."""

    host = "imap.example.test"

    def __init__(self):
        self.searches, self.fetches = [], []

    async def uid(self, command, uids, item):
        assert command == "fetch" and "HEADER.FIELDS" in item
        self.fetches.append(uids)
        lines = []
        for uid in uids.split(","):
            lines += [f"1 FETCH (UID {uid} BODY[HEADER.FIELDS (FROM TO CC SUBJECT)] {{{len(MAILS[uid])}}}".encode(),
                      bytearray(MAILS[uid]), b")"]
        return classic.aioimaplib.Response("OK", [*lines, b"FETCH completed"])


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()

    async def original(imap, criteria):
        imap.searches.append(list(criteria))
        words = [c.upper() for c in criteria if isinstance(c, str)]
        if any(w in mailserver.HEADER_KEYS or w == "BODY" for w in words):
            return classic.aioimaplib.Response("BAD", [b"invalid command or parameters"])
        return classic.aioimaplib.Response("OK", [" ".join(MAILS).encode(), b"SEARCH completed"])

    monkeypatch.setattr(classic, "_uid_search", original)
    monkeypatch.setattr(mailserver, "text_search_refused", set())
    mailserver.patch_search(classic)
    return fake


def uids(response) -> list[str]:
    assert response.result == "OK"
    return classic._normalize_search_uids(response.lines)


def search(fake, *criteria):
    return asyncio.run(classic._uid_search(fake, list(criteria)))


def test_a_refused_sender_search_is_answered_from_the_headers(server):
    assert uids(search(server, "FROM", "creator1@example.com")) == ["5"]
    # the server was asked once with the key, then without it; the second search skips the refused attempt
    assert server.searches == [["FROM", "creator1@example.com"], ["ALL"]]
    assert uids(search(server, "TO", '"creator1@example.com"')) == ["7"]
    assert server.searches[-1] == ["ALL"] and len(server.searches) == 3


def test_matching_is_case_insensitive_and_reads_encoded_names(server):
    assert uids(search(server, "FROM", "CREATOR")) == ["5"]
    assert uids(search(server, "TO", "boss@example.org")) == []  # Cc is not To, as in IMAP
    assert uids(search(server, "TEXT", "boss@example.org")) == ["7"]  # TEXT covers Cc
    literal = classic._ImapSearchLiteral("张三".encode())
    assert uids(search(server, "FROM", literal)) == ["9"]
    assert uids(search(server, "SUBJECT", classic._ImapSearchLiteral("合作".encode()))) == ["9"]


def test_text_matches_any_header_and_keys_combine_like_imap(server):
    assert uids(search(server, "TEXT", "creator")) == ["5", "7"]
    assert uids(search(server, "FROM", "owner", "SUBJECT", "Creator")) == ["7"]
    assert uids(search(server, "SINCE", "12-Nov-2026", "FROM", "nobody@example.com")) == []
    assert server.searches[-1] == ["SINCE", "12-Nov-2026"]


def test_body_or_negated_searches_keep_the_servers_answer(server):
    assert search(server, "BODY", "Genshin").result == "BAD"
    assert search(server, "NOT", "FROM", "x@example.com").result == "BAD"
    assert search(server, "FROM", "x", "HEADER", "Content-Type", "multipart/mixed").result == "BAD"
    assert search(server, "KEYWORD", "kolmail_tag", "FROM", "x").result == "BAD"
    assert server.fetches == []


def test_a_value_that_looks_like_a_key_is_a_value(server):
    MAILS["11"] = b"From: a@example.com\r\nTo: b@example.com\r\nSubject: Do not miss this\r\n\r\n"
    try:
        assert uids(search(server, "SUBJECT", "not")) == ["11"]
        assert uids(search(server, "SINCE", "1-Nov-2026", "SUBJECT", "not")) == ["11"]
        assert server.searches[-1] == ["SINCE", "1-Nov-2026"]
    finally:
        del MAILS["11"]


def test_folded_and_raw_utf8_headers_match(server):
    MAILS["12"] = (b"From: =?utf-8?b?5byg5LiJ?= <zhang@example.com>\r\nTo: owner@example.org\r\n"
                   b"Subject: Genshin Impact collab with LootBar |\r\n Creator Channel\r\n\r\n")
    MAILS["13"] = "From: 李四 <li@example.com>\r\nTo: owner@example.org\r\nSubject: 合作愉快\r\n\r\n".encode()
    try:
        assert uids(search(server, "SUBJECT", '"LootBar | Creator"')) == ["5", "7", "12"]
        assert uids(search(server, "FROM", classic._ImapSearchLiteral("李四".encode()))) == ["13"]
        assert uids(search(server, "SUBJECT", classic._ImapSearchLiteral("合作".encode()))) == ["9", "13"]
    finally:
        del MAILS["12"], MAILS["13"]


def test_a_fetch_that_leaves_a_mail_out_raises_instead_of_hiding_it(server, monkeypatch):
    real = FakeServer.uid

    async def lossy(self, command, uids, item):
        response = await real(self, command, uids, item)
        return classic.aioimaplib.Response("OK", response.lines[3:])  # the first mail's lines are gone

    monkeypatch.setattr(FakeServer, "uid", lossy)
    with pytest.raises(classic.MetadataProviderObservationError):
        search(server, "FROM", "creator")


def test_a_server_with_text_search_is_never_marked(monkeypatch):
    """A BAD for another reason (here: a bad date) fails the fallback too: the host stays on the server's search."""
    fake = FakeServer()

    async def original(imap, criteria):
        imap.searches.append(list(criteria))
        if "SINCE" in criteria:
            return classic.aioimaplib.Response("BAD", [b"bad date"])
        return classic.aioimaplib.Response("OK", [b"5", b"SEARCH completed"])

    monkeypatch.setattr(classic, "_uid_search", original)
    monkeypatch.setattr(mailserver, "text_search_refused", set())
    mailserver.patch_search(classic)
    assert search(fake, "SINCE", "99-Foo-2026", "FROM", "creator").result == "BAD"
    assert mailserver.text_search_refused == set()
    assert uids(search(fake, "FROM", "creator")) == ["5"]
    assert fake.searches[-1] == ["FROM", "creator"] and fake.fetches == []


def test_searches_without_header_keys_are_untouched(server):
    assert uids(search(server, "UNSEEN")) == ["5", "7", "9"]
    assert server.fetches == []


def test_the_real_metadata_listing_works_through_the_fallback(server, monkeypatch):
    """get_emails_metadata end to end with the fake server: the refused FROM search now returns the mail."""
    client = classic.EmailClient.__new__(classic.EmailClient)

    async def connect():
        return server

    async def login(imap, user, password):
        return None

    async def nothing(*args, **kwargs):
        return None

    async def selected(name):
        return classic.aioimaplib.Response("OK", [b"SELECT completed"])

    async def dates(imap, ids):
        from datetime import UTC, datetime
        return {uid: datetime(2026, 11, 12, tzinfo=UTC) for uid in ids}

    async def headers(imap, ids, include_flags=False, header_budget=None):
        return {uid: {"email_id": uid, "subject": "s", "from": "f", "to": [], "date": None, "attachments": []}
                for uid in ids}

    server.select = selected
    server.logout = nothing
    secret = type("S", (), {"get_secret_value": lambda self: "x"})()
    client.email_server = type("E", (), {"user_name": "u", "password": secret})()
    monkeypatch.setattr(client, "_connect_imap", connect, raising=False)
    monkeypatch.setattr(classic, "_imap_login", login)
    monkeypatch.setattr(classic, "_send_imap_id", nothing)
    monkeypatch.setattr(client, "_batch_fetch_dates", dates, raising=False)
    monkeypatch.setattr(client, "_batch_fetch_headers", headers, raising=False)
    total, emails = asyncio.run(client.get_emails_metadata(from_address="creator1@example.com"))
    assert total == 1 and [e["email_id"] for e in emails] == ["5"]
