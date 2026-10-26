"""Shared pieces for the regression tests: each test in this folder replays one bug a colleague hit while installing
KOL Mail, against the installer's own code (install.ps1 / install.sh) wherever the bug lived there.

- `ai_check` runs the Python script install.ps1 uses as its AI check, unchanged.
- `fake_codemaker` puts tests/fakes/fake_codemaker.py on disk as an old or new `codemaker`, so the check runs end to
  end without a login.
- `run_ps_functions` loads named functions out of install.ps1 with the PowerShell parser and runs a snippet with them."""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = ROOT / "install.ps1"
INSTALL_SH = ROOT / "install.sh"

sys.path.insert(0, str(ROOT / "tests" / "fakes"))
import fake_codemaker as fake_cm  # noqa: E402


@pytest.fixture
def fake_codemaker(tmp_path):
    """make(version, delete_seconds) -> (path of a runnable `codemaker`, log file of its calls)."""
    def make(version="1.18.31", delete_seconds=0.0):
        folder = tmp_path / f"codemaker-{version}"
        exe = fake_cm.install(folder, version, folder / "calls.jsonl", delete_seconds)
        return exe, folder / "calls.jsonl"
    return make


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line)["argv"] for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def ai_check(tmp_path):
    """run(mode, codemaker_exe, timeout) runs install.ps1's AI check as Invoke-AiCheck does; returns
    (exit code, the JSON line the installer reads or None, seconds taken, stdout)."""
    src = INSTALL_PS1.read_text(encoding="utf-8-sig")
    code = re.search(r"\$aiCheckCode = @'\r?\n(.*?)\r?\n'@", src, re.S).group(1)
    script = tmp_path / "aicheck.py"
    script.write_text(code, encoding="utf-8")
    # The installed app folder, holding only llm.py: the check must not read this checkout's data/settings.json.
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy2(ROOT / "llm.py", app / "llm.py")

    def run(mode, exe, timeout):
        import time
        env = {k: v for k, v in os.environ.items()
               if k not in ("KOL_LLM_BACKEND", "KOL_TRANSLATE_MODEL", "KOL_DRAFT_MODEL", "KOL_CLAUDE_BIN")}
        env["KOL_CODEMAKER_BIN"] = str(exe)
        env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp_path)  # the check's work folder
        started = time.monotonic()
        try:
            r = subprocess.run([sys.executable, "-I", str(script), str(app), "codemaker", mode, ""],
                               capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            pytest.fail(f"the installer's AI check ({mode}) was still running after {timeout} s")
        took = time.monotonic() - started
        out = r.stdout.decode("utf-8", "replace")
        lines = [l for l in out.splitlines() if l.strip().startswith("{")]
        return r.returncode, (json.loads(lines[-1]) if lines else None), took, out
    return run


def powershell() -> str | None:
    return shutil.which("powershell.exe") if sys.platform == "win32" else None


@pytest.fixture
def run_ps_functions(tmp_path):
    """run(names, body, timeout) loads the functions `names` from install.ps1 (by the PowerShell parser, so the rest
    of the installer never runs) and then runs `body`; returns the CompletedProcess. A name starting with "?" is
    loaded when it exists (a helper older installers did not have)."""
    exe = powershell()
    if not exe:
        pytest.skip("Windows PowerShell 5.1 only (install.ps1 is the Windows installer)")

    def run(names, body, timeout=60):
        loader = textwrap.dedent(f"""
            $ErrorActionPreference = "Stop"
            [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false  # what the test reads back
            $tokens = $null; $errors = $null
            $ast = [Management.Automation.Language.Parser]::ParseFile('{INSTALL_PS1}', [ref]$tokens, [ref]$errors)
            $wanted = @({", ".join(f"'{n.lstrip('?')}'" for n in names)})
            $required = @({", ".join(f"'{n}'" for n in names if not n.startswith('?')) or "$null"})
            $found = $ast.FindAll({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }}, $true) |
                Where-Object {{ $wanted -contains $_.Name }}
            $missing = @($required | Where-Object {{ $_ -and @($found.Name) -notcontains $_ }})
            if ($missing) {{ throw "install.ps1 has no function $($missing -join ', ')" }}
            foreach ($f in $found) {{ . ([scriptblock]::Create($f.Extent.Text)) }}
        """)
        ps1 = tmp_path / "run.ps1"
        ps1.write_text(loader + textwrap.dedent(body), encoding="utf-8-sig")  # 5.1 reads BOM-less files as ANSI
        try:
            return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                                  capture_output=True, timeout=timeout,
                                  creationflags=subprocess.CREATE_NO_WINDOW)  # a console of its own, like the kit's
        except subprocess.TimeoutExpired:
            pytest.fail(f"PowerShell was still running after {timeout} s")
    return run
