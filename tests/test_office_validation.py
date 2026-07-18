# -*- coding: utf-8 -*-
"""Regression tests for Office source validation and encrypted-package handling.

Covers PF-002 (an encrypted OOXML must reach the password prompt, not be
rejected first), PF-019 (family-accurate OOXML validation), PF-038 (OLE
directory parsing instead of a bounded byte scan), PF-020 (folder mode
validates exactly like manual mode) and PF-048 (end-to-end through the real
prompt/job-building path, not helpers in isolation).
"""

import struct
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402

CONTENT_TYPES = {
    "word": (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
        'package/2006/content-types"><Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/></Types>'
    ),
    "excel": (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
        'package/2006/content-types"><Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/></Types>'
    ),
    "powerpoint": (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
        'package/2006/content-types"><Override PartName="/ppt/presentation.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'presentationml.presentation.main+xml"/></Types>'
    ),
}
MAIN_PART = {
    "word": "word/document.xml",
    "excel": "xl/workbook.xml",
    "powerpoint": "ppt/presentation.xml",
}


def make_ooxml(path: Path, family: str, extra=None) -> Path:
    """A minimal but structurally valid OOXML package for ``family``."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES[family])
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr(MAIN_PART[family], "<main/>")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


def make_encrypted_ooxml(path: Path, pad_mib: int = 0) -> Path:
    """A real OLE2 container holding EncryptionInfo + EncryptedPackage.

    ``pad_mib`` inflates the payload so the directory lands far past any fixed
    scan window - the PF-038 case.
    """
    import olefile  # noqa: F401  (ensures the parser is installed)

    payload = b"\x00" * (pad_mib * 1024 * 1024) if pad_mib else b"secret"
    _write_minimal_ole(path, {
        "EncryptionInfo": b"\x04\x00\x04\x00" + b"\x00" * 60,
        "EncryptedPackage": payload,
    })
    return path


def _write_minimal_ole(path: Path, streams: dict) -> None:
    """Write a valid OLE2/CFB file containing ``streams``.

    Hand-built so the tests need no Microsoft Office to produce a fixture.
    Layout is deliberately ``[header][FAT sectors][stream data][directory]`` -
    the directory sits *after* the payload, so a large stream pushes the
    directory far past any fixed scan window. That is exactly the PF-038 case:
    detection must parse the directory, not scan the first N bytes.
    """
    SECTOR = 512
    ENTRIES_PER_FAT = SECTOR // 4
    ENDOFCHAIN, FATSECT = 0xFFFFFFFE, 0xFFFFFFFD

    entries = list(streams.items())
    data_sectors = [blob + b"\x00" * (-len(blob) % SECTOR) for _n, blob in entries]
    data_sector_counts = [len(d) // SECTOR for d in data_sectors]

    def dir_entry(name, entry_type, start_sector, size, child=-1, left=-1, right=-1):
        raw = bytearray(b"\x00" * 128)
        encoded = name.encode("utf-16-le") + b"\x00\x00"
        raw[0:len(encoded)] = encoded
        struct.pack_into("<H", raw, 64, len(encoded))
        raw[66] = entry_type          # 1=storage, 2=stream, 5=root
        raw[67] = 1                   # black
        struct.pack_into("<i", raw, 68, left)
        struct.pack_into("<i", raw, 72, right)
        struct.pack_into("<i", raw, 76, child)
        struct.pack_into("<I", raw, 116, start_sector & 0xFFFFFFFF)
        struct.pack_into("<Q", raw, 120, size)
        return raw

    directory = bytearray()
    directory += dir_entry("Root Entry", 5, ENDOFCHAIN, 0, child=1)
    # Stream entries are chained through `right` so all of them are reachable.
    data_total = sum(data_sector_counts)
    dir_sector_count = max(
        1, ((len(entries) + 1) * 128 + SECTOR - 1) // SECTOR
    )
    # Total sectors decides how many FAT sectors are needed; solve iteratively.
    fat_sector_count = 1
    while True:
        total = data_total + dir_sector_count + fat_sector_count
        needed = max(1, (total + ENTRIES_PER_FAT - 1) // ENTRIES_PER_FAT)
        if needed == fat_sector_count:
            break
        fat_sector_count = needed

    first_data = fat_sector_count            # data starts right after the FAT
    cursor = first_data
    for index, ((name, blob), count) in enumerate(zip(entries, data_sector_counts)):
        right = index + 2 if index + 1 < len(entries) else -1
        directory += dir_entry(name, 2, cursor, len(blob), right=right)
        cursor += count
    directory += b"\x00" * (-len(directory) % SECTOR)
    dir_first_sector = cursor                # directory lives after the payload

    fat = bytearray(b"\xff" * (SECTOR * fat_sector_count))
    for i in range(fat_sector_count):        # the FAT sectors describe themselves
        struct.pack_into("<I", fat, 4 * i, FATSECT)
    pos = first_data
    for count in data_sector_counts:
        for step in range(count):
            nxt = ENDOFCHAIN if step == count - 1 else pos + step + 1
            struct.pack_into("<I", fat, 4 * (pos + step), nxt)
        pos += count
    for step in range(dir_sector_count):
        nxt = ENDOFCHAIN if step == dir_sector_count - 1 else dir_first_sector + step + 1
        struct.pack_into("<I", fat, 4 * (dir_first_sector + step), nxt)

    header = bytearray(b"\x00" * SECTOR)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 24, 0x003E)
    struct.pack_into("<H", header, 26, 0x0003)   # major version 3
    struct.pack_into("<H", header, 28, 0xFFFE)   # little endian
    struct.pack_into("<H", header, 30, 9)        # 512-byte sectors
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 44, fat_sector_count)
    struct.pack_into("<I", header, 48, dir_first_sector)
    struct.pack_into("<I", header, 60, ENDOFCHAIN)   # no mini FAT
    struct.pack_into("<I", header, 64, ENDOFCHAIN)
    struct.pack_into("<I", header, 68, ENDOFCHAIN)   # no DIFAT extension
    struct.pack_into("<I", header, 72, 0)
    for i in range(fat_sector_count):                # DIFAT entries in header
        struct.pack_into("<I", header, 76 + 4 * i, i)

    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(fat)
        for padded in data_sectors:
            handle.write(padded)
        handle.write(directory)


# --------------------------------------------------------------------------- #
# Fixture sanity: our hand-built OLE really is parseable
# --------------------------------------------------------------------------- #

def test_fixture_is_a_real_ole_container(tmp_path):
    import olefile

    path = make_encrypted_ooxml(tmp_path / "enc.docx")
    assert olefile.isOleFile(str(path))
    with olefile.OleFileIO(str(path)) as ole:
        names = {"/".join(e) for e in ole.listdir(streams=True)}
    assert {"EncryptionInfo", "EncryptedPackage"} <= names


# --------------------------------------------------------------------------- #
# PF-002 - encrypted OOXML must be accepted so the password can be requested
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ext", [".docx", ".xlsx", ".pptx"])
def test_encrypted_ooxml_is_accepted_for_password_handling(tmp_path, ext):
    path = make_encrypted_ooxml(tmp_path / ("enc" + ext))
    ok, reason = app.validate_office_file(path)
    assert ok, f"encrypted {ext} rejected before any password prompt: {reason}"
    assert app.is_encrypted_office_file(path) is True


def test_encrypted_ooxml_survives_the_real_job_builder(tmp_path):
    """PF-002/PF-048 end-to-end: the public builder must keep the locked file."""
    path = make_encrypted_ooxml(tmp_path / "locked.docx")
    jobs = app.ops_office._build_jobs([path])
    accepted = list(jobs.accepted) if hasattr(jobs, "accepted") else jobs
    assert len(accepted) == 1, "the encrypted file must reach conversion"
    assert Path(accepted[0]["src"]).name == "locked.docx"


def test_encrypted_ooxml_reaches_the_manual_prompt(tmp_path, monkeypatch):
    """The manual picker must accept it (it is only locked, not invalid)."""
    path = make_encrypted_ooxml(tmp_path / "locked.docx")
    answers = iter([str(path), "done"])
    monkeypatch.setattr(app.ops_office, "_input", lambda _p: next(answers))
    chosen = app.ops_office.prompt_office_source_files()
    assert [p.name for p in chosen] == ["locked.docx"]


# --------------------------------------------------------------------------- #
# PF-038 - directory parsing, not a bounded byte scan
# --------------------------------------------------------------------------- #

def test_encrypted_detection_works_past_one_mib(tmp_path):
    path = make_encrypted_ooxml(tmp_path / "big.docx", pad_mib=3)
    assert path.stat().st_size > (1 << 20)
    assert app.is_encrypted_office_file(path) is True


def test_marker_bytes_in_ordinary_data_are_not_a_false_positive(tmp_path):
    """A plain package containing the literal marker must not look encrypted."""
    marker = "EncryptedPackage".encode("utf-16-le")
    path = make_ooxml(tmp_path / "plain.docx", "word",
                      extra={"word/media/blob.bin": marker * 100})
    assert app.is_encrypted_office_file(path) is False
    assert app.validate_office_file(path)[0] is True


def test_malformed_ole_is_rejected(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200)
    ok, reason = app.validate_office_file(path)
    assert ok is False and reason


# --------------------------------------------------------------------------- #
# PF-019 - family-accurate OOXML validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("family,ext", [
    ("word", ".docx"), ("excel", ".xlsx"), ("powerpoint", ".pptx"),
])
def test_real_minimal_fixtures_are_accepted(tmp_path, family, ext):
    path = make_ooxml(tmp_path / ("ok" + ext), family)
    ok, reason = app.validate_office_file(path)
    assert ok, reason


def test_content_types_only_zip_is_rejected(tmp_path):
    path = tmp_path / "fake.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    ok, reason = app.validate_office_file(path)
    assert ok is False and "content type" in reason.lower()


def test_xlsx_renamed_to_docx_is_rejected_as_wrong_family(tmp_path):
    path = make_ooxml(tmp_path / "sheet.docx", "excel")
    ok, reason = app.validate_office_file(path)
    assert ok is False
    assert "excel" in reason.lower()


def test_pptx_renamed_to_xlsx_is_rejected(tmp_path):
    path = make_ooxml(tmp_path / "deck.xlsx", "powerpoint")
    ok, reason = app.validate_office_file(path)
    assert ok is False and "powerpoint" in reason.lower()


def test_arbitrary_ole2_renamed_docx_is_rejected(tmp_path):
    """A legacy .doc renamed .docx is OLE2 but not an encrypted package."""
    path = tmp_path / "legacy.docx"
    _write_minimal_ole(path, {"WordDocument": b"legacy body"})
    ok, reason = app.validate_office_file(path)
    assert ok is False and "not an encrypted OOXML package" in reason


def test_path_traversal_member_is_rejected(tmp_path):
    path = tmp_path / "eve.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES["word"])
        zf.writestr("word/document.xml", "<main/>")
        zf.writestr("../../escape.txt", "nope")
    ok, reason = app.validate_office_file(path)
    assert ok is False and "unsafe entry path" in reason


def test_zip_bomb_ratio_is_rejected(tmp_path):
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES["word"])
        zf.writestr("word/document.xml", "<main/>")
        zf.writestr("word/bomb.bin", b"\x00" * (40 * 1024 * 1024))
    ok, reason = app.validate_office_file(path)
    assert ok is False and "compression ratio" in reason


def test_corrupt_zip_is_rejected(tmp_path):
    good = make_ooxml(tmp_path / "good.docx", "word")
    raw = bytearray(good.read_bytes())
    raw[len(raw) // 2:] = b"\x00" * (len(raw) - len(raw) // 2)
    bad = tmp_path / "corrupt.docx"
    bad.write_bytes(bytes(raw))
    assert app.validate_office_file(bad)[0] is False


# --------------------------------------------------------------------------- #
# PF-020 - folder mode validates exactly like manual mode
# --------------------------------------------------------------------------- #

def test_folder_and_manual_flows_agree_on_validation(tmp_path, monkeypatch):
    good = make_ooxml(tmp_path / "good.docx", "word")
    wrong_family = make_ooxml(tmp_path / "sheet.docx", "excel")
    corrupt = tmp_path / "corrupt.pptx"
    corrupt.write_bytes(b"not a package at all")

    folder_jobs = app.ops_office._build_jobs(
        app.discover_office_files(tmp_path)
    )
    manual_jobs = app.ops_office._build_jobs([good, wrong_family, corrupt])

    def names(result):
        accepted = result.accepted if hasattr(result, "accepted") else result
        return sorted(Path(j["src"]).name for j in accepted)

    assert names(folder_jobs) == names(manual_jobs) == ["good.docx"]


def test_folder_with_no_valid_jobs_reports_and_starts_nothing(tmp_path):
    (tmp_path / "a.docx").write_bytes(b"junk")
    (tmp_path / "b.xlsx").write_bytes(b"junk")
    result = app.ops_office._build_jobs(app.discover_office_files(tmp_path))
    accepted = result.accepted if hasattr(result, "accepted") else result
    assert accepted == [], "no LibreOffice run should be possible"
    if hasattr(result, "skipped"):
        assert len(result.skipped) == 2
        assert all(reason for _p, reason in result.skipped)


# --------------------------------------------------------------------------- #
# Writer export fallback (found by the real Windows E2E)
# --------------------------------------------------------------------------- #

def test_bridge_loss_without_password_falls_back_to_the_cli(tmp_path, monkeypatch):
    """unoserver 3.7 + LibreOffice 25.8 cannot export Writer documents here.

    The export raises inside the UNO bridge and pyuno then fails to marshal its
    own exception, destroying the real error and disposing the bridge. The same
    document converts through `soffice --convert-to`, so a bridge loss with no
    password involved must fall back to the CLI instead of failing the file.
    """
    from pdf_forge import office_runtime as ort

    src = make_ooxml(tmp_path / "doc.docx", "word")
    out = tmp_path / "doc.pdf"
    calls = {"cli": 0}

    class FakeServer:
        port = 1
        soffice = Path("soffice.exe")

    def exploding_client(**_kw):
        raise RuntimeError("Binary URP bridge already disposed")

    class FakeUnoClient:
        def __init__(self, **_kw):
            pass

        def convert(self, **_kw):
            exploding_client()

    def fake_cli(_soffice, _in_path, out_path, timeout=None):
        calls["cli"] += 1
        Path(out_path).write_bytes(b"%PDF-1.7\nfrom cli\n")

    import types
    fake_module = types.ModuleType("unoserver.client")
    fake_module.UnoClient = FakeUnoClient
    monkeypatch.setitem(sys.modules, "unoserver", types.ModuleType("unoserver"))
    monkeypatch.setitem(sys.modules, "unoserver.client", fake_module)
    monkeypatch.setattr(ort, "convert_via_soffice_cli", fake_cli)

    ort.convert_to_pdf(FakeServer(), src, out, timeout=30)
    assert calls["cli"] == 1, "the CLI fallback must run"
    assert out.read_bytes().startswith(b"%PDF")


def test_bridge_loss_with_a_password_does_not_fall_back(tmp_path, monkeypatch):
    """The CLI cannot carry a password without exposing it on a command line,
    so an encrypted source must stay on the in-memory path and fail loudly."""
    from pdf_forge import office_runtime as ort

    src = make_ooxml(tmp_path / "doc.docx", "word")

    class FakeServer:
        port = 1
        soffice = Path("soffice.exe")

    class FakeUnoClient:
        def __init__(self, **_kw):
            pass

        def convert(self, **_kw):
            raise RuntimeError("Binary URP bridge already disposed")

    called = {"cli": 0}

    def fake_cli(*_a, **_k):
        called["cli"] += 1

    import types
    fake_module = types.ModuleType("unoserver.client")
    fake_module.UnoClient = FakeUnoClient
    monkeypatch.setitem(sys.modules, "unoserver", types.ModuleType("unoserver"))
    monkeypatch.setitem(sys.modules, "unoserver.client", fake_module)
    monkeypatch.setattr(ort, "convert_via_soffice_cli", fake_cli)

    with pytest.raises(ort.OfficeRuntimeError):
        ort.convert_to_pdf(FakeServer(), src, tmp_path / "o.pdf",
                           password="secret", timeout=30)
    assert called["cli"] == 0, "a password must never reach the command line"
