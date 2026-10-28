"""Automatic error reports (autoreport.py), the app's hooks into them, and the installers' own reports."""

import asyncio
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import autoreport  # noqa: E402

HOOK = "https://popo.example/robot/x"
BOT_TOKEN = "123456789:" + "AA" + "b" * 33  # put together: written out, the release scan would refuse this file


@pytest.fixture
def sent(monkeypatch, tmp_path):
    out = []
    monkeypatch.setattr(autoreport, "post", lambda text, timeout=15: out.append(text) or True)
    monkeypatch.delenv("KOL_DEV", raising=False)
    autoreport.configure(HOOK, "0.4.1", tmp_path / "autoreport.json", who="Me <me@example.com>",
                         context=lambda: ["AI CodeMaker codemaker.exe"])
    autoreport._recent_log.clear()
    yield out
    autoreport.configure("", "?", None)


def test_a_report_says_where_what_why_and_on_which_machine(sent):
    autoreport.note_log("10:00:01 translator started")
    try:
        raise RuntimeError("Error: No context found for instance")
    except RuntimeError as e:
        assert autoreport.report("AI", str(e), exc=e, wait=True)
    [text] = sent
    assert text.startswith("【KOL Mail 自动报错 · AI】") and "KOL Mail" in text  # the robot's keyword
    assert "错误：Error: No context found for instance" in text
    assert "可能原因：CodeMaker 版本太旧" in text
    assert "Me <me@example.com>" in text and "AI CodeMaker codemaker.exe" in text and "Python " in text
    assert "Traceback (most recent call last)" in text and "10:00:01 translator started" in text
    assert text.endswith("(版本 0.4.1)")


def test_secrets_never_leave_the_machine(sent):
    autoreport.report("Telegram", f"bot {BOT_TOKEN} failed",
                      f"posting to {HOOK}/ho" + "ok/abcdefghijklmnop1234 password=hunter22", wait=True)
    [text] = sent
    assert BOT_TOKEN not in text and "abcdefghijklmnop1234" not in text and "hunter22" not in text


def test_the_same_error_goes_once_every_six_hours_even_across_restarts(sent, tmp_path, monkeypatch):
    assert autoreport.report("AI", "timed out after 180 s", wait=True)
    assert not autoreport.report("AI", "timed out after 181 s", wait=True)  # numbers differ, same error
    assert autoreport.report("邮箱连接", "timed out after 180 s", wait=True)  # elsewhere: a new one
    autoreport.configure(HOOK, "0.4.1", tmp_path / "autoreport.json")  # the app restarted
    assert not autoreport.report("AI", "timed out after 180 s", wait=True)
    now = autoreport.time.time()
    monkeypatch.setattr(autoreport.time, "time", lambda: now + autoreport.COOLDOWN + 1)
    assert autoreport.report("AI", "timed out after 180 s", wait=True)
    assert len(sent) == 3


def test_a_report_the_robot_did_not_take_goes_again_next_time(monkeypatch, tmp_path):
    """The laptop wakes, the app crashes before the network is up: that report must not be counted as sent."""
    out, up = [], {"net": False}
    monkeypatch.setattr(autoreport, "post", lambda text, timeout=15: up["net"] and (out.append(text) or True))
    monkeypatch.delenv("KOL_DEV", raising=False)
    autoreport.configure(HOOK, "0.4.1", tmp_path / "a.json")
    assert autoreport.report("程序退出", "ImportError: x", wait=True) is False
    assert autoreport.report("AI", "background, no network")  # queued
    autoreport.flush(10)
    up["net"] = True
    assert autoreport.report("程序退出", "ImportError: x", wait=True) is True
    assert autoreport.report("AI", "background, no network")
    autoreport.flush(10)
    assert len(out) == 2
    autoreport.configure("", "?", None)


def test_odd_characters_never_make_a_report_raise(sent):
    assert autoreport.report("AI", "bad name \udcff in a file", "detail \udcfe too", wait=True) is True
    assert autoreport.report("AI", "background \udcfd one")
    autoreport.flush(10)
    assert len(sent) == 2


def test_the_webhook_and_other_credentials_are_masked_wherever_they_appear(sent):
    autoreport.note_log(f"10:00 posting to {HOOK} failed")
    autoreport.report("x", "Authorization: Bearer abcdefghijklmnop1234 refused",
                      "{'password': 'hunter22'} --token tok123456 GOCSPX-abcdefghijk123 Cookie: sid=1234567", wait=True)
    [text] = sent
    for secret in (HOOK, "abcdefghijklmnop1234", "hunter22", "tok123456", "GOCSPX-abcdefghijk123", "sid=1234567"):
        assert secret not in text, secret


def test_ids_that_change_every_time_are_one_error():
    assert autoreport.signature("AI", "case 8f3a9c1b not found") == autoreport.signature("AI", "case 1d2e3f4a not found")
    assert autoreport.signature("AI", "case 8f3a9c1b not found") != autoreport.signature("AI", "case 8f3a9c1b sent")


def test_hints_follow_the_side_the_error_came_from():
    assert "邮箱" in autoreport.diagnose("Authentication failed", "邮箱连接")
    assert "邮箱" not in autoreport.diagnose("Authentication failed", "AI")
    assert autoreport.diagnose("Expecting value: line 1 column 1") == ""  # a proxy's HTML page, not settings.json


def test_one_machine_sends_at_most_thirty_an_hour(sent):
    results = [autoreport.report("x", f"error kind {chr(65 + i % 26)}{chr(65 + i // 26)}", wait=True) for i in range(40)]
    assert results.count(True) == autoreport.PER_HOUR and len(sent) == autoreport.PER_HOUR


def test_nothing_is_sent_from_a_dev_copy_or_without_a_webhook(monkeypatch, tmp_path):
    out = []
    monkeypatch.setattr(autoreport, "post", lambda text, timeout=15: out.append(text) or True)
    monkeypatch.setenv("KOL_DEV", "1")
    autoreport.configure(HOOK, "0.4.1", tmp_path / "a.json")
    assert not autoreport.enabled() and not autoreport.report("AI", "boom", wait=True)
    monkeypatch.delenv("KOL_DEV")
    autoreport.configure("", "0.4.1", tmp_path / "a.json")
    assert not autoreport.enabled() and not autoreport.report("AI", "boom", wait=True)
    assert out == []


def test_a_report_that_cannot_be_sent_never_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("KOL_DEV", raising=False)
    autoreport.configure("https://127.0.0.1:9/nothing-listens", "0.4.1", tmp_path / "a.json")
    assert autoreport.report("AI", "boom", wait=True) is False
    autoreport.configure("", "?", None)


def test_background_reports_go_out(sent):
    assert autoreport.report("AI", "background one")
    autoreport.flush(10)
    assert len(sent) == 1


def test_known_errors_get_a_likely_cause():
    for error, words in [("Authentication failed: provider policy rejected the connection", "密码或授权码"),
                         ("API rate limit exceeded for <ip>", "GitHub"), ("[WinError 5] Access is denied", "权限"),
                         ("ModuleNotFoundError: No module named 'httpx'", "重新运行安装命令"),
                         ("httpx.ConnectTimeout", "网络不通")]:
        assert words in autoreport.diagnose(error), error
    assert autoreport.diagnose("something nobody has seen") == ""


def test_what_one_night_sent_the_group_now_gets_a_likely_cause():
    # The reports of 2026-10-27/28 that said 没认出来
    for error, where, words in [
            ("手机审稿机器人：连不上 Telegram：ReadTimeout", "手机审稿", "网络不通"),
            ("手机审稿机器人：Bad Gateway", "手机审稿", "服务器临时出错"),
            ("总表暂时写不进去（POPO 返回错误 0：操作失败），改动已排队，稍后自动补写", "KOL 总表", "服务器临时出错"),
            ("有改动没写进总表：x@y.com: Unavailable: POPO 返回错误 500：下游服务调用失败, HTTP -1", "KOL 总表",
             "服务器临时出错"),
            ("AI 账号的用量到上限了：…（原始信息：You've hit your monthly spend limit · raise it at "
             "https://claude.ai/settings/usage）", "AI", "调高上限")]:
        assert words in autoreport.diagnose(error, where), error


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def watch(clock, values_by_minute):
    """Feed the watcher one sample a minute, as autoreport_loop does; returns the fields reported at each sample."""
    w = autoreport.StateWatcher({"mobile_error": "手机审稿", "sheet_error": "KOL 总表", "scan_error": "收信检查"},
                                clock=clock)
    out = []
    for values in values_by_minute:
        out.append(w.check(values))
        clock.now += 60
    return w, out


def test_errors_the_app_is_already_retrying_are_reported_only_when_they_last(sent):
    clock = Clock()
    blip = {"mobile_error": "手机审稿机器人：连不上 Telegram：ConnectError（自动重试中）"}
    # a five-minute outage, then Telegram answers for good: nothing to tell
    w, out = watch(clock, [blip] * 5 + [{}] * 5)
    assert not any(out) and sent == []
    # an outage that flaps (a minute up now and then) for half an hour is one outage
    w, out = watch(clock, ([blip] * 4 + [{}]) * 6 + [blip])
    assert [n for n, fields in enumerate(out) if fields] == [30]
    # more of the same cause while it lasts is not news; another cause is
    gateway = {"mobile_error": "手机审稿机器人：Bad Gateway（自动重试中）"}
    assert w.check({"mobile_error": "手机审稿机器人：连不上 Telegram：ReadTimeout（自动重试中）"}) == []
    assert w.check(gateway) == ["mobile_error"]
    assert w.check({"mobile_error": "手机审稿推送失败：Forbidden: bot was blocked by the user"}) == ["mobile_error"]
    autoreport.flush(10)
    assert "ConnectError（自动重试中）（已经持续 30 分钟）" in sent[0] and "网络不通" in sent[0]
    assert "服务器临时出错" in sent[1] and len(sent) == 3


def test_errors_a_person_has_to_fix_go_at_once_even_when_the_change_is_queued(sent):
    clock = Clock()
    for error in ["总表暂时写不进去（POPO 授权过期或还没确认：在终端运行 popo-cli auth login，并在 POPO 里确认授权卡片），"
                  "改动已排队，稍后自动补写",
                  "总表暂时写不进去（总表在 Excel 里是以只读方式打开的，存不进去。请关掉后正常打开），改动已排队，稍后自动补写",
                  "写总表失败，稍后重试：Traceback (most recent call last): KeyError: 'name'",
                  "有改动没写进总表：x@y.com: KeyError: 'name'"]:
        w = autoreport.StateWatcher({"sheet_error": "KOL 总表"}, clock=clock)
        assert w.check({"sheet_error": error}) == ["sheet_error"], error
    autoreport.flush(10)
    assert "popo-cli auth login" in sent[0].split("可能原因：")[1].split("\n")[0]  # not "AI 没登录"
    # a server hiccup behind the queued change waits; one mailbox reconnecting does not hide another one's password
    hiccup = "总表暂时写不进去（POPO 返回错误 500：下游服务调用失败, HTTP -1），改动已排队，稍后自动补写"
    assert autoreport.self_healing(hiccup)
    assert autoreport.self_healing("邮件后端 180 秒没有响应，正在重连")
    assert not autoreport.self_healing("邮件后端 180 秒没有响应，正在重连；Lynn：LOGIN failed")


def test_an_error_from_before_the_computer_slept_has_not_lasted(sent):
    clock = Clock()
    blip = {"mobile_error": "手机审稿机器人：连不上 Telegram：ConnectError（自动重试中）"}
    w, out = watch(clock, [blip] * 3)
    clock.now += 8 * 3600  # the lid was closed overnight with the error on the status bar
    assert w.check(blip) == []  # the network is not up yet a moment after waking: not an eight-hour outage


def test_a_long_report_is_cut_but_keeps_its_version(sent):
    autoreport.report("AI", "boom", "x" * 10000, wait=True)
    [text] = sent
    assert len(text) <= autoreport.MAX_CHARS and text.endswith("(版本 0.4.1)")


def test_the_status_watcher_reports_each_new_error_once(sent):
    w = autoreport.StateWatcher({"claude_error": "AI", "sheet_error": "KOL 总表"})
    assert w.check({"claude_error": "CodeMaker 没登录", "sheet_error": None}) == ["claude_error"]
    assert w.check({"claude_error": "CodeMaker 没登录", "sheet_error": None}) == []  # still showing: not again
    assert w.check({"claude_error": None, "sheet_error": "Excel 正忙"}) == ["sheet_error"]
    autoreport.flush(10)
    assert len(sent) == 2


def test_the_status_watcher_tries_again_what_could_not_go_out(sent, monkeypatch):
    w = autoreport.StateWatcher({"claude_error": "AI"})
    monkeypatch.setattr(autoreport, "PER_HOUR", 0)  # the hourly limit is reached
    assert w.check({"claude_error": "CodeMaker 没登录"}) == []
    monkeypatch.setattr(autoreport, "PER_HOUR", 30)
    assert w.check({"claude_error": "CodeMaker 没登录"}) == ["claude_error"]


def test_an_app_that_exits_with_an_error_is_reported_with_its_traceback(monkeypatch, tmp_path):
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "VERSION").write_text("0.4.1\n", encoding="utf-8")
    (app / "data" / "settings.json").write_text(json.dumps({
        "feedback": {"popo_webhook": HOOK}, "identity": {"name": "Me"}, "mail": {"address": "me@example.com"}}),
        encoding="utf-8")
    log = tmp_path / "server.log"
    log.write_text("starting\nTraceback (most recent call last):\n  File \"app.py\", line 33, in <module>\n"
                   "ModuleNotFoundError: No module named 'uvicorn'\n", encoding="utf-8")
    out = []
    monkeypatch.setattr(autoreport, "post", lambda text, timeout=15: out.append(text) or True)
    monkeypatch.delenv("KOL_DEV", raising=False)
    assert autoreport.report_exit(1, log, app)
    [text] = out
    assert "【KOL Mail 自动报错 · 程序退出】" in text and "错误：ModuleNotFoundError: No module named 'uvicorn'" in text
    assert "依赖缺失" in text and "Me <me@example.com>" in text and text.endswith("(版本 0.4.1)")
    assert not autoreport.report_exit(1, log, app)  # the runner restarts it every 15 s: once is enough
    # a settings.json broken badly enough to crash the app: the webhook the app last ran with is used
    (app / "data" / "settings.json").write_text("{ broken", encoding="utf-8")
    log.write_text("starting\nsomething went wrong at startup\n", encoding="utf-8")
    assert autoreport.report_exit(1, log, app)
    assert "KOL Mail 退出了，退出码 1：something went wrong at startup" in out[-1]
    autoreport.configure("", "?", None)


def test_the_app_reports_failed_requests_and_background_tasks(monkeypatch):
    import app
    from starlette.requests import Request
    seen = []
    monkeypatch.setattr(app.autoreport, "report", lambda where, error, detail="", exc=None, **kw: seen.append((where, error)))
    request = Request({"type": "http", "method": "POST", "path": "/api/reply/draft", "headers": [], "query_string": b""})
    resp = asyncio.run(app.handle_errors(request, ValueError("boom")))
    assert resp.status_code == 500
    resp = asyncio.run(app.handle_errors(request, app.MailError("server said no")))  # e.g. SMTP refused a send
    assert resp.status_code == 502

    async def failing():
        raise RuntimeError("sheet tool crashed")

    async def run_spawn():
        app.spawn(failing(), "sheet sync")
        await asyncio.sleep(0.05)
    asyncio.run(run_spawn())
    assert seen == [("页面操作 POST /api/reply/draft", "boom"), ("页面操作 POST /api/reply/draft", "server said no"),
                    ("后台任务 sheet sync", "sheet tool crashed")]
    assert "邮箱连接" in app.ERROR_FIELDS.values() and "AI" in app.ERROR_FIELDS.values()


# ---------------------------------------------------------------- the installers' own reports

@pytest.fixture
def robot():
    """A stand-in POPO robot on loopback (the installers accept http only there); records every message."""
    got = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))["message"])
            body = b'{"errcode": 0}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/robot", got
    server.shutdown()


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell.exe"), reason="the Windows installer")
def test_the_windows_installer_reports_a_failed_install(robot, tmp_path):
    url, got = robot
    r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "install.ps1"),
                        "-Source", str(tmp_path / "no-such-folder"), "-Dir", str(tmp_path / "kol"), "-NoService",
                        "-Feedback", url], capture_output=True, timeout=180, stdin=subprocess.DEVNULL)
    assert r.returncode != 0  # it still fails as before
    [text] = got
    assert text.startswith("【KOL Mail 自动报错 · 安装失败 Windows】") and "卡在：[2/8]" in text
    assert "no-such-folder" in text and "PowerShell" in text and text.rstrip().endswith(")")


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac installer")
def test_the_mac_installer_reports_a_failed_install(robot, tmp_path):
    url, got = robot
    r = subprocess.run(["/bin/bash", str(ROOT / "install.sh"), "--source", str(tmp_path / "no-such-folder"), "--yes",
                        "--dir", str(tmp_path / "kol"), "--no-open", "--feedback", url],
                       capture_output=True, timeout=180, stdin=subprocess.DEVNULL)
    assert r.returncode != 0
    assert len(got) == 1, (r.stdout + r.stderr).decode("utf-8", "replace")[-3000:]
    [text] = got
    assert text.startswith("【KOL Mail 自动报错 · 安装失败 Mac】") and "卡在：[2/8]" in text and "macOS" in text
