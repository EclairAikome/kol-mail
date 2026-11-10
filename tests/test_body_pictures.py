"""0.10: pictures inside the text of a reply. The text stays plain, with a line "[image: N]" where picture N goes;
the mail that leaves shows the picture there (inline, like the signature's pictures), its text version names the
file, and nothing goes out when the text and the pictures do not agree."""

import asyncio
import email
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import mailserver  # noqa: E402
import outgoing  # noqa: E402
import replyflow  # noqa: E402
import signature  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

SIG = "Jordan\nMarketing Manager | LootBar\nhttps://lootbar.com/"
TEXT = "Hi Ana,\n\nHere is the dashboard:\n\n[image: 1]\n\nAnd the payout page:\n[image: 2]\nLet me know.\n\nBest,\n" + SIG


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + b"\0\0\0\0" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


GIF = b"GIF89a" + struct.pack("<HH", 320, 200) + b"\0" * 20
JPEG = (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9
        + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 480, 1280) + b"\0" * 12)


@pytest.fixture
def signing(tmp_path, monkeypatch):
    s = signature.Signature(tmp_path / "sig")
    monkeypatch.setattr(app, "signing", s)
    return s


@pytest.fixture
def box(tmp_path, monkeypatch):
    store = outgoing.Store(tmp_path / "outgoing")
    monkeypatch.setattr(app, "outbox", store)
    monkeypatch.setattr(app, "sending_ids", set())
    return store


# ---------------------------------------------------------------- the text

def test_a_picture_line_is_a_line_of_its_own():
    assert signature.picture_labels(TEXT) == ["1", "2"]
    assert signature.picture_labels("see [image: 1] here\n[image: 2] x\n[image:3]\n  [image: 4]\t\n[image: 123]\n[Image: 5]") == ["3", "4"]
    assert signature.picture_labels("") == [] and signature.picture_labels(None) == []
    assert signature.only_pictures("[image: 1]\n[image: 2]") and not signature.only_pictures("x\n[image: 1]")
    assert not signature.only_pictures("") and not signature.only_pictures("\n\n")


def test_a_paragraph_of_pictures_needs_no_translation_and_a_lost_line_is_noticed():
    assert not replyflow.needs_translation("[image: 1]", SIG)
    assert not replyflow.needs_translation("[image: 1]\n[image: 2]", SIG)
    assert replyflow.needs_translation("And the payout page:\n[image: 2]\nLet me know.", SIG)
    assert replyflow.lost_pictures(TEXT, TEXT.replace("[image: 2]\n", "")) == ["2"]
    assert replyflow.lost_pictures(TEXT, TEXT.replace("[image: 1]", "[imagen: 1]")) == ["1"]  # reworded is lost
    assert replyflow.lost_pictures(TEXT, TEXT) == [] and replyflow.lost_pictures("no pictures", "x") == []
    for prompt in (replyflow.draft_system("Me", "BD", "C", "b", "pb", "Me"), replyflow.compose_system("Me", "BD", "C", "b", "pb", "Me"),
                   replyflow.chat_system("Me", "BD", "C", "b", "pb", "Me"), replyflow.ZH_EDIT_SYSTEM,
                   replyflow.BACKTRANSLATE_SYSTEM):
        assert "[image: 1]" in prompt  # every prompt that rewrites or reads a draft is told what the line is


def test_picture_widths_are_read_from_png_gif_and_jpeg_headers():
    assert signature.picture_width(png(1920, 10)) == 1920
    assert signature.picture_width(GIF) == 320
    assert signature.picture_width(JPEG) == 1280
    assert signature.picture_width(b"not a picture") is None and signature.picture_width(b"\xff\xd8\xff") is None
    assert signature.is_picture("a.PNG", png(1, 1)) and signature.is_picture("a.jpeg", JPEG) and signature.is_picture("a.gif", GIF)
    assert not signature.is_picture("a.png", JPEG)       # a JPEG named .png
    assert not signature.is_picture("a.webp", b"RIFFxxxxWEBP")  # mail programs show WebP badly: attach it instead
    assert not signature.is_picture("a.svg", b"<svg") and not signature.is_picture("png", png(1, 1))


def test_the_html_shows_each_picture_where_its_line_stands():
    placed = {"1": {"src": "cid:body1@kolmail", "alt": 'dash"board.png', "width": 600},
              "2": {"src": "cid:body2@kolmail", "alt": "", "width": None}}
    markup = signature.render(TEXT, SIG, signature.clean({"banner_on": False}), {}, {}, placed)
    assert "[image:" not in markup
    first, second = markup.index("cid:body1@kolmail"), markup.index("cid:body2@kolmail")
    assert markup.index("Here is the dashboard:") < first < markup.index("And the payout page:") < second < markup.index("Let me know.")
    assert 'width="600" alt="dash&quot;board.png"' in markup and "max-width:100%" in markup
    assert markup.count("<img ") == 2 and markup.index("Let me know.") < markup.index("Best,")
    # a line whose picture is not there stays the text it is (the send refuses that before it gets here)
    assert "[image: 2]" in signature.render(TEXT, SIG, signature.clean({"banner_on": False}), {}, {}, {"1": placed["1"]})
    assert signature.render(TEXT, SIG, signature.clean({}), {}, {}) == signature.render(TEXT, SIG, signature.clean({}), {}, {}, {})
    assert signature.text_version(TEXT, {"1": "dashboard.png", "2": "payout.png"}).count("[image: dashboard.png]") == 1


# ---------------------------------------------------------------- the mail (the patched mcp-email-server)

@pytest.fixture
def client():
    from mcp_email_server.config import EmailServer
    from mcp_email_server.emails import classic
    assert mailserver.patch(), "mcp-email-server is not the version mailserver.py was made for"
    server = EmailServer(user_name="me@example.org", password="x", host="smtp.example.org", port=465)
    return classic.EmailClient(server, sender="Jordan <me@example.org>")


def test_the_mail_carries_the_pictures_inline_and_the_file_as_an_attachment(client, signing, box, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "HTML_MAIL", True)
    monkeypatch.setattr(app, "SIGNATURE", SIG)
    one = box.add("dashboard.png", png(1920, 40))
    two = box.add("payout.gif", GIF)
    doc = box.add("brief.pdf", b"%PDF-1.4 brief")
    body = {"attachments": [doc["id"]], "inline": {"1": one["id"], "2": two["id"]}}
    attachments, placed, ids = app.reply_files(body, TEXT)
    assert [p.name for p in attachments] == ["brief.pdf"] and ids == {doc["id"], one["id"], two["id"]}
    pictures, folder = app.placed_pictures(placed)
    try:
        args = app.mail_args(TEXT, [str(p) for p in attachments], pictures)
        msg = client.compose_message(["ana@example.org"], "Re: hi", args["body"], html=True, attachments=args["attachments"])
    finally:
        import shutil
        shutil.rmtree(folder)
    kinds = [(p.get_content_type(), p.get("Content-ID")) for p in msg.walk()]
    assert kinds == [("multipart/mixed", None), ("multipart/related", None), ("multipart/alternative", None),
                     ("text/plain", None), ("text/html", None), ("image/png", "<banner@kolmail>"),
                     ("image/png", "<body1@kolmail>"), ("image/gif", "<body2@kolmail>"), ("application/pdf", None)]
    parsed = email.message_from_bytes(msg.as_bytes())
    plain = next(p for p in parsed.walk() if p.get_content_type() == "text/plain").get_payload(decode=True).decode()
    markup = next(p for p in parsed.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    assert "[image: dashboard.png]" in plain and "[image: payout.gif]" in plain and "[image: 1]" not in plain
    assert 'src="cid:body1@kolmail" width="600"' in markup and 'src="cid:body2@kolmail" width="320"' in markup
    first = next(p for p in parsed.walk() if p.get("Content-ID") == "<body1@kolmail>")
    assert first.get_content_disposition() == "inline" and first.get_filename() == "image1.png"
    assert not mailserver.is_signature_picture("image1.png")  # shown in the conversation as what was sent
    assert not folder.exists() and box.path(one["id"]).name == "dashboard.png"  # the upload itself is untouched


def test_without_the_patched_server_the_pictures_go_as_attachments(signing, box, monkeypatch):
    monkeypatch.setattr(app, "HTML_MAIL", False)
    one = box.add("dashboard.png", png(10, 10))
    _, placed, _ = app.reply_files({"inline": {"1": one["id"]}}, "Hi\n\n[image: 1]\n\nBye")
    pictures, folder = app.placed_pictures(placed)
    args = app.mail_args("Hi\n\n[image: 1]\n\nBye", ["x.pdf"], pictures)
    assert args == {"body": "Hi\n\n[image: dashboard.png]\n\nBye", "attachments": ["x.pdf", str(placed["1"])]}
    assert app.mail_args("Hi", ["a.pdf"]) == {"body": "Hi", "attachments": ["a.pdf"]}  # as before without pictures


# ---------------------------------------------------------------- text and pictures must agree

def test_nothing_is_sent_unless_every_line_has_its_picture_and_every_picture_its_line(box):
    one, two = box.add("a.png", png(4, 4)), box.add("b.png", png(4, 4))
    doc = box.add("brief.pdf", b"%PDF-1.4")

    def refused(body, text):
        with pytest.raises(outgoing.AttachmentError) as e:
            app.reply_files(body, text)
        return str(e.value)

    assert "[image: 2] 没有对应的图片" in refused({"inline": {"1": one["id"]}}, TEXT)       # a line left from an old draft
    assert "图片 2 没有放进正文" in refused({"inline": {"1": one["id"], "2": two["id"]}}, "Hi\n\n[image: 1]")
    assert "不止一次" in refused({"inline": {"1": one["id"]}}, "[image: 1]\n\n[image: 1]")
    assert "既是附件又是正文图片" in refused({"attachments": [one["id"]], "inline": {"1": one["id"]}}, "[image: 1]")
    assert "不是 PNG、JPG 或 GIF" in refused({"inline": {"1": doc["id"]}}, "[image: 1]")
    assert "附件参数不对" in refused({"inline": {"x": one["id"]}}, "[image: 1]")
    assert "附件参数不对" in refused({"inline": [one["id"]]}, "[image: 1]")
    assert "附件已经过期" in refused({"inline": {"1": "0" * 16}}, "[image: 1]")
    many = {str(i): one["id"] for i in range(1, signature.MAX_BODY_PICTURES + 2)}
    assert "最多放" in refused({"inline": many}, "x")
    assert "[image: 7] 没有对应的图片" in refused({}, "Hello\n\n[image: 7]")              # the model wrote one on its own
    assert app.reply_files({}, "no pictures at all") == ([], {}, set())
    assert app.reply_files({"attachments": [doc["id"]]}, "see [image: 1] inline text")[1] == {}  # words, not a line


def test_the_upload_takes_only_real_pictures_for_the_text(box, monkeypatch):
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    http = TestClient(app.app, base_url=f"http://127.0.0.1:{app.PORT}")
    head = {"X-KOL-Mail": "1", "Origin": f"http://127.0.0.1:{app.PORT}", "Content-Type": "application/octet-stream"}
    ok = http.post("/api/reply/upload", content=png(8, 8), headers={**head, "X-File-Name": "shot.png", "X-Inline": "1"})
    assert ok.status_code == 200 and ok.json()["inline"] is True and box.path(ok.json()["id"]).name == "shot.png"
    for name, data in (("kit.pdf", b"%PDF-1.4"), ("fake.png", b"MZ\x90\0 not a picture"), ("a.webp", b"RIFFxxxxWEBP"),
                       ("a.svg", b"<svg/>")):
        r = http.post("/api/reply/upload", content=data, headers={**head, "X-File-Name": name, "X-Inline": "1"})
        assert r.status_code == 400 and "PNG、JPG 或 GIF" in r.json()["error"], name
    plain = http.post("/api/reply/upload", content=b"%PDF-1.4", headers={**head, "X-File-Name": "kit.pdf"})
    assert plain.status_code == 200 and plain.json()["inline"] is False  # an attachment as before


def test_the_preview_marks_where_the_page_puts_its_own_copy_of_each_picture(signing, monkeypatch):
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    monkeypatch.setattr(app, "SIGNATURE", SIG)
    http = TestClient(app.app, base_url=f"http://127.0.0.1:{app.PORT}")
    head = {"X-KOL-Mail": "1", "Origin": f"http://127.0.0.1:{app.PORT}"}
    r = http.post("/api/reply/preview", headers=head, json={"body": TEXT, "names": {"1": "dash.png", "x": "bad"}}).json()
    assert 'src="kolmail-body:1"' in r["html"] and 'alt="dash.png"' in r["html"]
    assert "[image: 2]" in r["html"] and "kolmail-body:x" not in r["html"]
    assert "<script" not in http.post("/api/reply/preview", headers=head,
                                      json={"body": "<script>alert(1)</script>", "names": {}}).json()["html"]


def test_a_rewrite_that_drops_a_picture_line_says_so(tmp_path, monkeypatch):
    import learnkit
    learnkit.setup(tmp_path, monkeypatch)
    previous = "Hi!\n\n[image: 1]\n\nThanks.\n\n" + app.SIGNATURE

    async def model(system, user, schema, role, timeout, files=None):
        return {"reply_language": "English", "reply_body": "Hi!\n\nThanks a lot.\n\n" + app.SIGNATURE,
                "notes_zh": "n", "needs_approval_zh": "无"}
    monkeypatch.setattr(app, "run_claude", model)
    ctx = {"channel": "email", "msg": {"date": "d", "sender": "a@example.com", "subject": "Hi", "body": "Rates?"},
           "earlier": [], "ours": [], "materials": []}
    draft = asyncio.run(app.write_draft(ctx, previous=previous, feedback="更热情一点"))
    assert any("没有 [image: 1] 这一行了" in w for w in draft["warnings"])
    first = asyncio.run(app.write_draft(ctx, notes="问报价"))
    assert not any("[image:" in w for w in first["warnings"])
