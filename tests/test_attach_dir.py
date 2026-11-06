"""Attachment downloads on a Windows drive whose root lets every signed-in user change everything: mcp-email-server
refuses such a folder ("Attachment destination parent is unsafe"), so attachdir hands it a private staging folder
and moves the file into the cache afterwards."""

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import attachdir  # noqa: E402

SCREENSHOT_ERROR = "Error executing tool download_attachment: Attachment destination parent is unsafe"
AUTHENTICATED_USERS = "*S-1-5-11"  # by SID: account names are translated on a Chinese Windows

windows = pytest.mark.skipif(os.name != "nt", reason="the destination check is Windows only")


def loosen(path: Path) -> Path:
    """What the root of D: gives on the machine that failed: Authenticated Users may change everything below."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["icacls", str(path), "/grant", f"{AUTHENTICATED_USERS}:(OI)(CI)M"],
                   check=True, capture_output=True)
    return path


@pytest.fixture
def writer():
    """The code mcp-email-server runs for download_attachment: preflight, then the write."""
    reads = pytest.importorskip("mcp_email_server.adapters.reads")
    payloads = pytest.importorskip("mcp_email_server.application.reads")
    return reads.LocalArtifactWriter, payloads.AttachmentPayload


@pytest.fixture
def local_appdata(tmp_path, monkeypatch):
    """A profile folder as loose as that drive: only the staging folder itself may be private."""
    base = loosen(tmp_path / "Local")
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    return base


@windows
def test_a_cache_folder_on_a_loose_drive_is_refused_as_on_the_screenshot(tmp_path, writer):
    """Before: save_path was data\\attachments\\<hash>\\part-..., whose folder inherited the drive's ACL."""
    writer_cls, _ = writer
    cache = loosen(tmp_path / "data" / "attachments") / "0123abcd"
    cache.mkdir()  # as app.checked_attachment does before downloading
    with pytest.raises(PermissionError, match="Attachment destination parent is unsafe"):
        writer_cls._preflight(str(cache / "part-1"), "stats.png")


@windows
def test_the_staging_folder_passes_the_check_and_the_file_reaches_the_cache(tmp_path, writer, local_appdata):
    writer_cls, payload = writer
    cache = loosen(tmp_path / "data" / "attachments") / "0123abcd"
    cache.mkdir()
    target = attachdir.download_target(cache)
    assert local_appdata in target.parents and cache not in target.parents
    checked = writer_cls._preflight(str(target), "stats.png")
    saved = writer_cls._write(checked, payload("3", "stats.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"x" * 100))
    attachdir.move_into(Path(saved), cache / "file.png")
    assert (cache / "file.png").read_bytes().startswith(b"\x89PNG") and not Path(saved).exists()


@windows
def test_a_staging_folder_left_loose_is_secured_again(tmp_path, writer, local_appdata):
    writer_cls, _ = writer
    loosen(attachdir.staging_root())
    with pytest.raises(PermissionError, match="unsafe"):
        writer_cls._preflight(str(attachdir.staging_root() / "part-1"), "a.png")
    target = attachdir.download_target(tmp_path / "cache")
    assert target.parent == attachdir.staging_root()
    writer_cls._preflight(str(target), "a.png")


@windows
def test_downloads_a_crash_left_behind_are_swept_and_fresh_ones_kept(tmp_path, local_appdata):
    root = attachdir.safe_download_dir(tmp_path / "cache")
    old, fresh = root / "part-old", root / "part-fresh"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (1, 1))
    attachdir.safe_download_dir(tmp_path / "cache")
    assert not old.exists() and fresh.exists()


@windows
def test_a_junction_in_place_of_the_staging_folder_is_not_followed(tmp_path, local_appdata):
    elsewhere = loosen(tmp_path / "elsewhere")
    attachdir.staging_root().parent.mkdir(parents=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(attachdir.staging_root()), str(elsewhere)],
                   check=True, capture_output=True)
    with pytest.raises(attachdir.AttachDirError):
        attachdir.safe_download_dir(tmp_path / "cache")


@windows
def test_a_cache_folder_swapped_for_a_junction_gets_nothing(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cache = tmp_path / "attachments"
    cache.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(cache / "slot"), str(elsewhere)], check=True, capture_output=True)
    src = tmp_path / "part"
    src.write_bytes(b"x")
    with pytest.raises(attachdir.AttachDirError):
        attachdir.move_into(src, cache / "slot" / "file.png")
    assert not (elsewhere / "file.png").exists()


@pytest.mark.skipif(os.name == "nt", reason="elsewhere the cache folder is used as before")
def test_elsewhere_the_cache_folder_is_kept(tmp_path):
    assert attachdir.safe_download_dir(tmp_path) == tmp_path


@pytest.mark.parametrize("error", [OSError(errno.EXDEV, "cross-device link"),
                                   OSError(0, "not the same device", None, 17)])  # how Windows says it
def test_a_file_from_another_drive_is_copied_then_renamed(tmp_path, monkeypatch, error):
    src, dst = tmp_path / "part-1", tmp_path / "cache" / "file.png"
    src.write_bytes(b"data")
    real_replace = os.replace

    def replace(a, b):
        if Path(a) == src:
            raise error
        real_replace(a, b)
    monkeypatch.setattr(attachdir.os, "replace", replace)
    attachdir.move_into(src, dst)
    assert dst.read_bytes() == b"data" and not src.exists() and os.listdir(dst.parent) == ["file.png"]


def test_the_refusal_is_explained_in_plain_chinese():
    assert "权限不安全" in attachdir.explain(SCREENSHOT_ERROR)
    assert attachdir.explain("Error executing tool download_attachment: Attachment destination could not be written")
    assert attachdir.explain("IMAP login failed") is None
    assert attachdir.explain("") is None
