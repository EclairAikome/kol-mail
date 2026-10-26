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
