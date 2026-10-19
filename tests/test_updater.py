"""Self-update: only a newer, correctly signed release installs; data/ is never touched; anything unsigned, altered or
unsafe is refused and the running code stays as it was."""

import base64
import io
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import updater  # noqa: E402

KEY = Ed25519PrivateKey.generate()
PUB = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def release(files: dict[str, str], version: str = "0.2.0", key=KEY, tamper: dict | None = None,
            top: str = "kol-mail-0.2.0") -> bytes:
    files = {"VERSION": version, "app.py": "x = 1\n", **files}
    tmp = Path(__import__("tempfile").mkdtemp())
    for name, text in files.items():
        (tmp / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp / name).write_text(text, encoding="utf-8")
    manifest = updater.build_manifest(tmp, list(files), version)
    signature = base64.b64encode(key.sign(manifest))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in {**files, **(tamper or {})}.items():
            z.writestr(f"{top}/{name}", text)
        z.writestr(f"{top}/{updater.MANIFEST}", manifest)
        z.writestr(f"{top}/{updater.SIGNATURE}", signature)
    return buf.getvalue()


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "PUBLIC_KEY", PUB)
    b = tmp_path / "app"
    (b / "data").mkdir(parents=True)
    (b / "data" / "settings.json").write_text('{"mine": true}', encoding="utf-8")
    (b / "app.py").write_text("old = 1\n", encoding="utf-8")
    (b / "gone.py").write_text("old only\n", encoding="utf-8")
    (b / "VERSION").write_text("0.1.0", encoding="utf-8")
    return b


def updater_for(base):
    up = updater.Updater(base, base / "data", "owner/kol-mail", "0.1.0")
    up.install_requirements = lambda staging: None
    return up


def test_version_compare():
    assert updater.newer("v0.2.0", "0.1.9") and updater.newer("1.0.0", "0.9.9")
    assert not updater.newer("0.1.0", "0.1.0") and not updater.newer("v0.0.9", "0.1.0")
    assert not updater.newer("latest", "0.1.0")


def test_signed_release_swaps_code_and_keeps_data(base):
    updater_for(base).install(release({"static/index.html": "page"}), "0.2.0")
    assert (base / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (base / "static" / "index.html").exists()
    assert not (base / "gone.py").exists()
    assert (base / "data" / "settings.json").read_text(encoding="utf-8") == '{"mine": true}'


@pytest.mark.parametrize("blob", [
    lambda: release({}, key=Ed25519PrivateKey.generate()),                 # someone else's key
    lambda: release({}, tamper={"app.py": "import evil\n"}),               # altered after signing
    lambda: release({}, tamper={"extra.py": "import evil\n"}),             # unsigned extra file
    lambda: release({"app.py": "def broken(:\n"}),                         # signed but does not compile
    lambda: release({}, version="0.1.5"),                                   # signed for another version
    lambda: release({"DATA/settings.json": "{}"}),                          # data/ in any case
])
def test_bad_releases_are_refused_and_nothing_changes(base, blob):
    with pytest.raises(updater.UpdateError):
        updater_for(base).install(blob(), "0.2.0")
    assert (base / "app.py").read_text(encoding="utf-8") == "old = 1\n"
    assert (base / "gone.py").exists()


def test_unsigned_zip_is_refused(base):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("kol-mail-0.2.0/app.py", "x = 1\n")
        z.writestr("kol-mail-0.2.0/VERSION", "0.2.0")
    with pytest.raises(updater.UpdateError):
        updater_for(base).install(buf.getvalue(), "0.2.0")


def test_failed_swap_restores_the_old_code(base, monkeypatch):
    real = updater.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("file in use")
        return real(src, dst)
    monkeypatch.setattr(updater.os, "replace", flaky)
    with pytest.raises(OSError):
        updater_for(base).install(release({"static/index.html": "page"}), "0.2.0")
    assert (base / "app.py").read_text(encoding="utf-8") == "old = 1\n"
    assert (base / "gone.py").exists() and not (base / "static" / "index.html").exists()


def test_developer_checkout_never_updates(base):
    (base / ".git").mkdir()
    assert not updater.Updater(base, base / "data", "owner/kol-mail", "0.1.0").enabled
