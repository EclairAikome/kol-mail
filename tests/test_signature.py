"""Replies as HTML (signature.py) and the mail they become (mailserver.py, the patch on mcp-email-server): the font,
the handwritten name in place of the typed one, the banner, the plain text that goes along, inline pictures with
Content-IDs, and the settings and pictures the page saves."""

import base64
import email
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import mailserver  # noqa: E402
import signature  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

SIG = "Eclair\nMarketing Manager | LootBar\nhttps://lootbar.com/"
TEXT = "Hi Ana,\n\nThanks! Our guide: https://lootbar.com/affiliate?a=1&b=2\n<b>not bold</b>\n\nBest,\n" + SIG


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + b"\0\0\0\0" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture
def signing(tmp_path, monkeypatch):
    s = signature.Signature(tmp_path)
    monkeypatch.setattr(app, "signing", s)
    return s


def test_the_html_keeps_the_text_escaped_with_links_in_the_chosen_font():
    s = signature.clean({"font": "Georgia"})
    out = signature.render(TEXT, SIG, s, {}, {})
    assert "font-family:Georgia" in out and "&lt;b&gt;not bold&lt;/b&gt;" in out
    assert '<a href="https://lootbar.com/affiliate?a=1&amp;b=2"' in out
    assert out.count("<p ") == 3 and "Marketing Manager | LootBar" in out
    assert "font-family:Arial" in signature.render(TEXT, SIG, signature.clean({"font": "Comic Sans"}), {}, {})


def test_the_handwritten_name_stands_for_the_typed_one_and_the_banner_closes_the_signature():
    s = signature.clean({"name_on": True, "name_text": "eclair"})
    out = signature.render(TEXT, SIG, s, {"name": "cid:name@kolmail", "banner": "cid:banner@kolmail"},
                           {"name": {"width": 90, "height": 40}, "banner": {"width": 200, "height": 43}})
    sig = out[out.index('src="cid:name@kolmail"'):]
    assert ">Eclair<" not in sig and "Marketing Manager" in sig
    assert sig.index("Marketing Manager") < sig.index('src="cid:banner@kolmail"')
    assert 'width="90" height="40" alt="eclair"' in out


def test_without_the_signature_at_the_end_the_pictures_still_come_last():
    out = signature.render("Hi\n\nbye", SIG, signature.clean({}), {"banner": "x.png"}, {"banner": None})
    assert out.index("bye") < out.index('src="x.png"')


def test_pictures_settings_and_the_team_banner(signing):
    assert signing.shown("banner")["own"] is False  # the team's banner ships with the app
    assert signing.pictures() == {"banner": signature.DEFAULT_BANNER}
    with pytest.raises(signature.SignatureError):
        signing.set_picture("name", b"GIF89a...")
    with pytest.raises(signature.SignatureError):
        signing.set_picture("logo", png(2, 2))
    signing.set_picture("name", png(180, 80))
    assert signing.pictures() == {"banner": signature.DEFAULT_BANNER}  # not switched on yet
    signing.save({"name_on": True, "name_text": "Eclair", "banner_on": False, "versions": {"name": "forged"}})
    assert list(signing.pictures()) == ["name"] and signing.load()["versions"]["name"] != "forged"
    assert signing.shown("name") == {"width": 90, "height": 40, "version": signing.load()["versions"]["name"],
                                     "own": True}
    signing.set_picture("banner", png(400, 86))
    assert signing.shown("banner")["own"]
    signing.remove_picture("banner")
    assert signing.picture("banner") == signature.DEFAULT_BANNER


def test_outgoing_carries_the_text_the_html_and_the_inline_files(signing):
    signing.set_picture("name", png(180, 80))
    signing.save({"name_on": True, "name_text": "Eclair"})
    out = signing.outgoing(TEXT, SIG)
    markup, text = mailserver.split_body(out["body"])
    assert text == TEXT and out["html"] is True and 'src="cid:name@kolmail"' in markup
    assert [Path(p).name for p in out["attachments"]] == ["kolmail-inline-name.png", "kolmail-inline-banner.png"]


def test_mail_args_falls_back_to_plain_text_without_the_patched_server(signing, monkeypatch):
    monkeypatch.setattr(app, "HTML_MAIL", False)
    assert app.mail_args("Hi", ["a.pdf"]) == {"body": "Hi", "attachments": ["a.pdf"]}
    monkeypatch.setattr(app, "HTML_MAIL", True)
    args = app.mail_args("Hi", ["a.pdf"])
    assert args["html"] is True and args["attachments"][-1] == "a.pdf" and len(args["attachments"]) == 2


# ---------------------------------------------------------------- the mail itself (mailserver.py)

@pytest.fixture
def client():
    from mcp_email_server.config import EmailServer
    from mcp_email_server.emails import classic
    assert mailserver.patch(), "mcp-email-server is not the version mailserver.py was made for"
    server = EmailServer(user_name="me@example.org", password="x", host="smtp.example.org", port=465)
    return classic.EmailClient(server, sender="Eclair <me@example.org>")


def parts(msg) -> list[tuple[str, str | None]]:
    return [(p.get_content_type(), p.get("Content-ID")) for p in msg.walk()]


def test_a_reply_with_pictures_is_related_with_its_text_alternative(client, signing, tmp_path):
    signing.set_picture("name", png(180, 80))
    signing.save({"name_on": True, "name_text": "Eclair"})
    out = signing.outgoing(TEXT, SIG)
    doc = tmp_path / "brief.pdf"
    doc.write_bytes(b"%PDF-1.4")
    msg = client.compose_message(["ana@example.org"], "Re: hi", out["body"], html=True,
                                 attachments=[*out["attachments"], str(doc)])
    assert parts(msg) == [("multipart/mixed", None), ("multipart/related", None), ("multipart/alternative", None),
                          ("text/plain", None), ("text/html", None), ("image/png", "<name@kolmail>"),
                          ("image/png", "<banner@kolmail>"), ("application/pdf", None)]
    parsed = email.message_from_bytes(msg.as_bytes())
    plain = next(p for p in parsed.walk() if p.get_content_type() == "text/plain")
    html_part = next(p for p in parsed.walk() if p.get_content_type() == "text/html")
    assert plain.get_payload(decode=True).decode() == TEXT
    assert "kolmail-text" not in html_part.get_payload(decode=True).decode()
    banner = [p for p in parsed.walk() if p.get("Content-ID") == "<banner@kolmail>"][0]
    # what Gmail writes itself: without a file name Gmail on the web shows "inline.txt" and a blank picture
    assert banner.get_content_disposition() == "inline" and banner.get_filename() == "signature-banner.png"
    assert banner.get_param("name") == "signature-banner.png" and banner["X-Attachment-Id"] == "banner@kolmail"
    related = next(p for p in parsed.walk() if p.get_content_type() == "multipart/related")
    assert related.get_param("type") == "multipart/alternative"
    assert mailserver.is_signature_picture("signature-banner.png") and not mailserver.is_signature_picture("brief.pdf")
    assert not mailserver.is_signature_picture({"name": "signature-banner.png"})


def test_links_end_before_cjk_punctuation_and_indents_survive():
    out = signature.render("詳細は https://lootbar.com/aff?a=1。よろしく\n  - one\n    - two", "", signature.clean({}), {}, {})
    assert 'href="https://lootbar.com/aff?a=1"' in out and "。よろしく" in out
    assert "&nbsp;&nbsp;- one" in out and "&nbsp;&nbsp;&nbsp;&nbsp;- two" in out


def test_html_without_pictures_still_gets_its_text_version(client):
    msg = client.compose_message(["ana@example.org"], "Re: hi", mailserver.text_mark("Hi") + "<p>Hi</p>", html=True)
    assert parts(msg) == [("multipart/alternative", None), ("text/plain", None), ("text/html", None)]
    assert msg["Subject"] == "Re: hi"


def test_plain_mails_and_ordinary_attachments_are_untouched(client, tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("x")
    assert parts(client.compose_message(["a@example.org"], "s", "plain body")) == [("text/plain", None)]
    msg = client.compose_message(["a@example.org"], "s", "plain body", attachments=[str(doc)])
    assert parts(msg) == [("multipart/mixed", None), ("text/plain", None), ("text/plain", None)]


def test_a_broken_text_mark_is_dropped_not_sent():
    mark = "<!--kolmail-text:" + base64.b64encode(b"\xff\xfe").decode() + "-->"
    assert mailserver.split_body(mark + "<p>x</p>") == ("<p>x</p>", None)


# ---------------------------------------------------------------- the page's API

def local(**extra):
    return {"X-KOL-Mail": "1", "Origin": f"http://127.0.0.1:{app.PORT}", **extra}


def test_the_settings_tab_saves_uploads_and_previews(signing, monkeypatch):
    monkeypatch.setattr(app, "LOGGED_OUT", False)
    c = TestClient(app.app, base_url=f"http://127.0.0.1:{app.PORT}")
    d = c.post("/api/signature", headers=local(), json={"font": "Verdana", "name_text": "Eclair"}).json()
    assert d["font"] == "Verdana" and "Arial" in d["fonts"] and d["pictures"]["banner"]["own"] is False
    r = c.post("/api/signature/picture?kind=name", headers=local(**{"Content-Type": "image/png"}), content=png(180, 80))
    assert r.json()["pictures"]["name"]["width"] == 90
    assert c.post("/api/signature/picture?kind=name", headers=local(), content=b"<svg/>").status_code == 400
    preview = c.post("/api/signature/preview", headers=local(), json={"name_on": True}).json()["html"]
    assert "/signature/name.png?v=" in preview and "font-family:Verdana" in preview
    assert c.get("/signature/name.png").content == png(180, 80)
    assert c.get("/signature/name.png", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.get("/signature/logo.png").status_code == 404
