"""mcp-email-server, run with one change to how it builds a mail: replies that carry pictures in their signature.

KOL Mail sends HTML replies whose signature shows a banner and a handwritten name (signature.py). mcp-email-server
1.13.1 only attaches files, so a picture would arrive as an attachment the HTML cannot show. Started as
`python mailserver.py stdio` instead of `mcp-email-server stdio` (app.mail_params), this module patches
EmailClient._create_message_with_attachments before the server starts:
- attachments named kolmail-inline-<name>.<ext> become inline parts with Content-ID <name@kolmail>, which the HTML
  shows as <img src="cid:name@kolmail">;
- an HTML body that starts with <!--kolmail-text:<base64>--> gets that text as its plain-text alternative.
The mail is then multipart/mixed( multipart/related( multipart/alternative(text, html), pictures ), attachments ),
the layout mail programs expect for a signature with pictures. Everything else (plain mails, forwards, the copy in
Sent, which is built by the same function) stays exactly as mcp-email-server makes it.

Only for the pinned mcp-email-server version (requirements.txt): with another one the patch is left out, KOL Mail
sees that in its startup check (supported()) and sends plain text as before."""

from __future__ import annotations

import base64
import binascii
import re
import sys
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

INLINE_PREFIX = "kolmail-inline-"
SHOWN_PREFIX = "signature-"  # the file name an inline picture carries in the mail (KOL Mail hides it in Sent)
CID_DOMAIN = "kolmail"
TEXT_MARK = re.compile(r"\A<!--kolmail-text:([A-Za-z0-9+/=]*)-->")
PINNED = "1.13.1"


def is_signature_picture(name) -> bool:
    """An attachment name that is one of our own signature pictures, not a file the owner attached."""
    return isinstance(name, str) and bool(re.fullmatch(re.escape(SHOWN_PREFIX) + r"(name|banner)\.(png|jpe?g)", name,
                                                       re.IGNORECASE))


def cid(name: str) -> str:
    """The Content-ID the HTML refers to for an inline file named kolmail-inline-<name>.<ext>."""
    return f"{name}@{CID_DOMAIN}"


def inline_name(path: str | Path) -> str | None:
    stem, name = Path(path).stem, Path(path).name
    return stem[len(INLINE_PREFIX):] if name.startswith(INLINE_PREFIX) and stem[len(INLINE_PREFIX):] else None


def text_mark(text: str) -> str:
    """The comment that carries the plain-text version in front of an HTML body."""
    return "<!--kolmail-text:" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "-->"


def split_body(html: str) -> tuple[str, str | None]:
    """(the HTML without the mark, the plain text it carried or None)."""
    m = TEXT_MARK.match(html or "")
    if not m:
        return html, None
    try:
        text = base64.b64decode(m.group(1), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return html[m.end():], None
    return html[m.end():], text


def build(client, body: str, html: bool, attachments: list[str] | None, extra_parts: list[Message] | None,
          original, html_to_text) -> MIMEMultipart:
    """EmailClient._create_message_with_attachments with inline pictures and a plain-text alternative."""
    inline = [p for p in attachments or [] if inline_name(p)]
    if not html or not inline:  # its text part comes from text_part, which makes a marked one an alternative
        return original(client, body, html, attachments, extra_parts)
    files = [p for p in attachments or [] if not inline_name(p)]
    markup, text = split_body(body)
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(text if text is not None else html_to_text(markup), "plain", "utf-8"))
    alternative.attach(MIMEText(markup, "html", "utf-8"))
    related = MIMEMultipart("related", type="multipart/alternative")  # RFC 2387: the type of its root part
    related.attach(alternative)
    total = 0
    for file_path in inline:
        path = client._validate_attachment(file_path)
        data = client._read_attachment(path)
        total += len(data)
        client._validate_total_attachment_bytes(total)
        subtype = path.suffix.lower().lstrip(".").replace("jpg", "jpeg") or "png"
        name = inline_name(path)
        shown = f"{SHOWN_PREFIX}{name}{path.suffix.lower()}"
        # The headers Gmail itself writes for a picture in a mail: Gmail on the web shows a part without a file
        # name as an attachment called inline.txt and leaves the picture blank (seen 2026-10-27).
        part = MIMEImage(data, _subtype=subtype, name=shown)
        part.add_header("Content-Disposition", "inline", filename=shown)
        part.add_header("Content-ID", f"<{cid(name)}>")
        part.add_header("X-Attachment-Id", cid(name))
        related.attach(part)
    if not files and not extra_parts:
        return related
    mixed = MIMEMultipart("mixed")
    mixed.attach(related)
    rest: MIMEBase | MIMEMultipart = original(client, "", False, files, extra_parts)
    for part in rest.get_payload()[1:]:  # mcp-email-server's own attachment parts, after its empty text part
        mixed.attach(part)
    if rest["Content-Transfer-Encoding"]:
        mixed["Content-Transfer-Encoding"] = rest["Content-Transfer-Encoding"]
    return mixed


def patch() -> bool:
    """Patch the installed mcp-email-server; False (nothing changed) when it is not the version this was made for."""
    import importlib.metadata

    from mcp_email_server.emails import classic

    if not supported():
        return False
    original = classic.EmailClient._create_message_with_attachments
    if getattr(original, "kolmail", False):
        return True

    def create(self, body, html, attachments, extra_parts=None):
        return build(self, body, html, attachments, extra_parts, original, classic._html_to_text)

    create.kolmail = True
    classic.EmailClient._create_message_with_attachments = create
    classic.MIMEText = text_part  # compose_message builds a mail without attachments from it directly
    return True


def text_part(body, subtype="plain", charset=None, *, policy=None):
    """MIMEText, except that a marked HTML body becomes multipart/alternative with its plain text first."""
    if subtype == "html" and isinstance(body, str):
        markup, text = split_body(body)
        if text is not None:
            alternative = MIMEMultipart("alternative")
            alternative.attach(MIMEText(text, "plain", charset))
            alternative.attach(MIMEText(markup, "html", charset))
            return alternative
    return MIMEText(body, subtype, charset, policy=policy)


def supported() -> bool:
    """Whether pictures can go inline with this installation (checked by app.py without starting a server)."""
    try:
        import importlib.metadata
        return importlib.metadata.version("mcp-email-server") == PINNED
    except importlib.metadata.PackageNotFoundError:
        return False


def main():
    if sys.path and Path(sys.path[0] or ".").resolve() == Path(__file__).resolve().parent:
        sys.path.pop(0)  # KOL Mail's own modules (config, app, ...) must not stand in for the server's imports
    patch()
    from mcp_email_server.cli import app
    sys.argv = ["mcp-email-server", *sys.argv[1:]]
    app()


if __name__ == "__main__":
    main()
