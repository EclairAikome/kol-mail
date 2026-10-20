"""The Mac build: AI command lines found where their installers put them, the sheet tool importable and locking
without msvcrt, the shell scripts parse, and pywin32 never installed on a Mac. Runs on every OS."""

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import llm  # noqa: E402
import outgoing  # noqa: E402


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # os.path.expanduser on Windows
    return tmp_path


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_codemaker_found_where_its_mac_installer_puts_it(mac_home):
    assert llm.find_codemaker() is None
    cm = executable(mac_home / ".codemaker" / "bin" / "codemaker")
    assert llm.find_codemaker() == str(cm)


def test_claude_found_in_the_native_install_folder(mac_home):
    assert llm.find_claude() is None
    claude = executable(mac_home / ".local" / "bin" / "claude")
    assert Path(llm.find_claude()) == claude


def test_configured_path_wins(mac_home):
    other = executable(mac_home / "elsewhere" / "claude")
    executable(mac_home / ".local" / "bin" / "claude")
    assert llm.find_claude(str(other)) == str(other)
    assert llm.Settings(backend="claude", claude_bin=str(other)).binary() == str(other)


def test_sheet_tool_lock_without_msvcrt(tmp_path, monkeypatch):
    monkeypatch.setenv("KOL_SHEET_STATE", str(tmp_path))
    monkeypatch.syspath_prepend(str(ROOT / "sheet"))
    kol = importlib.import_module("kol")
    kol = importlib.reload(kol)  # pick up KOL_SHEET_STATE
    lock = tmp_path / ".test.lock"
    with kol.file_lock(lock, 1, "held"):
        with pytest.raises(kol.Unavailable):
            with kol.file_lock(lock, 0.5, "held"):
                pass
    with kol.file_lock(lock, 1, "free again"):
        pass


def test_pywin32_only_on_windows():
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pywin32 = [l for l in lines if l.startswith("pywin32")]
    assert pywin32 and all('sys_platform == "win32"' in l for l in pywin32)


@pytest.mark.parametrize("script", ["install.sh", "service/run.sh", "service/open.sh"])
def test_shell_scripts_parse(script):
    bash = shutil.which("bash")
    if not bash or (os.name == "nt" and "system32" in bash.lower()):  # WSL's bash.exe cannot see Windows paths
        pytest.skip("no bash")
    raw = (ROOT / script).read_bytes()
    assert b"\r\n" not in raw, "bash scripts must have LF line endings"
    result = subprocess.run([bash, "-n", str(ROOT / script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_long_chinese_attachment_name_fits_a_mac_file_name(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    name = outgoing.clean_name("合作报价" * 40 + ".pdf")
    assert name.endswith(".pdf") and len(name.encode("utf-8")) <= 240
    assert outgoing.clean_name("short.pdf") == "short.pdf"
