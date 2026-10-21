"""The installer's AI check (the Python script inside install.ps1) must answer at once: in v0.3.1 its watchdog thread
kept every "version" check alive for 11 minutes, after which the installer read the watchdog's line and told the
colleague "CodeMaker not found"."""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_script(tmp_path, watchdog_seconds: int) -> Path:
    src = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    code = re.search(r"\$aiCheckCode = @'\r?\n(.*?)\r?\n'@", src, re.S).group(1)
    code, n = re.subn(r"threading\.Timer\(660,", f"threading.Timer({watchdog_seconds},", code)
    assert n == 1, "the watchdog line moved: update this test"
    script = tmp_path / "aicheck.py"
    script.write_text(code, encoding="utf-8")
    return script


def test_version_check_returns_at_once_and_is_what_the_installer_reads(tmp_path):
    script = check_script(tmp_path, 30)
    started = time.monotonic()
    r = subprocess.run([sys.executable, "-I", str(script), str(ROOT), "codemaker", "version", ""],
                       capture_output=True, timeout=60)
    took = time.monotonic() - started
    lines = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip().startswith("{")]
    assert took < 20, f"the check waited {took:.0f} s: something keeps the process alive"
    assert r.returncode == 0 and len(lines) == 1
    last = json.loads(lines[-1])  # Invoke-AiCheck reads the last line starting with "{"
    assert "binary" in last and last["errors"] == []


def test_the_watchdog_is_a_daemon_thread():
    src = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "_dog.daemon = True" in src
    assert re.search(r'print\(json\.dumps\(out\), flush=True\)\r?\n    os\._exit\(0\)', src)  # the version branch
