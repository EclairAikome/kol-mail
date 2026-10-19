"""Files the owner attaches to a reply (picked, dragged in or pasted as screenshots).

The page uploads each file on its own; it is kept under data/outgoing/<random id>/<name> until the mail is sent (then
removed) or for a week at most. Only the ids go back to the page, and only ids that exist here can be attached, so
a request can never make the mail server read some other file on this machine.

Limits keep the message under what mail providers accept: base64 makes attachments a third bigger on the wire, and
most providers (Amazon WorkMail, Gmail) refuse mails over 25 MB."""

from __future__ import annotations

import re
import secrets
import shutil
import time
import unicodedata
from pathlib import Path

MAX_FILE = 15 * 1024 * 1024   # base64 plus line breaks: 15 MiB of files is about 21.5 MB on the wire
MAX_TOTAL = 15 * 1024 * 1024
MAX_FILES = 10
KEEP_SECONDS = 7 * 24 * 3600
# Never sent from here: things that run when opened. A KOL should not receive them, and a mistaken pick is likelier
# than a real need (zip them if you must).
BLOCKED = {".exe", ".com", ".bat", ".cmd", ".msi", ".msp", ".scr", ".pif", ".cpl", ".js", ".jse", ".vbs", ".vbe",
           ".wsf", ".wsh", ".ps1", ".psm1", ".hta", ".lnk", ".url", ".reg", ".jar", ".dll", ".sys", ".iso", ".img",
           ".vhd", ".vhdx", ".appx", ".msix", ".application", ".gadget", ".scf", ".inf", ".chm", ".docm", ".xlsm",
           ".pptm", ".dotm", ".xltm", ".xlam", ".ppam", ".sldm", ".xlsb", ".xll", ".msc", ".jnlp", ".ws", ".vb",
           ".apk", ".sh", ".py", ".html", ".htm", ".svg"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
ID = re.compile(r"[0-9a-f]{16}")


class AttachmentError(Exception):
    pass


def clean_name(name: str) -> str:
    """A safe file name that still reads like the original: no folders, no control or bidi characters (which can
    make "gpj.exe" look like ".jpg"), nothing Windows refuses."""
    name = unicodedata.normalize("NFC", (name or "").replace("\\", "/").split("/")[-1])
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))
    name = re.sub(r'[<>:"|?*]', "_", name).strip(" .")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name):
        name = "_" + name
    if not name:
        name = "attachment"
    stem, dot, ext = name.rpartition(".")
    if dot and len(name) > 120:
        name = stem[:110] + "." + ext[:9]
    return name[:120]


def extension(name: str) -> str:
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def add(self, name: str, data: bytes) -> dict:
        name = clean_name(name)
        ext = extension(name)
        if ext in BLOCKED:
            raise AttachmentError(f"{ext} 文件不能作为附件发出（可能会被当成程序运行），需要的话请先压缩成 zip")
        if not data:
            raise AttachmentError("文件是空的")
        if len(data) > MAX_FILE:
            raise AttachmentError(f"单个附件最大 {MAX_FILE // 1024 // 1024} MB，这个有 {len(data) / 1024 / 1024:.1f} MB")
        file_id = secrets.token_hex(8)
        folder = self.root / file_id
        folder.mkdir(parents=True)
        (folder / name).write_bytes(data)
        return {"id": file_id, "name": name, "size": len(data), "image": ext in IMAGE_TYPES}

    def path(self, file_id: str) -> Path:
        if not ID.fullmatch(file_id or ""):
            raise AttachmentError("附件编号不对")
        folder = self.root / file_id
        files = [p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
        if len(files) != 1:
            raise AttachmentError("附件已经过期或被删除，请重新添加")
        return files[0]

    def resolve(self, ids: list) -> list[Path]:
        """Absolute paths for a send, checked against the limits as a whole."""
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise AttachmentError("附件参数不对")
        ids = list(dict.fromkeys(ids))
        if len(ids) > MAX_FILES:
            raise AttachmentError(f"一封信最多 {MAX_FILES} 个附件")
        paths = [self.path(i) for i in ids]
        total = sum(p.stat().st_size for p in paths)
        if total > MAX_TOTAL:
            raise AttachmentError(f"附件加起来最多 {MAX_TOTAL // 1024 // 1024} MB，现在有 {total / 1024 / 1024:.1f} MB")
        return [p.resolve() for p in paths]

    def remove(self, file_id: str):
        if ID.fullmatch(file_id or ""):
            shutil.rmtree(self.root / file_id, ignore_errors=True)

    def cleanup(self, now: float | None = None):
        now = now or time.time()
        if not self.root.is_dir():
            return
        for folder in self.root.iterdir():
            try:
                if folder.is_dir() and now - folder.stat().st_mtime > KEEP_SECONDS:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass
