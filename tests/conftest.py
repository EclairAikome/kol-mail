"""Tests run against a fixed example identity, never the settings of the machine they run on."""

import json
import os
import tempfile
from pathlib import Path

_settings = Path(tempfile.mkdtemp(prefix="kol-mail-test-")) / "settings.json"
_settings.write_text(json.dumps({
    "mail": {"account_name": "me", "address": "me@example.com", "company_domain": "@example.com"},
    "identity": {"name": "Me", "company": "Example", "signature": "Me\nMarketing Manager | Example\nhttps://example.com/"},
}), encoding="utf-8")
os.environ["KOL_MAIL_SETTINGS"] = str(_settings)
os.environ["KOL_MAIL_DATA"] = str(_settings.parent / "data")  # app.py writes its files here, never in the checkout
# attachdir.py downloads into %LOCALAPPDATA%\kol-mail\attach-tmp: a test never touches the real one
os.environ["LOCALAPPDATA"] = str(_settings.parent / "localappdata")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_lookups_from_tests(monkeypatch):
    """Finding a mailbox's servers (mailfind.py) asks DNS, the web and the servers themselves. Never from a test:
    here nothing is found, and a test that needs an answer sets its own."""
    import mailfind

    async def none(*args, **kwargs):
        return []

    async def no_file(*args, **kwargs):
        return None

    async def no_answer(*args, **kwargs):
        return False
    monkeypatch.setattr(mailfind, "nslookup_mx", none)
    monkeypatch.setattr(mailfind, "doh_mx", none)
    monkeypatch.setattr(mailfind, "fetch", no_file)
    monkeypatch.setattr(mailfind, "answers", no_answer)
