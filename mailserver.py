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

It also adds two tools mcp-email-server lacks, for the folder menu (app.py): kolmail_rename_mailbox (also moves a
folder under another one) and kolmail_delete_mailbox, which deletes an EMPTY folder without subfolders only (KOL Mail
moves the mails to Deleted Items first, so nothing is lost on the way). Both need the "organize" permission, like
create_mailbox, and go through the server's own account and permission checks.

Only for the pinned mcp-email-server version (requirements.txt): with another one the patch is left out, KOL Mail
sees that in its startup check (supported()) and sends plain text as before."""

from __future__ import annotations

import asyncio
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
    try:
        add_folder_tools()
    except Exception as e:  # noqa: BLE001  sending and reading must work without them (the folder menu then fails)
        print(f"kol-mail: folder tools not added: {e!r}", file=sys.stderr)
    return True


# ---------------------------------------------------------------- renaming and deleting folders

FOLDER_TIMEOUT = 120  # seconds for one rename or delete, login included
# The count in the attribute list that ends a STATUS line: * STATUS "name" (MESSAGES 3). Never inside the name,
# which a folder called "x (MESSAGES 0)" would otherwise answer for.
STATUS_ITEMS = re.compile(rb"\(([^()]*)\)\s*$")
MESSAGES = re.compile(rb"\bMESSAGES\s+(\d+)", re.IGNORECASE)


async def _folders(classic, imap) -> list:
    response = await imap.list('""', '"*"')
    classic._raise_for_imap_error(response, "LIST mailboxes")
    return classic._parse_list_responses(response[1])


def _selectable(info) -> bool:
    return not any(f.casefold() in ("\\noselect", "\\nonexistent") for f in info.flags)


def _same(a: str, b: str) -> bool:
    return a == b or a.casefold() == "inbox" == b.casefold()


async def rename_mailbox(classic, client, old: str, new: str) -> str:
    """renamed, missing (no such folder), exists (the new name is taken) or unknown (the answer was lost)."""
    imap = await client._connect_imap()
    try:
        await classic._imap_login(imap, client.email_server.user_name, client.email_server.password.get_secret_value())
        await classic._send_imap_id(imap)
        folders = await _folders(classic, imap)
        if not any(_same(f.name, old) and _selectable(f) for f in folders):
            return "missing"
        if any(_same(f.name, new) for f in folders):
            return "exists"
        client._check_mutation_authority()
        try:
            response = await imap.rename(classic._quote_mailbox(old), classic._quote_mailbox(new))
        except Exception:  # noqa: BLE001  the RENAME may have reached the server: never replayed
            return "unknown"
        status = classic._imap_effect_status(response)
        if status == "failed":
            raise RuntimeError(f"RENAME mailbox failed ({classic._imap_status(response)})")
        return "renamed" if status == "succeeded" else "unknown"
    finally:
        await classic._best_effort_imap_logout(imap)


async def delete_mailbox(classic, client, name: str) -> str:
    """deleted, missing, has_children (a subfolder is in it), not_empty (mails are in it) or unknown."""
    imap = await client._connect_imap()
    try:
        await classic._imap_login(imap, client.email_server.user_name, client.email_server.password.get_secret_value())
        await classic._send_imap_id(imap)
        folders = await _folders(classic, imap)
        mine = next((f for f in folders if _same(f.name, name) and _selectable(f)), None)
        if mine is None:
            return "missing"
        if mine.delimiter and any(f.name.startswith(name + mine.delimiter) for f in folders):
            return "has_children"
        response = await imap.status(classic._quote_mailbox(name), "(MESSAGES)")
        classic._raise_for_imap_error(response, "STATUS mailbox")
        counts = [int(m.group(1)) for line in response[1] if isinstance(line, (bytes, bytearray))
                  for items in [STATUS_ITEMS.search(bytes(line))] if items
                  for m in [MESSAGES.search(items.group(1))] if m]
        if not counts:
            raise RuntimeError("STATUS mailbox gave no message count")
        if counts[0]:
            return "not_empty"
        client._check_mutation_authority()
        try:
            response = await imap.delete(classic._quote_mailbox(name))
        except Exception:  # noqa: BLE001
            return "unknown"
        status = classic._imap_effect_status(response)
        if status == "failed":
            raise RuntimeError(f"DELETE mailbox failed ({classic._imap_status(response)})")
        return "deleted" if status == "succeeded" else "unknown"
    finally:
        await classic._best_effort_imap_logout(imap)


FOLDER_TOOLS: list = []  # registered once per process, however often patch() runs


def add_folder_tools():
    """Registers kolmail_rename_mailbox and kolmail_delete_mailbox on the server about to start."""
    if FOLDER_TOOLS:
        return
    from mcp_email_server import app as server
    from mcp_email_server.application.mutation_policy import require_mutation
    from mcp_email_server.application.mutations import CreateMailboxCommand
    from mcp_email_server.emails import classic
    from mcp_email_server.runtime import get_application_runtime

    def opened(account_name: str):
        """The account's IMAP client with the organize permission checked, as create_mailbox does it."""
        service = get_application_runtime().mutations.create_mailbox
        account = service._resolve(account_name)
        require_mutation(account.allowed_mutations, "organize")
        access = service._open(account)
        provider = access.provider
        handler = getattr(provider, "_handler", None)
        if handler is None or not hasattr(provider, "_guard"):
            raise RuntimeError("this account type cannot rename or delete folders")
        provider._guard(access.account, "organize")
        return service, access.account, handler.incoming_client

    @server.mcp.tool(
        description="KOL Mail: rename one mailbox/folder, or move it under another one by giving the full new name "
                    "with the server's hierarchy delimiter. Its subfolders and mails go along. Status: renamed, "
                    "missing, exists or unknown (never retried automatically).",
        annotations=server._IDEMPOTENT_REMOTE_MUTATION,
    )
    async def kolmail_rename_mailbox(account_name: str, mailbox: str, new_name: str) -> dict:
        CreateMailboxCommand(account_name, new_name).validate()
        if "inbox" in (mailbox.casefold(), new_name.casefold()):
            raise ValueError("INBOX cannot be renamed here")
        service, account, client = opened(account_name)
        status = await asyncio.wait_for(rename_mailbox(classic, client, mailbox, new_name), FOLDER_TIMEOUT)
        await service._invalidate(account, (mailbox, new_name))
        return {"mailbox": mailbox, "new_name": new_name, "status": status}

    @server.mcp.tool(
        description="KOL Mail: delete one EMPTY mailbox/folder that has no subfolders. Status: deleted, missing, "
                    "has_children, not_empty or unknown (never retried automatically).",
        annotations=server._DESTRUCTIVE_REMOTE_MUTATION,
    )
    async def kolmail_delete_mailbox(account_name: str, mailbox: str) -> dict:
        CreateMailboxCommand(account_name, mailbox).validate()
        if mailbox.casefold() == "inbox":
            raise ValueError("INBOX cannot be deleted")
        service, account, client = opened(account_name)
        status = await asyncio.wait_for(delete_mailbox(classic, client, mailbox), FOLDER_TIMEOUT)
        await service._invalidate(account, (mailbox,))
        return {"mailbox": mailbox, "status": status}

    FOLDER_TOOLS.extend([kolmail_rename_mailbox, kolmail_delete_mailbox])


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
