"""The draft flow of app.py gets the KOL's materials: email attachments through checked_attachment, Telegram photos
and documents through telegram-mcp's inspect_document, a longer history, and the owner told in needs_approval_zh
about whatever nobody could read."""

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest
from mcp.types import ImageContent, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import materials as mt  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8000  # above SMALL_IMAGE, like a real screenshot


@pytest.fixture
def reader(tmp_path, monkeypatch):
    """app's gatherer with the AI reading faked and no network."""
    shown = []

    async def visual(path):
        shown.append(path)
        return {"what_zh": "数据截图", "transcript": "Avg views 12,345"}

    async def clean(name, path):
        return None, ""
    g = mt.Gatherer(mt.Cache(tmp_path / "m.sqlite3"), visual, clean, tmp_path,
                    client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    monkeypatch.setattr(app, "gatherer", g)
    monkeypatch.setattr(app, "attachment_verdict", clean)
    return shown


def test_email_attachments_are_read_through_the_attachment_check(tmp_path, monkeypatch, reader):
    img = tmp_path / "file.png"
    img.write_bytes(PNG)
    asked = []

    async def checked(box, email_id, name):
        asked.append((box, email_id, name))
        if name == "setup.exe":
            return None, ".exe 文件不在允许的类型里（只收 PDF、图片、文本、Word/Excel/PowerPoint、zip、视频），请让对方换成 PDF 或图片", ""
        return img, None, "已通过检查和杀毒扫描。"
    monkeypatch.setattr(app, "checked_attachment", checked)
    mails = [{"mailbox": "INBOX", "email_id": "12", "date": "2026-10-22T08:00:00Z", "attachments": ["stats.png"],
              "body": "Stats attached.\n\n> On Monday Me wrote:\n> https://bit.ly/ours"},
             {"mailbox": "纯CPS", "email_id": "7", "date": "2026-10-20T08:00:00Z", "attachments": ["setup.exe"],
              "body": "my channel https://bit.ly/abc"}]
    found = asyncio.run(app.email_materials(mails))
    assert asked == [("INBOX", "12", "stats.png"), ("纯CPS", "7", "setup.exe")]
    assert [(m["what"], m["status"]) for m in found] == [("附件「stats.png」", mt.READ), ("附件「setup.exe」", mt.UNREAD),
                                                         ("链接 https://bit.ly/abc", mt.SUSPICIOUS)]
    assert found[0]["source"] == "最新这封邮件" and found[0]["text"] == "数据截图\nAvg views 12,345"
    assert found[1]["source"] == "10-20 的邮件" and "没通过安全检查" in found[1]["reason"]
    assert reader == [img]  # the quoted link (our own) is not theirs


def test_telegram_photos_and_documents_are_read_through_inspect_document(monkeypatch, reader):
    calls = []

    async def fake_call(tool, timeout=180, raw=False, **args):
        if tool == "get_media_info":  # telegram-mcp answers with the Telethon repr of the media
            mime = "application/pdf" if args["message_id"] == 12 else "text/plain"  # 12: a PDF named prices.txt
            return {"result": "MessageMediaDocument(document=Document(id=1, mime_type='%s', "
                              "size=%d, thumbs=[PhotoSize(type='m', size=900)]))"
                              % (mime, 20_000_000 if args["message_id"] == 10 else 4321)}
        calls.append((tool, raw, args))
        if args["message_id"] == 5:
            return [ImageContent(type="image", data=base64.b64encode(PNG).decode(), mimeType="image/png")]
        return [TextContent(type="text", text="Price 400 USD")]
    monkeypatch.setattr(app.tg, "call", fake_call)
    msgs = [{"id": 5, "date": "2026-10-22T10:00:00+00:00", "media": "photo", "text": "my stats"},
            {"id": 6, "date": "2026-10-22T10:01:00+00:00", "media": "document: kit.pdf"},
            {"id": 7, "date": "2026-10-22T10:02:00+00:00", "media": "document: scan.pdf"},
            {"id": 8, "date": "2026-10-22T10:03:00+00:00", "media": "document: tool.exe"},
            {"id": 9, "date": "2026-10-22T10:04:00+00:00", "text": "and twitch.tv/gamerx_live",
             "web_preview": {"url": "https://bit.ly/x"}},
            {"id": 10, "date": "2026-10-22T10:05:00+00:00", "media": "document: huge.txt"},
            {"id": 11, "date": "2026-10-22T10:06:00+00:00", "media": "document: deck.docx"},
            {"id": 12, "date": "2026-10-22T10:07:00+00:00", "media": "document: prices.txt"},
            {"id": 13, "date": "2026-10-22T10:08:00+00:00", "media": "document: rates.txt"}]
    found = asyncio.run(app.telegram_materials(4242, msgs))
    by_what = {m["what"]: m for m in found}
    assert by_what["照片「photo.jpg」"]["status"] == mt.READ and "Avg views 12,345" in by_what["照片「photo.jpg」"]["text"]
    for pdf in ("kit.pdf", "scan.pdf", "prices.txt"):  # never handed to telegram-mcp's pypdf, whatever the name
        assert by_what[f"文件「{pdf}」"]["status"] == mt.UNREAD and by_what[f"文件「{pdf}」"]["reason"] == app.TG_PDF
    assert by_what["文件「rates.txt」"]["status"] == mt.READ and by_what["文件「rates.txt」"]["text"] == "Price 400 USD"
    assert by_what["文件「tool.exe」"]["status"] == mt.UNREAD and ".exe" in by_what["文件「tool.exe」"]["reason"]
    assert by_what["链接 https://bit.ly/x"]["status"] == mt.SUSPICIOUS
    assert "超过 10 MB，没下载" in by_what["文件「huge.txt」"]["reason"]
    assert "Telegram 里的 .docx 文件不读" in by_what["文件「deck.docx」"]["reason"]
    assert by_what["Twitch 频道 https://www.twitch.tv/gamerx_live"]["status"] == mt.UNREAD  # 404 from the fake
    assert all(tool == "inspect_document" and raw and args["chat_id"] == 4242 for tool, raw, args in calls)
    assert sorted(a["message_id"] for _, _, a in calls) == [5, 13]  # the .exe and the PDFs are never downloaded


def test_telegram_case_keeps_media_and_the_longer_history(monkeypatch):
    stored = {}
    monkeypatch.setattr(app.cases, "for_ref", lambda ref: None)
    monkeypatch.setattr(app.cases, "count_since", lambda *a: 0)
    monkeypatch.setattr(app.cases, "open_case", lambda *a, **k: 99)
    monkeypatch.setattr(app.cases, "set_kv", lambda k, v: stored.setdefault(k, v))
    monkeypatch.setattr(app.case_queue, "put_nowait", lambda x: None)
    msgs = [{"id": i, "date": f"d{i}", "text": f"m{i}"} for i in range(1, 40)]
    msgs.append({"id": 40, "date": "d40", "text": "stats", "media": "photo", "web_preview": {"url": "https://x.io"}})
    msgs.append({"id": 41, "date": "d41", "text": "ok", "out": True})
    app.open_telegram_case(4242, "Ana", msgs, 40, None, "tg:4242:40")
    import json
    ctx = json.loads(stored["ctx:99"])
    assert ctx["msg"]["body"] == "[d40] stats [attached: photo] [link preview: https://x.io]"
    assert ctx["earlier"][0]["body"].count("\n") == 38  # all 39 older messages of theirs
    assert [m["id"] for m in ctx["tg_msgs"]] == list(range(1, 41))
    assert app.TG_HISTORY >= 50


def test_draft_tells_the_owner_about_unread_materials(monkeypatch):
    seen = {}

    async def fake_claude(system, user_text, schema, model, timeout, files=None):
        seen["user"] = user_text
        return {"reply_language": "English", "reply_body": "Thanks!\n\n" + app.SIGNATURE, "notes_zh": "",
                "needs_approval_zh": "无"}
    monkeypatch.setattr(app, "run_claude", fake_claude)
    monkeypatch.setattr(app, "read_file", lambda path, what: "playbook")
    ctx = {"channel": "email", "msg": {"date": "d", "sender": "a@b.c", "subject": "Hi", "body": "kit attached",
                                       "attachments": ["kit.pdf"]},
           "earlier": [], "ours": [],
           "materials": [mt.material("最新这封邮件", "附件 kit.pdf", mt.UNREAD, reason="杀毒扫描超时")]}
    draft = asyncio.run(app.write_draft(ctx, notes="reply"))
    assert draft["needs_approval_zh"] == "- 对方发过附件 kit.pdf（最新这封邮件），没读到：杀毒扫描超时"
    assert "Attachments: kit.pdf" in seen["user"] and "KOL_MATERIAL>>\nSource: 最新这封邮件" in seen["user"]


def text_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with a text layer (Helvetica), or none when lines is empty (like a scan)."""
    stream = "BT /F1 18 Tf 50 750 Td " + " ".join(f"({t}) Tj 0 -30 Td" for t in lines) + " ET" if lines else ""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def test_codemaker_reads_a_pdf_from_its_text_layer_without_an_ai_call(monkeypatch, tmp_path):
    monkeypatch.setattr(app.AI, "backend", "codemaker")

    async def boom(*a, **k):
        raise AssertionError("no AI call")
    monkeypatch.setattr(app, "run_claude", boom)
    kit = tmp_path / "file.pdf"
    kit.write_bytes(text_pdf(["Media Kit - GamerX", "Price: 600 USD per integration"]))
    got = asyncio.run(app.read_visual(kit))
    assert "Price: 600 USD per integration" in got["transcript"] and got["transcript"].startswith("[page 1]")
    scan = tmp_path / "scan" / "file.pdf"
    scan.parent.mkdir()
    scan.write_bytes(text_pdf([]))
    with pytest.raises(mt.Unreadable, match="扫描版 PDF"):
        asyncio.run(app.read_visual(scan))


def test_claude_still_looks_at_pdfs_itself(monkeypatch, tmp_path):
    monkeypatch.setattr(app.AI, "backend", "claude")
    seen = []

    async def fake_claude(system, user_text, schema, model, timeout, files=None):
        seen.append(files)
        return {"what_zh": "media kit", "transcript": "x"}
    monkeypatch.setattr(app, "run_claude", fake_claude)
    kit = tmp_path / "file.pdf"
    kit.write_bytes(text_pdf(["a"]))
    asyncio.run(app.read_visual(kit))
    assert seen == [[kit]]


def test_a_telegram_error_never_passes_for_a_text_files_content(monkeypatch, reader):
    async def fake_call(tool, timeout=180, raw=False, **args):
        if tool == "get_media_info":
            return {"result": "MessageMediaDocument(document=Document(mime_type='text/plain', size=120))"}
        return [TextContent(type="text", text="Rate limit exceeded (FloodWait): wait 30 seconds")]
    monkeypatch.setattr(app.tg, "call", fake_call)
    found = asyncio.run(app.telegram_materials(1, [{"id": 3, "date": "d", "media": "document: prices.txt"}]))
    assert found[0]["status"] == mt.UNREAD and "Rate limit" in found[0]["reason"]


def test_a_telegram_image_is_typed_by_its_bytes(monkeypatch, tmp_path):
    seen = []

    async def verdict(name, path):
        seen.append((name, path.suffix))
        return None, ""
    monkeypatch.setattr(app, "attachment_verdict", verdict)
    got = asyncio.run(app.telegram_image(b"\xff\xd8\xff" + b"\x00" * 9000))
    assert got.path.suffix == ".jpg" and seen == [("photo.jpg", ".jpg")]
    assert asyncio.run(app.telegram_image(b"MZ" + b"\x00" * 100)).refusal == "文件内容不是图片"
