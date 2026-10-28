"""The folder menu's rules: which folders may be renamed, moved or deleted, and what a new name becomes on the server.

A folder's server name is its whole path, the levels joined by the server's hierarchy delimiter ("/" on WorkMail):
"Partners/Asia" is the folder Asia inside Partners. Renaming keeps a folder where it is and changes its last level;
moving keeps its last level and puts it under another parent (or at the top). Subfolders and mails go along.

Locked: INBOX, the special folders (Sent, Drafts, Junk, Deleted Items, and any other the server marks as special,
such as Gmail's All Mail), the hidden leftovers, and the three deal folders. Those names are fixed in KOL Mail
(app.DEAL_BOXES) and in the owner's daily triage task, which files closed deals into them: renamed, both would stop
finding them. Only pure functions here; app.py talks to the server."""

from __future__ import annotations

LEAF_LIMIT = 100  # characters of one level; servers take more, nobody types more


def leaf(name: str, delim: str) -> str:
    return name.rsplit(delim, 1)[-1] if delim else name


def parent(name: str, delim: str) -> str:
    return name.rsplit(delim, 1)[0] if delim and delim in name else ""


def join(parent_name: str, leaf_name: str, delim: str) -> str:
    return f"{parent_name}{delim}{leaf_name}" if parent_name else leaf_name


def inside(name: str, folder: str, delim: str) -> bool:
    """Whether name is the folder itself or somewhere below it."""
    return name == folder or bool(delim) and name.startswith(folder + delim)


def locked(name: str, locked_names) -> bool:
    return name.casefold() == "inbox" or name in locked_names


def leaf_problem(text: str, delim: str) -> str | None:
    """Why a typed folder name cannot be used, or None."""
    if not text:
        return "名字不能是空的"
    if len(text) > LEAF_LIMIT:
        return f"名字太长了，最多 {LEAF_LIMIT} 个字"
    if delim and delim in text:
        return f"名字里不能有 {delim}（它在邮箱里表示上下级）"
    if any(c in text for c in "*%\"\\") or any(ord(c) < 32 for c in text):
        return "名字里不能有 * % \" \\ 这些符号"
    if text.casefold() == "inbox":
        return "这个名字留给收件箱了"
    return None


def rename_target(name: str, new_leaf: str, delim: str) -> str:
    return join(parent(name, delim), new_leaf, delim)


def move_target(name: str, new_parent: str, delim: str) -> str:
    return join(new_parent, leaf(name, delim), delim)


def move_problem(name: str, new_parent: str, delim: str, names) -> str | None:
    """Why the folder cannot go under new_parent ("" = the top level), or None."""
    if new_parent and not delim:
        return "这个邮箱不支持文件夹分层"
    if new_parent and new_parent not in names:
        return "目标文件夹不存在了，刷新一下再试"
    if new_parent and inside(new_parent, name, delim):
        return "不能把文件夹放进它自己或它的子文件夹里"
    if new_parent == parent(name, delim):
        return "已经在这个位置了"
    return None


def renamed(name: str, old: str, new: str, delim: str) -> str:
    """Where a folder named name ends up when old becomes new (subfolders follow)."""
    if name == old:
        return new
    if delim and name.startswith(old + delim):
        return new + name[len(old):]
    return name


def custom(names, locked_names, hidden) -> tuple[str, ...]:
    """The folders the owner made: where mails can be filed besides INBOX and the deal folders."""
    return tuple(n for n in names if not locked(n, locked_names) and n not in hidden)
