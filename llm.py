"""The AI backend: one call shape (system prompt, untrusted user text, JSON schema) over two command-line agents.

- claude:    Claude Code CLI in print mode, all tools off, structured output via --json-schema.
- codemaker: CodeMaker CLI (an opencode fork), `codemaker run --format json` with an agent whose tools are all off.
             CodeMaker has no schema flag, so the schema goes into the system prompt and the JSON is parsed from the
             reply text. Models are named without their provider here; the full provider/model id is looked up once
             in `codemaker models`.

Either way the model only ever sees text and can only answer with text. Both CLIs add their own context to the user
turn (Claude Code appends reminders such as <ip_reminder>); answers are scrubbed of those tags, and callers that show
model text as a translation must still check it (see looks_chinese).
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

ROLES = ("translate", "draft")
MODELS = {
    "claude": {"translate": "claude-sonnet-5-5", "draft": "claude-opus-5-5"},
    "codemaker": {"translate": "claude-sonnet-5-5", "draft": "claude-opus-5-5"},
}
_codemaker_ids: dict[str, str] = {}  # bare model name -> provider/model, from `codemaker models`
UNAVAILABLE_MARKERS = ("log in", "login", "/login", "limit", "credit", "overloaded", "authenticat", "quota",
                       "登录", "额度")
# Context the CLIs inject into the user turn. A model that copies its input (a lazy "translation") copies these too.
INJECTED_TAG = re.compile(r"<(system-reminder|ip_reminder|user-prompt-submit-hook|env|context)\b[^>]*>.*?</\1>\s*",
                          re.DOTALL | re.IGNORECASE)
INJECTED_OPEN = re.compile(r"</?(system-reminder|ip_reminder|user-prompt-submit-hook)\b[^>]*>", re.IGNORECASE)


class LLMError(Exception):
    """This particular call failed (bad output, timeout)."""


class LLMUnavailable(LLMError):
    """The backend itself cannot run right now (missing, logged out, out of quota)."""


# ---------------------------------------------------------------- settings

class Settings:
    """Which backend and models to use. Values come from the caller's config; the environment can override them."""

    def __init__(self, backend: str = "claude", models: dict | None = None, claude_bin: str = "",
                 codemaker_bin: str = "", workdir: Path | None = None):
        self.backend = os.environ.get("KOL_LLM_BACKEND", backend) or "claude"
        if self.backend not in MODELS:
            raise ValueError(f"未知的 AI 后端：{self.backend}（只支持 claude 或 codemaker）")
        chosen = dict(MODELS[self.backend])
        chosen.update({k: v for k, v in (models or {}).items() if k in ROLES and v})
        for role in ROLES:
            env = os.environ.get(f"KOL_{role.upper()}_MODEL")
            if env:
                chosen[role] = env
        self.models = chosen
        self.claude_bin = os.environ.get("KOL_CLAUDE_BIN", claude_bin)
        self.codemaker_bin = os.environ.get("KOL_CODEMAKER_BIN", codemaker_bin)
        self.workdir = workdir or Path(tempfile.gettempdir()) / "kol-mail-llm"

    def binary(self) -> str | None:
        return find_claude(self.claude_bin) if self.backend == "claude" else find_codemaker(self.codemaker_bin)

    def label(self) -> str:
        return "Claude Code" if self.backend == "claude" else "CodeMaker"


def find_claude(configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    # The desktop app ships its own CLI; the Store (MSIX) build keeps AppData under its package folder.
    patterns = [
        os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "*", "claude.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages", "Claude_*", "LocalCache", "Roaming",
                     "Claude", "claude-code", "*", "*", "claude.exe"),
    ]
    candidates = [p for pattern in patterns for p in glob.glob(pattern)]
    return max(candidates, key=os.path.getmtime) if candidates else None


def find_codemaker(configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    on_path = shutil.which("codemaker")
    if on_path:
        return on_path
    default = Path(os.environ.get("USERPROFILE", "")) / ".codemaker" / "bin" / "codemaker.exe"
    return str(default) if default.exists() else None


# ---------------------------------------------------------------- output handling

def scrub(value):
    """Remove CLI-injected reminder blocks from every string in a model answer."""
    if isinstance(value, str):
        return INJECTED_OPEN.sub("", INJECTED_TAG.sub("", value))
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


def had_injection(value) -> bool:
    text = json.dumps(value, ensure_ascii=False)
    return bool(INJECTED_OPEN.search(text))


CJK = re.compile(r"[㐀-䶿一-鿿]")
LATIN_WORD = re.compile(r"[A-Za-zÀ-ÿĀ-žА-я]{2,}")


def looks_chinese(text: str) -> bool:
    """True when the text reads as Chinese: links, names and the signature may stay in Latin script, but most of
    the words must be Chinese. Guards against a 'translation' that is the original copied back."""
    plain = re.sub(r"https?://\S+|\S+@\S+", " ", text or "")
    han = len(CJK.findall(plain))
    words = len(LATIN_WORD.findall(plain))
    return han >= 4 and han >= words


def parse_json_text(text: str) -> dict:
    """The JSON object in a free-text answer: a bare object, one in a ``` fence, or the outermost {...}."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise LLMError("AI 没有按格式返回结果")


def check_required(obj: dict, schema: dict) -> dict:
    missing = [k for k in schema.get("required", []) if k not in obj]
    if missing:
        raise LLMError(f"AI 返回结果缺少字段：{', '.join(missing)}")
    return obj


def unavailable(message: str) -> bool:
    low = message.lower()
    return any(m in low for m in UNAVAILABLE_MARKERS)


# ---------------------------------------------------------------- the call

async def complete(settings: Settings, system_prompt: str, user_text: str, schema: dict, role: str,
                   timeout: int) -> dict:
    """Run one call and return the JSON object the schema describes, scrubbed of injected reminder blocks."""
    if role not in ROLES:
        raise ValueError(role)
    binary = settings.binary()
    if not binary:
        raise LLMUnavailable(f"找不到 {settings.label()} 命令行")
    model = settings.models[role]
    settings.workdir.mkdir(parents=True, exist_ok=True)
    if settings.backend == "claude":
        result = await _claude(binary, model, system_prompt, user_text, schema, settings.workdir, timeout)
    else:
        result = await _codemaker(binary, model, system_prompt, user_text, schema, settings.workdir, timeout)
    return check_required(scrub(result), schema)


async def _communicate(proc, data: bytes, timeout: int, label: str) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(proc.communicate(data), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise LLMError(f"{label} 超过 {timeout} 秒没有返回")
    except asyncio.CancelledError:
        proc.kill()
        raise


async def _claude(binary: str, model: str, system_prompt: str, user_text: str, schema: dict, cwd: Path,
                  timeout: int) -> dict:
    # The prompt (with the whole playbook) goes through a private file: a command line is readable by other local
    # processes and Windows caps it at 32K characters.
    prompt_file = cwd / f"prompt-{secrets.token_hex(6)}.md"
    prompt_file.write_text(system_prompt, encoding="utf-8")
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")  # mail text must never meet personal memories
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "-p",
            "--model", model,
            "--tools", "",
            "--disallowedTools", "mcp__*",
            "--strict-mcp-config",
            "--output-format", "json",
            "--json-schema", json.dumps(schema, ensure_ascii=False),
            "--system-prompt-file", str(prompt_file),
            "--setting-sources", "local",
            "--disable-slash-commands",
            "--no-session-persistence",
            cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await _communicate(proc, user_text.encode("utf-8"), timeout, "Claude Code")
    finally:
        try:
            prompt_file.unlink()
        except OSError:
            pass
    try:
        obj = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        raise LLMError((err or out).decode("utf-8", "replace")[:300] or "Claude Code 输出无法解析")
    if obj.get("is_error"):
        msg = str(obj.get("result") or obj.get("terminal_reason") or "Claude Code 调用失败")[:300]
        raise (LLMUnavailable if unavailable(msg) else LLMError)(msg)
    structured = obj.get("structured_output")
    if isinstance(structured, dict):
        return structured
    return parse_json_text(str(obj.get("result", "")))


CODEMAKER_FORMAT = """

Answer with exactly one JSON object and nothing else (no prose, no code fence). It must follow this JSON Schema:
{schema}
"""


async def codemaker_model_id(binary: str, model: str) -> str:
    """CodeMaker wants provider/model. A bare name is matched against `codemaker models` (once per name), preferring
    the provider CodeMaker logs you in with; a name that already has a provider is used as it is."""
    if "/" in model:
        return model
    if model in _codemaker_ids:
        return _codemaker_ids[model]
    proc = await asyncio.create_subprocess_exec(binary, "models", stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await _communicate(proc, b"", 120, "CodeMaker")
    ids = [line.strip() for line in out.decode("utf-8", "replace").splitlines() if line.strip().endswith("/" + model)]
    if not ids:
        msg = (err or b"").decode("utf-8", "replace").strip()[-200:]
        raise (LLMUnavailable if unavailable(msg) else LLMError)(f"CodeMaker 里没有模型 {model}" + (f"：{msg}" if msg else ""))
    ids.sort(key=lambda i: "codemaker" not in i.split("/", 1)[0])
    _codemaker_ids[model] = ids[0]
    return ids[0]


async def _codemaker(binary: str, model: str, system_prompt: str, user_text: str, schema: dict, workdir: Path,
                     timeout: int) -> dict:
    model = await codemaker_model_id(binary, model)
    # The agent (system prompt, no tools) is defined in a codemaker.json of a fresh folder per call, so parallel
    # calls never share a prompt and nothing is left in a project folder.
    run_dir = workdir / f"cm-{secrets.token_hex(6)}"
    run_dir.mkdir(parents=True)
    agent = {
        "$schema": "https://opencode.ai/config.json",
        "agent": {"kolmail": {
            "mode": "primary",
            "description": "kol-mail text task, no tools",
            "prompt": system_prompt + CODEMAKER_FORMAT.format(schema=json.dumps(schema, ensure_ascii=False)),
            "tools": {"*": False},
            "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
        }},
    }
    (run_dir / "codemaker.json").write_text(json.dumps(agent, ensure_ascii=False), encoding="utf-8")
    session_id = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "run", "--pure", "--agent", "kolmail", "-m", model, "--format", "json", "--dir", str(run_dir),
            cwd=str(run_dir),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await _communicate(proc, user_text.encode("utf-8"), timeout, "CodeMaker")
        texts, errors = [], []
        for line in out.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            session_id = session_id or event.get("sessionID")
            if event.get("type") == "text":
                texts.append(str((event.get("part") or {}).get("text") or ""))
            elif event.get("type") == "error":
                errors.append(json.dumps(event.get("error"), ensure_ascii=False))
        if errors:
            msg = errors[0][:300]
            raise (LLMUnavailable if unavailable(msg) else LLMError)(f"CodeMaker 调用失败：{msg}")
        if not texts:
            detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
            msg = detail[-1][:300] if detail else "CodeMaker 没有返回内容"
            raise (LLMUnavailable if unavailable(msg) else LLMError)(msg)
        return parse_json_text(texts[-1])
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        if session_id:  # CodeMaker keeps every session locally; mail text should not pile up there
            asyncio.ensure_future(_forget(binary, session_id))


async def _forget(binary: str, session_id: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "session", "delete", session_id, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), 60)
    except Exception:  # noqa: BLE001  housekeeping only
        pass
