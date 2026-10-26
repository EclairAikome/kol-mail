"""Regression for e6fd8ab (v0.3.1). A colleague's install froze for good after "各试一次": the CodeMaker calls had
succeeded, but llm.py deleted CodeMaker's session in a fire-and-forget asyncio task, and asyncio.run cancelling that
task while it waited on its subprocess hangs on Python 3.12 for Windows. The hang itself only shows on 3.12 Windows;
the task left in the loop shows on every Python, so that is checked too."""

import asyncio
import sys

import pytest

from conftest import ROOT, calls

sys.path.insert(0, str(ROOT))
import llm  # noqa: E402


def test_the_smoke_check_finishes_and_deletes_the_session(ai_check, fake_codemaker):
    exe, log = fake_codemaker("1.18.31", delete_seconds=3)  # the clean-up is still running when each call returns
    code, answer, took, out = ai_check("smoke", exe, timeout=120)
    assert code == 0 and answer is not None and answer["errors"] == [], out
    assert took < 60, f"the smoke check took {took:.0f} s"
    made = calls(log)
    assert sum(c[:1] == ["run"] for c in made) == 2  # one call per role: translate, draft
    # the clean-up is a detached process: give the two of them a moment to log themselves
    import time
    for _ in range(50):
        if sum(c[:2] == ["session", "delete"] for c in calls(log)) == 2:
            break
        time.sleep(0.2)
    assert [c for c in calls(log) if c[:2] == ["session", "delete"]] == [["session", "delete", "ses_fake1"]] * 2


def test_a_codemaker_call_leaves_nothing_running_in_the_event_loop(fake_codemaker, tmp_path):
    exe, _ = fake_codemaker("1.18.31", delete_seconds=3)
    left = {}

    async def call():
        result = await llm._codemaker(str(exe), "gpt-5.6-terra", "SYSTEM", "ping",
                                      {"type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"]},
                                      tmp_path, 60)
        left["tasks"] = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in left["tasks"]:  # do not let this test hang on 3.12 Windows the way the installer did
            t.cancel()
        return result

    llm._codemaker_ids.clear()
    assert asyncio.run(call()) == {"ok": "ok"}
    assert left["tasks"] == [], "a task is left for asyncio.run to cancel: that hangs on Python 3.12 for Windows"


@pytest.mark.skipif(not (sys.platform == "win32" and sys.version_info[:2] == (3, 12)),
                    reason="the original hang only happens on Python 3.12 for Windows")
def test_on_python_312_windows_the_check_really_ends(ai_check, fake_codemaker):
    exe, _ = fake_codemaker("1.18.31", delete_seconds=5)
    code, answer, took, out = ai_check("smoke", exe, timeout=90)
    assert code == 0 and answer["errors"] == [], out
