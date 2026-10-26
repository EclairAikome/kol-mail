"""Regression for 90f3f5f (v0.3.3). A colleague's install sat at "连接 AI" for 11 minutes after choosing CodeMaker and
then said CodeMaker was not found: the AI check's watchdog was a normal thread, so the "version" check's exit waited
for it, and the installer read the watchdog's line as the answer.

Matches: 检测超过 11 分钟没有结束"""

from conftest import calls


def test_version_check_answers_at_once_with_the_codemaker_it_found(ai_check, fake_codemaker):
    exe, log = fake_codemaker("1.18.31")
    code, answer, took, out = ai_check("version", exe, timeout=60)  # the bug made this 660 s
    assert took < 20, f"the version check took {took:.0f} s"
    assert code == 0, out
    assert answer is not None and answer["binary"] == str(exe), out  # not the watchdog's {"errors": ...} line
    assert answer["version"] == "1.18.31" and answer["errors"] == []
    assert calls(log) == [["--version"]]
