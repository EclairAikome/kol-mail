"""The notice after any change of the playbook (and after an update of KOL Mail itself), and the SOP documents on
POPO that are followed through popo-cli."""

import asyncio
import json

import learnkit
from learnkit import Req

import app
import playbook_learn as pl
from sheet import popogrid


def notice():
    return json.loads(asyncio.run(app.api_playbook_notice(Req(method="GET"))).body)


def test_notice_lines_are_short_and_few():
    hunks = [{"section": "二、利润分成方案", "why_zh": "起步改报 25%，因为您连续三封都这样改了，而且对方都接受了"}] * 12
    lines = pl.notice_lines(hunks)
    assert lines[0].startswith("二、利润分成：") and all(len(line) <= pl.LINE_MAX for line in lines)
    assert len(lines) == 9 and lines[-1] == "……还有 4 处"
    assert pl.notice_title(3, "auto", ["11-05", "11-04", "11-05"]) == "手册更新了 3 处（自动 · 来自您 11-04、11-05 的修改）"


def test_a_hand_edit_is_a_change_shown_once(tmp_path, monkeypatch):
    book = learnkit.setup(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "SETUP_DONE", False)
    assert notice()["playbook"] is None
    book.write_text(book.read_text(encoding="utf-8").replace("- 用对方的语言回复。", "- 用对方的语言回复，短一点。"),
                    encoding="utf-8")
    n = notice()["playbook"]
    assert n["title"] == "手册更新了 1 处（手动修改）" and n["lines"] == ["十一、沟通风格：有改动"]
    asyncio.run(app.api_playbook_notice(Req({"ids": n["ids"]})))
    assert notice()["playbook"] is None
    old = sorted((tmp_path / "playbook_history").glob("*.md"))
    assert old and old[-1].read_text(encoding="utf-8") == learnkit.PLAYBOOK  # the version before is kept


def test_saving_my_way_of_negotiating_is_a_change_too(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    monkeypatch.setattr(app.config, "update", lambda *a, **k: {})
    monkeypatch.setattr(app, "SETUP_DONE", True)
    r = asyncio.run(app.api_setup(Req({"signature": "Me", "preferences": "先听对方说完。"})))
    assert r.status_code == 200
    assert any(c["source"] == "manual" for c in app.LEARN.unseen())


def test_the_version_notice_once_after_an_update(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "SETUP_DONE", True)
    monkeypatch.setattr(app.config, "version", lambda: "0.9.0")
    app.LEARN.put("seen_version", "0.8.7")
    v = notice()["version"]
    assert v["title"] == "KOL Mail 更新到了 v0.9.0" and any("谈判手册会跟着您学" in line for line in v["lines"])
    asyncio.run(app.api_playbook_notice(Req({"ids": [], "version": "0.9.0"})))
    assert notice()["version"] is None


def test_a_first_install_gets_no_version_notice(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "SETUP_DONE", False)
    assert notice()["version"] is None and app.LEARN.get("seen_version") == app.config.version()


def test_changelog_lines_by_version(tmp_path, monkeypatch):
    log = tmp_path / "CHANGELOG.md"
    log.write_text("# x\n\n## v0.9.1\n\n- 新的\n\n## v0.9.0\n\n- 旧的\n\n## v0.8.0\n\n- 更旧\n", encoding="utf-8")
    monkeypatch.setattr(app, "CHANGELOG", log)
    assert app.changelog_lines("0.8.7", "0.9.1") == ["新的", "旧的"]
    assert app.changelog_lines("0.9.0", "0.9.0") == []


# ---------------------------------------------------------------- SOP documents

SOP_DOC = "0123456789abcdef0123456789abcdef"
SOP_SPACE = "fedcba9876543210fedcba9876543210"
SOP_URL = f"https://docs.popo.example.com/team/pc/abcd1234/pageDetail/{SOP_DOC}"


def wrapped(inner: dict) -> bytes:
    """popo-cli's answer around the document service's {"status", "message", "data"}, as popo-cli 0.1.68 gives it."""
    out = {"ok": True, "code": "FABRIC_OK", "data": {"code": "FABRIC_OK", "data": {
        "code": "FABRIC_OK", "message": "操作成功", "data": {"status": 1, "message": "成功", "data": inner}}}}
    return json.dumps(out, ensure_ascii=False).encode("utf-8")


def popo(monkeypatch, texts):
    """A popo-cli with the two document calls the SOP read uses (answers copied from the real popo-cli 0.1.68 on
    2026-11-06): doc_get_folder_path for a team-space link, then doc_get_doc_detail, whose content is POPO's own
    HTML in one line. Each detail read answers with the next text; any other tool is not found, as in popo-cli."""
    calls = []

    def run_cli(argv, timeout, env=None):
        calls.append(argv)
        tool, args = argv[2], dict(a.split("=", 1) for a in argv[3:])
        if tool == "doc_get_folder_path" and SOP_DOC in args.get("url", ""):
            return 0, wrapped({"folderId": SOP_DOC, "teamSpaceId": SOP_SPACE}), b""
        if tool == "doc_get_doc_detail" and args.get("docId") == SOP_DOC:
            reads = [c for c in calls if c[2] == "doc_get_doc_detail"]
            text = texts[min(len(reads) - 1, len(texts) - 1)]
            return 0, wrapped({"title": "koc分销 SOP", "content": text, "type": 1}), b""
        return 0, json.dumps({"ok": False, "code": "FABRIC_TOOL_NOT_FOUND",
                              "message": f"未找到匹配的工具路由: {tool}"}, ensure_ascii=False).encode("utf-8"), b""
    monkeypatch.setattr(popogrid, "run_cli", run_cli)
    monkeypatch.setattr(app, "popo_cli", lambda: "popo-cli")
    return calls


SOP_1 = ("<doc id='a-1'><h1 id='b-2'>koc 分销 SOP</h1><doc-li id='c-3' list-type='ordered' list-level='1'>"
         "默认佣金 5%。</doc-li><doc-li id='d-4' list-type='ordered' list-level='1'>发布后付款。</doc-li>"
         "<table><tbody><tr><td><p id='e-5'>原神</p></td><td><p id='f-6'>5%</p></td></tr></tbody></table></doc>")
SOP_2 = SOP_1.replace("默认佣金 5%", "默认佣金 6%")


def test_the_sop_read_asks_popo_for_the_space_then_the_document(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    calls = popo(monkeypatch, [SOP_1])
    text = app.fetch_sop(SOP_URL, "popo-cli")
    assert [c[2] for c in calls] == ["doc_get_folder_path", "doc_get_doc_detail"]
    assert calls[1][3:] == [f"docId={SOP_DOC}", f"teamSpaceId={SOP_SPACE}"]
    assert text == "# koc 分销 SOP\n- 默认佣金 5%。\n- 发布后付款。\n| 原神 | 5%"


def test_popo_document_html_becomes_one_line_per_paragraph():
    html = ("<doc><p>a &amp;lt; b <b>粗</b><a href='https://lootbar.com/affiliate/guide'>入口</a></p>"
            "<doc-li list-level='2'>子项</doc-li><p><del>旧规则</del></p><p><img src='x' /></p>"
            "<p><attachment file-name='Steps.pdf' src='y'></attachment></p><p></p>"
            "<table><tr><td><p>x</p><p>y</p></td><td>z</td></tr></table></doc>")
    assert popogrid.doc_text(html, 1).split("\n") == [
        "a &lt; b 粗入口 (https://lootbar.com/affiliate/guide)", "- 子项", "~~旧规则~~", "[图片]", "[附件 Steps.pdf]",
        "| x y | z"]
    assert popogrid.doc_text("# 标题\r\n正文", 4) == "# 标题\n正文"
    # odd markup a later POPO might send: never an exception (sop_sync only catches PopoError), never merged lines
    assert popogrid.doc_text("<doc><doc-li list-level='²'>x</doc-li><doc-li list-level='-3'>y</doc-li></doc>") == "- x\n- y"
    assert popogrid.doc_text("<doc><p>a</p><table><tr><td/><td>b</td></tr></table><p>c</p><p>d</p></doc>") == \
        "a\n| | b\nc\nd"


def test_a_link_that_is_no_popo_document_is_said_plainly(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    monkeypatch.setitem(app.CFG, "learning", {"sop_docs": ["https://docs.popo.example.com/lingxi/abc123"]})
    popo(monkeypatch, [SOP_1])
    asyncio.run(app.sop_sync())
    assert "不是 POPO 文档的链接" in app.state["sop_error"]


def test_an_sop_that_did_not_change_costs_no_model_call(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    monkeypatch.setitem(app.CFG, "learning", {"sop_docs": [SOP_URL]})
    popo(monkeypatch, [SOP_1, SOP_1])
    asked = []

    async def model(*a, **k):
        asked.append(a)
        return {"hunks": [], "summary_zh": ""}
    monkeypatch.setattr(app, "run_claude", model)
    first = asyncio.run(app.sop_sync())
    again = asyncio.run(app.sop_sync())
    assert first["docs"][0].get("first") and again["docs"][0]["changed"] is False and asked == []


def test_a_changed_sop_only_proposes_and_never_writes(tmp_path, monkeypatch):
    book = learnkit.setup(tmp_path, monkeypatch)
    monkeypatch.setitem(app.CFG, "learning", {"sop_docs": [SOP_URL]})
    popo(monkeypatch, [SOP_1, SOP_2])
    asked = []

    async def model(system, user, schema, role, timeout, files=None):
        asked.append(user)
        return {"summary_zh": "默认佣金改 6%", "hunks": [{
            "section": "十一", "op": "insert_after", "anchor": "- 用对方的语言回复。", "new_text": "- SOP 说要更热情。",
            "kind": "rule", "touches_rules": [], "evidence_episode_ids": [], "evidence_quote": "默认佣金 6%。",
            "why_zh": "跟 SOP"}]}
    monkeypatch.setattr(app, "run_claude", model)
    asyncio.run(app.sop_sync())
    out = asyncio.run(app.sop_sync())
    # POPO sends the whole document as one line of HTML: the model must see the sentence that changed, not all of it
    assert len(asked) == 1 and "\n-- 默认佣金 5%。\n" in asked[0] and "\n+- 默认佣金 6%。\n" in asked[0]
    assert "<doc" not in asked[0] and "\n-- 发布后付款。" not in asked[0]
    assert out["docs"][0]["changed"] and book.read_text(encoding="utf-8") == learnkit.PLAYBOOK
    pending = app.LEARN.changes(("pending",))
    assert pending and pending[0]["source"] == "sop" and pending[0]["hunks"][0]["state"] == "pending"
    assert "SOP 有 2 行变化，建议改手册 1 处，待您批" == pending[0]["note"]


def test_no_popo_cli_is_said_plainly(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    monkeypatch.setitem(app.CFG, "learning", {"sop_docs": [SOP_URL]})
    monkeypatch.setattr(app, "popo_cli", lambda: "")
    out = asyncio.run(app.sop_sync())
    assert "popo-cli" in out["note"]


def test_popo_logged_out_is_said_plainly(tmp_path, monkeypatch):
    learnkit.setup(tmp_path, monkeypatch)
    monkeypatch.setitem(app.CFG, "learning", {"sop_docs": [SOP_URL]})
    monkeypatch.setattr(app, "popo_cli", lambda: "popo-cli")
    monkeypatch.setattr(popogrid, "run_cli", lambda argv, t, env=None: (
        0, json.dumps({"ok": False, "code": "FABRIC_EXPIRED"}).encode(), b""))
    asyncio.run(app.sop_sync())
    assert "popo-cli auth login" in app.state["sop_error"]
