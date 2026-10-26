"""Regression for 6b5c07e (v0.2.3). Colleagues still run CodeMaker 1.15.x, which crashes on `run --agent` with "No
context found for instance", so every installer AI check failed for them. The no-tools agent now replaces the default
agent "build" instead. Both an old and a current CodeMaker must pass the installer's check, with tools off.

Matches: No context found for instance"""

import json

import pytest

from conftest import calls


@pytest.mark.parametrize("version", ["1.15.11", "1.18.31"])
def test_the_installer_ai_check_passes_on_old_and_new_codemaker(ai_check, fake_codemaker, version):
    exe, log = fake_codemaker(version)
    code, answer, took, out = ai_check("smoke", exe, timeout=120)
    assert answer is not None, out
    assert answer["errors"] == [] and code == 0, json.dumps(answer["errors"], ensure_ascii=False)
    runs = [c for c in calls(log) if c[:1] == ["run"]]
    assert len(runs) == 2 and all("--agent" not in c for c in runs)
