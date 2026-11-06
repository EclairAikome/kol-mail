"""Where mcp-email-server may write a downloaded attachment, and what to tell the user when it refuses.

On Windows mcp-email-server (1.13, windows_security.preflight_artifact_destination) checks the folder an attachment
is written into: besides the user, SYSTEM and Administrators nobody may be allowed to add, change or delete files in
it. The folders above it only need a trusted owner and no junction. data\\attachments\\<hash> simply inherits what
its drive gives, and on many machines the root of D: lets every signed-in user change everything (Authenticated
Users and Users: full control). Every download into the cache then failed with "Attachment destination parent is
unsafe", whatever the attachment.

So on Windows the download goes into a private folder of our own, %LOCALAPPDATA%\\kol-mail\\attach-tmp (owned by the
user, protected DACL: user, SYSTEM and Administrators only; the same descriptor mcp-email-server gives the folders it
creates itself), and is moved into the cache afterwards with move_into. That folder lies in the user profile, whose
folders the server already relies on for its own temporary results, so it does not depend on where KOL Mail is
installed. Elsewhere the cache folder is used as before.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import stat
import time
from pathlib import Path

if os.name == "nt":
    import ntsecuritycon
    import win32api
    import win32con
    import win32file
    import win32security

STAGING_PARENT = "kol-mail"  # %LOCALAPPDATA%\kol-mail already holds server.log
STAGING_NAME = "attach-tmp"
ERROR_ALREADY_EXISTS = 183
ERROR_NOT_SAME_DEVICE = 17
STALE_SECONDS = 24 * 60 * 60  # a download is moved out within minutes; older ones were left by a crash


class AttachDirError(OSError):
    """The private download folder could not be made safe. The message is for the user."""


def staging_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / STAGING_PARENT / STAGING_NAME


def safe_download_dir(cache_dir: Path) -> Path:
    """A folder mcp-email-server accepts as download destination: on Windows the private staging folder (created or
    re-secured here), elsewhere cache_dir itself. Raises AttachDirError when Windows will not let us secure it."""
    if os.name != "nt":
        return Path(cache_dir)
    root = staging_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    make_private(root)
    sweep(root)
    return root


def sweep(root: Path) -> None:
    """Remove downloads a crash left in the staging folder: mail content must not pile up in the profile."""
    cutoff = time.time() - STALE_SECONDS
    for leftover in root.glob("part-*"):
        with contextlib.suppress(OSError):
            if leftover.lstat().st_mtime < cutoff:
                leftover.unlink()


def download_target(cache_dir: Path) -> Path:
    """A fresh file name in safe_download_dir(cache_dir) to pass as save_path."""
    return safe_download_dir(cache_dir) / f"part-{secrets.token_hex(8)}"


def is_reparse_point(path: Path) -> bool:
    """A symbolic link or (on Windows) a junction or other reparse point, without following it."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)  # REPARSE_POINT


def move_into(src: Path, dst: Path) -> None:
    """Move a downloaded file into the cache, replacing what is there. The staging folder may be on another drive
    than the cache, where a rename cannot go: copy next to dst first, then rename, so dst is never half written."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    for folder in (dst.parent, dst.parent.parent):  # the cache slot and data\attachments: never a junction or link
        if is_reparse_point(folder):
            raise AttachDirError(f"附件缓存文件夹 {folder} 被换成了链接或 junction，为了安全没有放进去")
    try:
        os.replace(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV and getattr(e, "winerror", None) != ERROR_NOT_SAME_DEVICE:
            raise
    part = dst.with_name(f".{dst.name}.{secrets.token_hex(4)}.part")
    try:
        with open(src, "rb") as fin, open(part, "xb") as fout:
            while chunk := fin.read(1024 * 1024):
                fout.write(chunk)
        os.replace(part, dst)
    finally:
        with contextlib.suppress(OSError):
            part.unlink()
    with contextlib.suppress(OSError):  # dst is in place; a copy left behind is swept later
        src.unlink()


UNSAFE = re.compile(r"destination parent is unsafe", re.IGNORECASE)
UNWRITABLE = re.compile(r"Attachment destination (could not be written|could not be resolved|must be a regular file)"
                        r"|destination identity changed", re.IGNORECASE)
UNSUPPORTED = re.compile(r"cannot enforce secure destination traversal", re.IGNORECASE)


def explain(err_text: str) -> str | None:
    """One sentence for a colleague when a download failed for where it was to be saved, else None."""
    text = err_text or ""
    if UNSAFE.search(text):
        return ("附件存放文件夹的权限不安全（这台电脑上别的账户也能改动它），邮件服务为了安全拒绝把附件存进去，"
                "请把这句话截图发给管理员。")
    if UNWRITABLE.search(text):
        return "附件没能存到本机的临时文件夹（可能是磁盘满了或被杀毒软件拦住），请稍后再试；一直这样就截图发给管理员。"
    if UNSUPPORTED.search(text):
        return "这台电脑的系统不支持安全地保存附件，请到邮箱客户端里查看这个附件。"
    return None


# ---------------------------------------------------------------- Windows: the private folder

def _current_user_sid():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _private_descriptor():
    """Owner the user; protected DACL with full control, inherited by what is created inside, for the user, SYSTEM
    and Administrators only (mcp-email-server's own _private_security_attributes)."""
    user = _current_user_sid()
    acl = win32security.ACL()
    inherit = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    for sid in (user, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None),
                win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inherit, ntsecuritycon.FILE_ALL_ACCESS, sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.Initialize()
    descriptor.SetSecurityDescriptorOwner(user, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED)
    return descriptor


def _is_private(handle, user) -> bool:
    """Already what make_private would set: rewriting it while another download is under way is then avoided."""
    security = win32security.GetSecurityInfo(
        handle, win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
    owner, dacl = security.GetSecurityDescriptorOwner(), security.GetSecurityDescriptorDacl()
    control, _revision = security.GetSecurityDescriptorControl()
    if owner != user or dacl is None or not control & win32security.SE_DACL_PROTECTED:
        return False
    text = win32security.ConvertSidToStringSid
    trusted = {text(user), text(win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)),
               text(win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None))}
    return all(text(dacl.GetAce(i)[-1]) in trusted for i in range(dacl.GetAceCount()))


def make_private(path: Path) -> None:
    """Create path as a private folder, or give an existing one (never a junction or link) the private descriptor."""
    descriptor = _private_descriptor()
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    try:
        win32file.CreateDirectory(str(path), attributes)
        return
    except win32file.error as e:
        if e.winerror != ERROR_ALREADY_EXISTS:
            raise AttachDirError(f"建不了附件临时文件夹 {path}：{e.strerror}") from e
    try:
        handle = win32file.CreateFile(
            str(path),
            ntsecuritycon.FILE_READ_ATTRIBUTES | ntsecuritycon.READ_CONTROL
            | ntsecuritycon.WRITE_DAC | ntsecuritycon.WRITE_OWNER,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None, win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
    except win32file.error as e:
        raise AttachDirError(f"打不开附件临时文件夹 {path}：{e.strerror}") from e
    try:
        info = win32file.GetFileInformationByHandle(handle)
        attrs = int(info["FileAttributes"] if isinstance(info, dict) else info[0])
        if attrs & win32con.FILE_ATTRIBUTE_REPARSE_POINT or not attrs & win32con.FILE_ATTRIBUTE_DIRECTORY:
            raise AttachDirError(f"附件临时文件夹 {path} 不是普通文件夹，请删掉它后重试")
        if _is_private(handle, descriptor.GetSecurityDescriptorOwner()):
            return
        win32security.SetSecurityInfo(
            handle, win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            descriptor.GetSecurityDescriptorOwner(), None, descriptor.GetSecurityDescriptorDacl(), None)
    except win32security.error as e:
        raise AttachDirError(f"改不了附件临时文件夹 {path} 的权限：{e.strerror}") from e
    finally:
        handle.Close()
