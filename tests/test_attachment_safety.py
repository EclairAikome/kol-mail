"""Received attachments: an Office file is never passed with XML left unchecked, the copies kept under ATTACH_DIR
are swept a week after their last use, and a Telegram file whose type cannot be told is not downloaded."""

import asyncio
import json
import os
import struct
import sys
import time
import zipfile
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import materials as mt  # noqa: E402

CONTENT_TYPES = b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DDE = (f'<w:document {W}><w:fldSimple w:instr=" DDEAUTO c:\\\\windows\\\\system32\\\\cmd.exe "/>'
       '</w:document>').encode()
HELLO = f"<w:document {W}>hi</w:document>".encode()


@pytest.fixture(autouse=True)
def fresh_sweep_clock(monkeypatch):
    """attachment_verdict may sweep; the time of the last sweep must not carry from one test to another."""
    monkeypatch.setattr(app, "attach_swept", 0.0)


def docx(path: Path, parts: dict[str, bytes], content_types: bytes = CONTENT_TYPES) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        for name, data in parts.items():
            z.writestr(name, data)
    return path


def test_a_clean_office_file_passes(tmp_path):
    assert app.inspect_zip(docx(tmp_path / "a.docx", {"word/document.xml": HELLO}),
                           office=True) is None


def test_dde_is_found_in_a_part_within_the_budget(tmp_path):
    assert "DDE" in app.inspect_zip(docx(tmp_path / "a.docx", {"word/document.xml": DDE}), office=True)


def test_a_part_too_big_to_scan_is_refused_not_skipped(tmp_path, monkeypatch):
    """Before: padding used up most of the budget, the DDE part no longer fit in what was left and was skipped,
    and the file passed as clean."""
    monkeypatch.setattr(app, "OOXML_SCAN_LIMIT", 10_000)
    path = docx(tmp_path / "a.docx", {"word/padding.xml": b"<x>" + b" " * 9_000 + b"</x>",
                                      "word/document.xml": DDE + b" " * 2_000})
    assert "内容太大" in app.inspect_zip(path, office=True)
    single = docx(tmp_path / "b.docx", {"word/document.xml": DDE + b" " * 20_000})
    assert "内容太大" in app.inspect_zip(single, office=True)


def test_parts_that_together_exceed_the_budget_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OOXML_SCAN_LIMIT", 10_000)
    parts = {f"word/p{i}.xml": b"<x>" + b" " * 4_000 + b"</x>" for i in range(3)}
    assert "内容太大" in app.inspect_zip(docx(tmp_path / "a.docx", parts), office=True)


def lie_about_size(path: Path, part: str, size: int, keep_crc: bool):
    """Rewrite one part's uncompressed size in the central directory, and its CRC to match the first size bytes."""
    data = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as z:
        info = z.getinfo(part)
        content = z.read(info)
    central = next(i for i in range(len(data)) if data[i:i + 4] == b"PK"
                   and data[i + 46:i + 46 + len(info.filename)] == info.filename.encode())
    if keep_crc:
        data[central + 16:central + 20] = struct.pack("<I", zlib.crc32(content[:size]))
    data[central + 24:central + 28] = struct.pack("<I", size)
    path.write_bytes(bytes(data))


def test_a_part_that_understates_its_size_is_refused(tmp_path):
    """zipfile would return the first 100 bytes without complaint (CRC of those bytes), and the DDE after them
    would go unseen."""
    path = docx(tmp_path / "a.docx", {"word/document.xml": b"<x>" + b" " * 20_000 + DDE + b"</x>"})
    lie_about_size(path, "word/document.xml", 100, keep_crc=True)
    with zipfile.ZipFile(path) as z:
        assert len(z.read("word/document.xml")) == 100  # what the old check scanned
    assert "大小和内容对不上" in app.inspect_zip(path, office=True)


def test_a_deflate_bomb_behind_a_small_size_is_refused_without_inflating_it(tmp_path):
    bomb = b"<x>" + b" " * 50_000_000 + b"</x>"  # packs to about 50 KB
    path = docx(tmp_path / "a.docx", {"word/document.xml": bomb})
    del bomb
    lie_about_size(path, "word/document.xml", 10, keep_crc=True)
    assert "大小和内容对不上" in app.inspect_zip(path, office=True)


def test_a_part_packed_with_bzip2_is_refused(tmp_path):
    """bzip2 and lzma have no output limit in zipfile; Office never writes them."""
    path = tmp_path / "a.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("word/document.xml", DDE, compress_type=zipfile.ZIP_BZIP2)
    assert "压缩方式" in app.inspect_zip(path, office=True)


def test_a_stored_part_is_still_scanned(tmp_path):
    path = tmp_path / "a.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("word/document.xml", DDE)
    assert "DDE" in app.inspect_zip(path, office=True)


def test_old_verdicts_are_checked_again_under_the_new_policy(tmp_path, monkeypatch):
    path = tmp_path / "f" / "file.docx"
    path.parent.mkdir()
    docx(path, {"word/document.xml": HELLO})
    (path.parent / "clean.json").write_text(json.dumps({"sha256": app.file_sha256(path), "policy": 2,
                                                         "caution": "旧结论", "noscan": app.MACOS}), encoding="utf-8")
    checked = []

    def inspect(p, ext):
        checked.append(p)
        return None, ""

    async def no_threats(p):
        return None
    monkeypatch.setattr(app, "inspect_content", inspect)
    monkeypatch.setattr(app, "defender_scan", no_threats)
    monkeypatch.setattr(app, "ATTACH_DIR", tmp_path)
    refused, caution = asyncio.run(app.attachment_verdict("deck.docx", path))
    assert refused is None and checked == [path] and caution != "旧结论"
    assert json.loads((path.parent / "clean.json").read_text(encoding="utf-8"))["policy"] == app.POLICY_VERSION


# ---------------------------------------------------------------- Office XML read as XML, not as bytes
# Each of these passed as clean while the check was a set of regexes over the raw bytes.

RELS_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
TEMPLATE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate"


def rels(*relationships: str) -> bytes:
    return f"<Relationships {RELS_NS}>{''.join(relationships)}</Relationships>".encode()


def body(inner: str) -> bytes:
    return f"<w:document {W}><w:body><w:p>{inner}</w:p></w:body></w:document>".encode()


def field(*runs: str) -> str:
    """A complex field whose code is spread over one w:instrText per run."""
    code = "".join(f'<w:r><w:instrText xml:space="preserve">{r}</w:instrText></w:r>' for r in runs)
    return (f'<w:r><w:fldChar w:fldCharType="begin"/></w:r>{code}'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>')


def office(tmp_path, parts, **kw):
    return app.inspect_zip(docx(tmp_path / "a.docx", parts, **kw), office=True)


def test_external_mode_in_single_quotes_is_seen(tmp_path):
    rel = f"<Relationship Id='r1' Type='{TEMPLATE}' Target='http://example.invalid/t.dotm' TargetMode='External'/>"
    assert "外部加载" in office(tmp_path, {"word/_rels/settings.xml.rels": rels(rel)})


def test_a_gt_sign_inside_an_attribute_does_not_cut_the_relationship_short(tmp_path):
    rel = (f'<Relationship Id="a>b" Type="{TEMPLATE}" Target="http://example.invalid/t.dotm" '
           'TargetMode="External"/>')
    assert "外部加载" in office(tmp_path, {"word/_rels/settings.xml.rels": rels(rel)})


def test_a_share_path_written_as_character_references_is_seen(tmp_path):
    rel = ('<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
           'Target="&#92;&#92;host&#92;share&#92;a.png"/>')
    assert "网络共享" in office(tmp_path, {"word/_rels/document.xml.rels": rels(rel)})


def test_dde_in_a_utf16_part_is_seen(tmp_path):
    part = body('<w:fldSimple w:instr=" DDEAUTO x "/>').decode().encode("utf-16")
    assert "DDE" in office(tmp_path, {"word/document.xml": part})


def test_a_dde_field_split_over_several_runs_is_put_back_together(tmp_path):
    assert "DDE" in office(tmp_path, {"word/document.xml": body(field(" DD", "EAUTO", " x "))})
    assert "DDE" in office(tmp_path, {"word/document.xml": body(field(" ", "DDE", " x y "))})


def test_ordinary_fields_split_the_same_way_pass(tmp_path):
    assert office(tmp_path, {"word/document.xml": body(field(" PA", "GE ") + field(" HYPERLINK ", '"https://a.b" '))}) \
        is None


def test_a_part_renamed_away_from_xml_is_scanned_by_its_declared_type(tmp_path):
    main = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    override = (f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                f'<Override PartName="/word/document.dat" ContentType="{main}"/></Types>').encode()
    assert "DDE" in office(tmp_path, {"word/document.dat": DDE}, content_types=override)
    default = (f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               f'<Default Extension="dat" ContentType="{main}"/></Types>').encode()
    assert "DDE" in office(tmp_path, {"word/document.dat": DDE}, content_types=default)


def test_a_part_not_declared_as_xml_is_left_to_defender(tmp_path):
    png = (b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
           b'<Default Extension="png" ContentType="image/png"/></Types>')
    assert office(tmp_path, {"word/document.xml": HELLO, "word/media/a.png": b"\x89PNG\r\n\x1a\n\xff\xfe"},
                  content_types=png) is None


def test_an_excel_dde_link_is_seen(tmp_path):
    link = (b'<externalLink xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b'<ddeLink ddeService="a" ddeTopic="b"/></externalLink>')
    assert "DDE" in office(tmp_path, {"xl/externalLinks/externalLink1.xml": link})


def test_a_dtd_is_refused_before_any_entity_is_expanded(tmp_path):
    """Entity definitions live only in a DTD; OPC allows none in a package, so none is ever expanded."""
    part = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><d>&b;</d>'
    assert "DTD" in office(tmp_path, {"word/document.xml": part})
    assert "DTD" in office(tmp_path, {"word/document.xml": HELLO},
                           content_types=CONTENT_TYPES.replace(b"?><", b'?><!DOCTYPE Types SYSTEM "x.dtd"><'))


def test_xml_that_does_not_parse_is_refused(tmp_path):
    assert "不是正常的 XML" in office(tmp_path, {"word/document.xml": b"<w:document>&undefined;</w:document>"})


X = 'xmlns:x="urn:x"'


def test_a_same_named_attribute_in_another_namespace_does_not_hide_the_real_one(tmp_path):
    """Office reads w:instr, w:fldCharType and the plain TargetMode/Type; a later x: twin must not replace them in
    what is checked."""
    assert "DDE" in office(tmp_path, {"word/document.xml": body(
        f'<w:fldSimple {X} w:instr=" DDEAUTO x " x:instr=" PAGE "/>')})
    twin = (f'<w:r><w:fldChar {X} w:fldCharType="begin" x:fldCharType="end"/></w:r>'
            '<w:r><w:instrText> DDE x y </w:instrText></w:r>')
    assert "字段结构异常" in office(tmp_path, {"word/document.xml": body(twin)})
    rel = (f'<Relationship {X} Id="r1" Type="{TEMPLATE}" x:Type="urn:t" Target="http://example.invalid/t.dotm" '
           'TargetMode="External" x:TargetMode="Internal"/>')
    assert "外部加载" in office(tmp_path, {"word/_rels/settings.xml.rels": rels(rel)})


def test_a_percent_encoded_dot_does_not_hide_the_xml_extension(tmp_path):
    assert "DDE" in office(tmp_path, {"word/document%2Exml": DDE})


def test_dde_instruction_text_outside_a_field_code_is_checked_run_by_run(tmp_path):
    after = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> PAGE </w:instrText></w:r>'
             '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
             '<w:r><w:instrText> PAGE </w:instrText></w:r><w:r><w:instrText> DDE x y </w:instrText></w:r>'
             '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    assert "DDE" in office(tmp_path, {"word/document.xml": body(after)})


# ---------------------------------------------------------------- sweeping ATTACH_DIR

def aged(folder: Path, days: float) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "file.png").write_bytes(b"x")
    then = time.time() - days * 86400
    os.utime(folder, (then, then))
    return folder


def test_copies_unused_for_a_week_are_swept_and_recent_ones_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ATTACH_DIR", tmp_path)
    old = [aged(tmp_path / name, 8) for name in ("0" * 32, "tg-" + "1" * 32, "drive-" + "2" * 32)]
    recent = aged(tmp_path / ("3" * 32), 6)
    app.sweep_attachments(force=True)
    assert not any(f.exists() for f in old) and recent.exists()


def test_another_mailboxs_copies_are_judged_one_by_one(tmp_path, monkeypatch):
    """Another mailbox's copies sit in its account- folder, whose own date is that of its first copy."""
    monkeypatch.setattr(app, "ATTACH_DIR", tmp_path)
    root = tmp_path / "account-me_163"
    recent = aged(root / ("6" * 32), 1)
    old = aged(root / ("7" * 32), 8)
    then = time.time() - 30 * 86400
    os.utime(root, (then, then))
    app.sweep_attachments(force=True)
    assert recent.exists() and not old.exists() and root.is_dir()


def test_a_sweep_runs_at_most_every_few_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ATTACH_DIR", tmp_path)
    now = time.time()
    app.sweep_attachments(force=True, now=now)
    old = aged(tmp_path / ("4" * 32), 8)
    app.sweep_attachments(now=now + 60)
    assert old.exists()
    app.sweep_attachments(now=now + app.ATTACH_SWEEP_EVERY + 1)
    assert not old.exists()


def test_every_use_of_a_copy_counts_as_recent(tmp_path, monkeypatch):
    """A screenshot looked at every day stays, though its folder was filled more than a week ago."""
    monkeypatch.setattr(app, "ATTACH_DIR", tmp_path)
    folder = aged(tmp_path / ("5" * 32), 8)
    path = folder / "file.png"
    (folder / "clean.json").write_text(json.dumps({"sha256": app.file_sha256(path), "policy": app.POLICY_VERSION,
                                                   "caution": "ok", "noscan": app.MACOS}), encoding="utf-8")
    os.utime(folder, (time.time() - 8 * 86400,) * 2)  # writing clean.json touched it
    assert asyncio.run(app.attachment_verdict("stats.png", path)) == (None, "ok")
    app.sweep_attachments(force=True)
    assert path.exists()


def test_an_old_folder_being_downloaded_into_again_is_kept(tmp_path, monkeypatch):
    """The email attachment was swept once, or its download failed: the folder is old and empty when the next
    download starts, and a sweep during that download must leave it."""
    monkeypatch.setattr(app, "ATTACH_DIR", tmp_path)
    monkeypatch.setattr(app, "attach_swept", time.time())

    async def body(mail, box, email_id):
        return {"attachments": ["stats.png"], "message_id": "<m@x>", "body": "hi"}

    async def download(self, tool, timeout=0, **args):
        app.sweep_attachments(force=True)  # a sweep while the file is still on its way
        Path(args["save_path"]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        return {"saved_path": args["save_path"]}

    async def verdict(name, path):
        return None, ""
    monkeypatch.setattr(app, "load_body", body)
    monkeypatch.setattr(app.MailBridge, "call", download)  # on the class: undoing it leaves no copy on app.mail
    monkeypatch.setattr(app, "attachment_verdict", verdict)
    identity = json.dumps(["INBOX", "3", "<m@x>", app.body_hash("hi"), ["stats.png"], "stats.png"], ensure_ascii=False)
    folder = tmp_path / app.hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    folder.mkdir()
    os.utime(folder, (time.time() - 8 * 86400,) * 2)
    path, refused, _ = asyncio.run(app.checked_attachment("INBOX", "3", "stats.png"))
    assert refused is None and path.exists()


# ---------------------------------------------------------------- Telegram files

def test_a_pdf_named_to_look_like_text_to_us_is_not_downloaded(monkeypatch):
    """We read "kit.txt:.pdf" as .txt (an NTFS stream suffix); telegram-mcp reads it as .pdf and would parse it."""
    calls = []

    async def fake_call(tool, timeout=180, raw=False, **args):
        calls.append(tool)
        return {"result": "MessageMediaDocument(document=Document(mime_type='text/plain', size=120))"}
    monkeypatch.setattr(app.tg, "call", fake_call)
    found = asyncio.run(app.telegram_materials(1, [{"id": 3, "date": "d", "media": "document: kit.txt:.pdf"}]))
    assert found[0]["status"] == mt.UNREAD and found[0]["reason"] == app.TG_PDF and calls == []


def test_a_telegram_file_of_unknown_type_is_not_downloaded(monkeypatch):
    calls = []

    async def fake_call(tool, timeout=180, raw=False, **args):
        calls.append(tool)
        return {"result": "MessageMediaDocument(document=Document(size=120))"}
    monkeypatch.setattr(app.tg, "call", fake_call)
    found = asyncio.run(app.telegram_materials(1, [{"id": 3, "date": "d", "media": "document: prices.txt"}]))
    assert found[0]["status"] == mt.UNREAD and "查不到文件类型" in found[0]["reason"]
    assert calls == ["get_media_info"]
