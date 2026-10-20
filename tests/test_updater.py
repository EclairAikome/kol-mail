"""Self-update: only a newer, correctly signed release installs; data/ is never touched; anything unsigned, altered or
unsafe is refused and the running code stays as it was."""

import asyncio
import base64
import io
import sys
import zipfile
from pathlib import Path

import httpx
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


# ---------------------------------------------------------------- finding the newest release

RELEASES = "https://github.com/owner/kol-mail/releases"
API = "https://api.github.com/repos/owner/kol-mail/releases/latest"


def find_latest(base, github, mirror=""):
    """Ask a stand-in GitHub for the newest release; returns the result and every URL that was asked."""
    asked = []

    def handle(request):
        asked.append(str(request.url))
        return github(request)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
            return await updater.Updater(base, base / "data", "owner/kol-mail", "0.1.0", mirror=mirror).latest(client)
    return asyncio.run(go()), asked


def test_latest_comes_from_the_redirect_without_the_rate_limited_api(base):
    def github(request):
        if str(request.url) == f"{RELEASES}/latest":
            return httpx.Response(302, headers={"location": f"{RELEASES}/tag/v0.2.1"})
        raise AssertionError(f"unexpected request {request.url}")
    rel, asked = find_latest(base, github)
    assert rel["tag"] == "v0.2.1" and rel["version"] == "0.2.1"
    assert rel["zip"] == "https://codeload.github.com/owner/kol-mail/zip/refs/tags/v0.2.1"
    assert asked == [f"{RELEASES}/latest"]


def test_redirect_goes_through_the_mirror(base):
    def github(request):
        return httpx.Response(302, headers={"location": f"https://gh.example.com/{RELEASES}/tag/v0.3.0"})
    rel, asked = find_latest(base, github, mirror="https://gh.example.com/")
    assert rel["version"] == "0.3.0" and asked == [f"https://gh.example.com/{RELEASES}/latest"]


def test_api_is_the_fallback_when_the_redirect_has_no_tag(base):
    def github(request):
        if str(request.url) == f"{RELEASES}/latest":
            return httpx.Response(200, text="<html>")
        return httpx.Response(200, json={"tag_name": "v0.2.1", "body": "notes"})
    rel, asked = find_latest(base, github)
    assert rel["version"] == "0.2.1" and rel["notes"] == "notes" and asked[-1] == API


def test_no_release_yet(base):
    def github(request):
        if str(request.url) == f"{RELEASES}/latest":
            return httpx.Response(302, headers={"location": RELEASES})
        return httpx.Response(404, json={"message": "Not Found"})
    with pytest.raises(updater.UpdateError, match="还没有发布"):
        find_latest(base, github)


def test_rate_limited_api_says_so(base):
    def github(request):
        if str(request.url) == f"{RELEASES}/latest":
            raise httpx.ConnectError("blocked")
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "API rate limit exceeded"})
    with pytest.raises(updater.UpdateError, match="限制了这个网络的查询次数"):
        find_latest(base, github)
