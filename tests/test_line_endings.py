"""tools/check_line_endings.py, the pre-commit hook's check, in a throwaway repository."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str):
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args], cwd=repo,
                          capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("needs git")
    (tmp_path / "tools").mkdir()
    shutil.copy2(ROOT / "tools" / "check_line_endings.py", tmp_path / "tools")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "ci@example.com")
    git(tmp_path, "config", "user.name", "CI")
    git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / "a.py").write_bytes(b"one\ntwo\n")
    (tmp_path / "pic.bin").write_bytes(b"\0\r\n\0")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "start")
    return tmp_path


def check(repo: Path, *args: str):
    r = subprocess.run([sys.executable, "tools/check_line_endings.py", *args], cwd=repo, capture_output=True, text=True)
    return r.returncode, r.stderr


def test_an_ordinary_change_passes(repo):
    (repo / "a.py").write_bytes(b"one\ntwo\nthree\n")
    git(repo, "add", "a.py")
    assert check(repo) == (0, "")


def test_a_file_turned_crlf_fails(repo):
    (repo / "a.py").write_bytes(b"one\r\ntwo\r\n")
    git(repo, "add", "a.py")
    code, err = check(repo)
    assert code == 1 and "a.py: line endings change from LF to CRLF" in err


def test_one_crlf_line_is_a_change_too(repo):
    (repo / "a.py").write_bytes(b"one\ntwo\r\n")
    git(repo, "add", "a.py")
    code, err = check(repo)
    assert code == 1 and "LF to MIXED" in err


def test_a_new_crlf_file_fails_and_a_new_lf_file_passes(repo):
    (repo / "new.md").write_bytes(b"x\r\n")
    (repo / "ok.md").write_bytes(b"x\n")
    git(repo, "add", ".")
    code, err = check(repo)
    assert code == 1 and "new.md: new file with CRLF" in err and "ok.md" not in err


def test_a_renamed_file_is_compared_with_its_old_self(repo):
    git(repo, "mv", "a.py", "b.py")
    (repo / "b.py").write_bytes(b"one\r\ntwo\r\n")
    git(repo, "add", "b.py")
    code, err = check(repo)
    assert code == 1 and "b.py: line endings change from LF to CRLF" in err


def test_binary_files_are_left_alone(repo):
    (repo / "pic.bin").write_bytes(b"\0\n\0")
    git(repo, "add", "pic.bin")
    assert check(repo)[0] == 0


def test_a_file_turned_cr_only_fails(repo):
    (repo / "a.py").write_bytes(b"one\rtwo\r")
    git(repo, "add", "a.py")
    code, err = check(repo)
    assert code == 1 and "LF to CR" in err


def test_a_change_to_the_line_ending_rules_is_stopped(repo):
    (repo / ".gitattributes").write_bytes(b"* -text\n")  # would switch the check off for everything
    (repo / "a.py").write_bytes(b"one\r\ntwo\r\n")
    git(repo, "add", ".")
    code, err = check(repo)
    assert code == 1 and ".gitattributes: the line-ending rules themselves change" in err


def test_with_the_repositorys_gitattributes_git_itself_keeps_files_lf(repo):
    shutil.copy2(ROOT / ".gitattributes", repo / ".gitattributes")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "rules")
    (repo / "a.py").write_bytes(b"one\r\ntwo\r\n")  # an editor's CRLF is normalised when staged
    (repo / "x.ps1").write_bytes(b"Write-Host 1\r\n")
    git(repo, "add", ".")
    assert check(repo) == (0, "")
    assert git(repo, "cat-file", "blob", ":a.py").stdout == b"one\ntwo\n"


def test_all_mode_finds_crlf_anywhere_in_the_repository(repo):
    (repo / "c.txt").write_bytes(b"x\r\n")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-q", "--no-verify", "-m", "crlf")
    code, err = check(repo, "--all")
    assert code == 1 and "c.txt: CRLF" in err and "pic.bin" not in err


def test_this_repository_is_all_lf():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_line_endings.py"), "--all"], capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr
