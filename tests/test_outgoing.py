"""Reply attachments: only files uploaded here can be attached, names are made safe, sizes and types are bounded."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import outgoing  # noqa: E402


def test_upload_and_resolve(tmp_path):
    store = outgoing.Store(tmp_path)
    a = store.add("media kit.pdf", b"%PDF-1.4 x")
    b = store.add("screenshot.png", b"\x89PNG data")
    assert a["name"] == "media kit.pdf" and b["image"] and not a["image"]
    paths = store.resolve([a["id"], b["id"], a["id"]])
    assert [p.name for p in paths] == ["media kit.pdf", "screenshot.png"] and all(p.is_absolute() for p in paths)


def test_names_cannot_escape_or_disguise(tmp_path):
    store = outgoing.Store(tmp_path)
    info = store.add("..\\..\\Windows\\evil.pdf", b"x")
    assert info["name"] == "evil.pdf"
    assert store.path(info["id"]).parent.parent == tmp_path
    assert outgoing.clean_name("report‮gpj.pdf") == "reportgpj.pdf"   # right-to-left override removed
    assert outgoing.clean_name("CON.txt") == "_CON.txt"
    assert outgoing.clean_name("   ") == "attachment"


@pytest.mark.parametrize("name", ["setup.exe", "run.BAT", "x.js", "deck.pptm", "page.html", "a.lnk"])
def test_programs_are_refused(tmp_path, name):
    with pytest.raises(outgoing.AttachmentError):
        outgoing.Store(tmp_path).add(name, b"x")


def test_limits(tmp_path, monkeypatch):
    store = outgoing.Store(tmp_path)
    with pytest.raises(outgoing.AttachmentError):
        store.add("empty.pdf", b"")
    monkeypatch.setattr(outgoing, "MAX_TOTAL", 10)
    ids = [store.add(f"{i}.txt", b"123456")["id"] for i in range(2)]
    with pytest.raises(outgoing.AttachmentError):
        store.resolve(ids)                       # 12 bytes over a 10-byte total
    with pytest.raises(outgoing.AttachmentError):
        store.resolve(["../../etc/passwd"])      # not an id
    with pytest.raises(outgoing.AttachmentError):
        store.resolve(["0123456789abcdef"])      # an id that does not exist


def test_cleanup_drops_old_uploads(tmp_path):
    store = outgoing.Store(tmp_path)
    old = store.add("old.txt", b"x")["id"]
    new = store.add("new.txt", b"x")["id"]
    store.cleanup(now=time.time() + outgoing.KEEP_SECONDS + 10)
    assert not (tmp_path / old).exists() and not (tmp_path / new).exists()
    keep = store.add("keep.txt", b"x")["id"]
    store.cleanup()
    assert (tmp_path / keep).exists()
