"""Regression for 368d536 (v0.2.1). A colleague's install stopped at step 2 with "API rate limit exceeded": the GitHub
REST API allows 60 anonymous calls an hour per IP, and the company network shares one. Both installers must read the
newest tag from where github.com/<repo>/releases/latest redirects, and touch the API only when that gives nothing.

A local server stands in for GitHub through the installers' mirror prefix: the redirect works, the API is rate
limited, and every request is recorded.

Matches: API rate limit exceeded"""

import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import INSTALL_SH

REPO = "example/kol-mail"
TAG = "v9.8.7"


@pytest.fixture
def github(request):
    """A stand-in GitHub; `redirect` False makes releases/latest answer 404 (so the API fallback is used)."""
    seen = []
    state = {"redirect": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def answer(self, body):
            path = self.path.lstrip("/")
            seen.append((self.command, path))
            if path == f"https://github.com/{REPO}/releases/latest" and state["redirect"]:
                self.send_response(302)
                self.send_header("Location", f"https://github.com/{REPO}/releases/tag/{TAG}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == f"https://api.github.com/repos/{REPO}/releases/latest":
                msg = b'{"message": "API rate limit exceeded for 203.0.113.7."}'
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                if body:
                    self.wfile.write(msg)
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_HEAD(self):
            self.answer(False)

        def do_GET(self):
            self.answer(True)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen, state
    server.shutdown()


def api_calls(seen):
    return [p for _, p in seen if p.startswith("https://api.github.com/")]


def test_windows_installer_reads_the_tag_from_the_redirect(run_ps_functions, github):
    mirror, seen, _ = github
    r = run_ps_functions(["Gh", "Get-LatestTag"], f"""
        $Repo = '{REPO}'; $Mirror = '{mirror}'
        Write-Output "TAG=$(Get-LatestTag)"
    """)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace") + out
    assert f"TAG={TAG}" in out
    assert api_calls(seen) == [], "the rate-limited API was called although the redirect had the tag"


def test_windows_installer_says_what_to_do_when_github_gives_nothing(run_ps_functions, github):
    mirror, seen, state = github
    state["redirect"] = False
    r = run_ps_functions(["Gh", "Get-LatestTag"], f"""
        $Repo = '{REPO}'; $Mirror = '{mirror}'
        try {{ Get-LatestTag; exit 3 }} catch {{ Write-Output "ERR=$($_.Exception.Message)"; exit 0 }}
    """)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out
    assert "换个网络" in out and "rate limit" not in out  # advice, not GitHub's raw refusal
    assert len(api_calls(seen)) == 1  # the fallback was tried once


def sh_tag_block() -> str:
    """The lines of install.sh that find the newest tag (from `TAG="$(curl` up to the version check)."""
    src = INSTALL_SH.read_text(encoding="utf-8")
    m = re.search(r'^(    TAG="\$\(curl -sS -I .*?)^    printf \'%s\' "\$TAG"', src, re.S | re.M)
    assert m, "install.sh's tag lookup moved: update this test"
    return m.group(1)


# install.sh is the Mac installer; on Windows "bash" may be WSL's launcher with no Linux installed
@pytest.mark.skipif(sys.platform == "win32" or not (shutil.which("bash") and shutil.which("curl")),
                    reason="the Mac installer, run where it runs")
def test_mac_installer_reads_the_tag_from_the_redirect(github, tmp_path):
    mirror, seen, _ = github
    script = tmp_path / "tag.sh"
    script.write_text(
        f'REPO="{REPO}"; MIRROR="{mirror}"; BASEPY="{sys.executable}"\n'
        'gh_url() { if [ -n "$MIRROR" ]; then printf \'%s/%s\' "${MIRROR%/}" "$1"; else printf \'%s\' "$1"; fi; }\n'
        + sh_tag_block() + 'printf "TAG=%s\\n" "$TAG"\n', encoding="utf-8", newline="\n")
    r = subprocess.run(["bash", str(script)], capture_output=True, timeout=60)
    out = r.stdout.decode("utf-8", "replace")
    assert f"TAG={TAG}" in out, out + r.stderr.decode("utf-8", "replace")
    assert api_calls(seen) == []
