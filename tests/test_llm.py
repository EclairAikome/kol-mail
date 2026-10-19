"""Model choice per backend, and AI errors turned into something a marketing colleague can act on."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402

LISTING = """openai/gpt-5.6-terra
codemaker/GPT-6.1-Sol
openai/gpt-6.1-sol
anthropic/claude-sonnet-4
"""


def test_codemaker_defaults_to_the_gpt_models():
    assert llm.Settings(backend="codemaker").models == {"translate": "gpt-5.6-terra", "draft": "gpt-6.1-sol"}
    assert llm.Settings(backend="claude").models == {"translate": "claude-sonnet-5-5", "draft": "claude-opus-5-5"}


def test_model_names_match_loosely_and_prefer_codemakers_own_provider():
    assert llm.match_models(LISTING, "gpt6.1 sol") == ["codemaker/GPT-6.1-Sol", "openai/gpt-6.1-sol"]
    assert llm.match_models(LISTING, "gpt-5.6-terra") == ["openai/gpt-5.6-terra"]
    assert llm.match_models(LISTING, "gpt-6.1") == []  # a prefix is a different model, never a match
    assert llm.match_models(LISTING, "claude-opus-5-5") == []
    noisy = "anthropic/gpt-56-terra\n- openai/gpt-5.6-terra\nUsage: x/gpt-5.6-terra"
    assert llm.match_models(noisy, "gpt-5.6-terra") == []


def test_ordinary_errors_are_not_mistaken_for_a_logout():
    for raw in ("dialog input was empty", "catalog in use", "at cli.js:401:12", "the model parameter is invalid"):
        assert llm.explain(raw, "claude") == raw, raw


def test_an_old_codemaker_is_not_told_to_run_claude_update():
    msg = llm.explain("version 3.0 or newer is required", "codemaker")
    assert "CodeMaker" in msg and "claude update" not in msg


def test_logged_out_errors_say_how_to_log_in():
    msg = llm.explain("Failed to authenticate. API Error: 401 OAuth access token is invalid.", "claude")
    assert "claude" in msg and "/login" in msg and "401 OAuth" in msg
    assert llm.unavailable(msg)
    cm = llm.explain("Error: not logged in, run codemaker auth login", "codemaker")
    assert "codemaker auth login" in cm and llm.unavailable(cm)


def test_an_old_claude_code_is_told_to_update():
    raw = ("API Error: 400 Claude Code 2.1.152 does not support this model; version 2.1.280 or newer is required. "
           "Run 'claude update', or update the Claude desktop app, then try again.")
    msg = llm.explain(raw, "claude")
    assert msg.startswith("Claude Code 版本太旧") and "claude update" in msg
    assert llm.unavailable(msg)


def test_an_old_claude_code_counts_as_unavailable():
    import pytest
    with pytest.raises(llm.LLMUnavailable, match="版本太旧"):
        llm._fail("Claude Code 2.1.152 does not support this model; version 2.1.280 or newer is required.", "claude")
    with pytest.raises(llm.LLMError) as e:
        llm._fail("AI 没有按格式返回结果", "claude")
    assert not isinstance(e.value, llm.LLMUnavailable)


def test_other_errors_pass_through_unchanged():
    assert llm.explain("AI 没有按格式返回结果", "claude") == "AI 没有按格式返回结果"
    assert llm.explain("", "codemaker") == ""
