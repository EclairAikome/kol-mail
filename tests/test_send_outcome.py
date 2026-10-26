"""send_email results: a mail SMTP accepted for every recipient counts as sent, even when its Sent copy is missing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

TO = ["kyle@example.com"]
MID = "<179264713907.15368.11641093133755813865@example.com>"


def test_clean_send():
    assert app.send_outcome(f"Email sent successfully to kyle@example.com. Message-Id: {MID}", TO) == (True, "", MID)
    assert app.send_outcome("Email sent successfully to kyle@example.com with 2 attachment(s)", TO) == (True, "", None)


def test_delivered_but_sent_copy_missing():
    for copy in ("failed", "failed (mailbox-unavailable)", "unknown (Sent Items; append-unknown)"):
        result = f"Email delivery [succeeded: kyle@example.com; message-id: {MID}; sent-copy: {copy}]"
        assert app.send_outcome(result, TO) == (True, app.COPY_MISSING, MID)
    result = "Email delivery [succeeded: Kyle@Example.com; sent-copy: failed]"
    assert app.send_outcome(result, TO) == (True, app.COPY_MISSING, None)


def test_copy_saved_but_index_not_refreshed():
    result = (f"Email delivery [succeeded: kyle@example.com; message-id: {MID}; sent-copy: succeeded (Sent Items); "
              "warning: reconciliation needed]")
    assert app.send_outcome(result, TO) == (True, "", MID)


def test_delivery_not_proven_stays_uncertain():
    for result in (
        "Email delivery [unknown: kyle@example.com (smtp-unknown); sent-copy: skipped]",
        "Email delivery [failed: kyle@example.com; sent-copy: skipped]",
        "Email delivery [succeeded: other@example.com; sent-copy: failed]",  # not the address we meant
        "Email delivery [succeeded: kyle@example.com; failed: b@example.com; sent-copy: failed]",
        "something unexpected",
        "",
    ):
        assert app.send_outcome(result, TO) == (False, "", None), result
    two = "Email delivery [succeeded: kyle@example.com; sent-copy: failed]"
    assert app.send_outcome(two, ["kyle@example.com", "b@example.com"])[0] is False
