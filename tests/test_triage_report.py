"""scripts/triage_report.py: a copied feedback message becomes a masked, failing test skeleton."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import triage_report as tr  # noqa: E402

# Private-looking details are put together at run time: written out whole, the release scan would refuse this file.
USER, MAC_USER = "wang" + ".wei", "li" + ".na"
ADDRESS = USER + "@" + "lootbar-like.com"
HOOK = "https://open.popo.example/open-apis/" + "robots/v1/hook/" + "abcdefghijklmnop1234"

WIN_REPORT = f"""KOL Mail  10:42
【KOL Mail 匿名反馈 · 问题】
装到第五步卡住了，截图里是这些：
[4/8] 配置邮箱…
[5/8] 连接 AI…
  检测到：CodeMaker
Traceback (most recent call last):
  File "C:\\Users\\{USER}\\AppData\\Local\\kol-mail\\app\\llm.py", line 402, in _codemaker
    raise LLMError(msg)
llm.LLMError: CodeMaker 调用失败：No context found for instance
codemaker 1.15.11, Python 3.12.10, 发信人 {ADDRESS}
webhook {HOOK}

(版本 0.4.0)
"""

MAC_REPORT = f"""【KOL Mail 匿名反馈 · 问题】
安装报错 curl: (22) The requested URL returned error: 403 rate limit
/Users/{MAC_USER}/Library/Application Support/kol-mail/server.log

(版本 0.3.3)
"""


@pytest.fixture(autouse=True)
def no_denylist(monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "DENYLIST", tmp_path / "no-denylist.txt")


def test_a_report_becomes_a_failing_masked_skeleton(tmp_path):
    [r] = tr.triage(WIN_REPORT, tmp_path)
    text = r["path"].read_text(encoding="utf-8")
    for private in (USER, "lootbar-like", "abcdefghijklmnop1234"):
        assert private not in text
    assert "C:\\\\Users\\\\alex\\\\AppData" in text and "someone@example.com" in text
    assert tr.leaks(text) == []
    info = r["facts"]
    assert (info["version"], info["platform"], info["python"], info["codemaker"], info["step"]) == \
        ("0.4.0", "Windows", "3.12.10", "1.15.11", "5/8")
    assert info["error"] == "llm.LLMError: CodeMaker 调用失败：No context found for instance"
    assert f"Signature: {r['signature']}" in text
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(r["path"])],
                         capture_output=True, text=True, cwd=tmp_path)
    assert run.returncode == 1 and "not reproduced yet" in run.stdout


def test_the_same_bug_again_is_recognised(tmp_path):
    [first] = tr.triage(WIN_REPORT, tmp_path)
    again = WIN_REPORT.replace(USER, "zhao" + ".lei").replace("line 402", "line 417").replace("10:42", "15:03")
    [second] = tr.triage(again, tmp_path)
    assert second["path"] is None and second["known"] == first["path"]
    [forced] = tr.triage(again, tmp_path, force=True)
    assert forced["path"] != first["path"] and forced["path"].exists()


def test_a_report_of_a_bug_with_a_regression_test_is_known():
    """The five install bugs already fixed: their tests' Matches: lines catch new reports of them."""
    [r] = tr.triage(WIN_REPORT, tr.OUT_DIR, dry_run=True)
    assert r["path"] is None and r["known"].name == "test_install_old_codemaker.py"
    [r] = tr.triage(MAC_REPORT.replace("403 rate limit", "403: API rate limit exceeded for 1.2.3.4"), tr.OUT_DIR,
                    dry_run=True)
    assert r["known"].name == "test_install_rate_limited_github.py"


def test_several_messages_in_one_paste_are_triaged_one_by_one(tmp_path):
    results = tr.triage(WIN_REPORT + "\n" + MAC_REPORT, tmp_path)
    assert len(results) == 2
    mac = results[1]
    assert mac["facts"]["platform"] == "macOS" and mac["facts"]["version"] == "0.3.3"
    text = mac["path"].read_text(encoding="utf-8")
    assert MAC_USER not in text and "/Users/alex/Library" in text


def test_denylist_patterns_are_masked(tmp_path, monkeypatch):
    deny = tmp_path / "deny.txt"
    deny.write_text("# private\nsecret-project\n", encoding="utf-8")
    monkeypatch.setattr(tr, "DENYLIST", deny)
    [r] = tr.triage(MAC_REPORT.replace("安装报错", "secret-project 安装报错"), tmp_path / "out")
    assert "secret-project" not in r["path"].read_text(encoding="utf-8")


def test_dry_run_writes_nothing_and_cli_reads_stdin(tmp_path):
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "triage_report.py"), "-", "--dry-run", "--out-dir", str(out)],
                       input=MAC_REPORT.encode("utf-8"), capture_output=True)
    assert r.returncode == 0 and b"would write" in r.stdout
    assert not out.exists()


def test_report_text_can_neither_break_nor_run_code_in_the_generated_file(tmp_path):
    """The report is anonymous text that ends up in a .py file: a Windows path in the error line once made it a
    SyntaxError, and a crafted line could close the docstring and run code when pytest imported it."""
    marker = tmp_path / "ran"
    evil = (f'【KOL Mail 匿名反馈 · 问题"""】\nFileNotFoundError: [WinError 3] C:\\Windows\\Temp\\x '
            f'"""; open({str(marker)!r}, "w"); x = """\n(版本 0.4.0)\n')
    [r] = tr.triage(evil, tmp_path)
    source = r["path"].read_text(encoding="utf-8")
    compile(source, "x.py", "exec")
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(r["path"])],
                         capture_output=True, text=True, cwd=tmp_path)
    assert "not reproduced yet" in run.stdout and not marker.exists()
    assert tr.leaks(source) == []


def test_an_allowed_address_starting_a_line_stays_allowed_in_the_file(tmp_path):
    report = "\n".join(["Delivery failed", "mailer-daemon@googlemail.com", "noreply@example.com\terror: rejected",
                        "(版本 0.4.0)"])
    [r] = tr.triage(report, tmp_path)
    source = r["path"].read_text(encoding="utf-8")
    assert [hit for line in source.splitlines() for hit in tr.leaks(line)] == []


@pytest.mark.parametrize("path", [
    "C:\\Users\\{u} Ming\\AppData\\x", "C:/Users/{u} Ming/AppData/x", "/c/Users/{u}/AppData/x",
    "/mnt/c/Users/{u}/AppData/x", "\\Users\\{u}\\AppData\\x", "/Users/{u} Ming/Library/x"])
def test_user_folders_are_masked_in_every_spelling(path):
    masked = tr.mask(path.format(u=USER))
    assert USER not in masked and "Ming" not in masked and "alex" in masked


def test_ip_addresses_are_masked_but_localhost_stays():
    masked = tr.mask("API rate limit exceeded for 203.0.113.7; server 127.0.0.1:8765; Python 3.12.10")
    assert "203.0.113.7" not in masked and "127.0.0.1" in masked and "3.12.10" in masked


def test_signatures_ignore_paths_numbers_and_values():
    a = tr.signature("FileNotFoundError: [Errno 2] No such file: 'C:\\Users\\alex\\x.json'")
    b = tr.signature("FileNotFoundError: [Errno 2] No such file: 'D:\\data\\y.json'")
    assert a == b
    assert a != tr.signature("PermissionError: [Errno 13] Permission denied: 'C:\\x'")


def test_an_automatic_report_is_read_too(tmp_path):
    auto = ("【KOL Mail 自动报错 · 安装失败 Windows】\n错误：Could not install packages due to an OSError: [WinError 206] "
            "The filename or extension is too long\n卡在：[3/8]\n环境：Microsoft Windows NT 10.0 · PowerShell 5.1\n\n"
            "(版本 0.4.1)")
    [r] = tr.triage(auto, tmp_path)
    assert r["facts"]["kind"] == "安装失败 Windows" and r["facts"]["step"] == "3/8" and r["facts"]["version"] == "0.4.1"
    assert "路径太长" in r["hint"] and "Likely cause: 路径太长" in r["path"].read_text(encoding="utf-8")
