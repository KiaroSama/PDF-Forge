# -*- coding: utf-8 -*-
"""Convert-to-PDF source handling: classification, validation, encrypted-source
detection, and CSV normalization.

Split out of the former single test_regressions module. Each test targets
behaviour that was wrong (or absent) before its fix, so it fails against the
old implementation for the right reason. Tests use temporary directories and
generated files only; they never touch real user files and never require the
native LibreOffice runtime.
"""

import csv  # noqa: F401
import io  # noqa: F401
import os  # noqa: F401
import sys
from pathlib import Path

import pytest  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402,F401
import pymupdf  # noqa: E402,F401
from PIL import Image  # noqa: E402,F401
from helpers import (  # noqa: E402,F401
    label_of, make_encrypted, make_pdf, repeated_image_pdf, rgb_png, rgba_png,
    zip_ooxml,
)
from pypdf import PdfWriter  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# B - office source handling (no native runtime required)
# --------------------------------------------------------------------------- #

def test_office_family_classification():
    assert app.classify_office_file(Path("a.docx")) == "word"
    assert app.classify_office_file(Path("a.DOC")) == "word"
    assert app.classify_office_file(Path("b.pptx")) == "powerpoint"
    assert app.classify_office_file(Path("c.xls")) == "excel"
    assert app.classify_office_file(Path("d.csv")) == "csv"
    assert app.classify_office_file(Path("e.pdf")) is None
    assert set(app.SUPPORTED_OFFICE_EXTS) == {
        ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv"
    }


def test_ooxml_validation_and_renamed_binaries(tmp_path):
    assert app.validate_office_file(zip_ooxml(tmp_path / "good.docx"))[0]

    # A ZIP carrying only [Content_Types].xml is not a real package.
    import zipfile

    stub = tmp_path / "stub.docx"
    with zipfile.ZipFile(stub, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    assert not app.validate_office_file(stub)[0]

    # A spreadsheet renamed to .docx must be rejected as the wrong family.
    wrong_family = zip_ooxml(tmp_path / "sheet.docx", family="excel")
    ok, reason = app.validate_office_file(wrong_family)
    assert not ok and "excel" in reason.lower()

    fake = tmp_path / "fake.docx"
    fake.write_bytes(b"not a zip at all")
    ok, reason = app.validate_office_file(fake)
    assert not ok and "OOXML" in reason

    binary_csv = tmp_path / "bin.csv"
    binary_csv.write_bytes(b"\x00\x01\x02binary")
    ok, reason = app.validate_office_file(binary_csv)
    assert not ok and "binary" in reason.lower()

    # The OLE2 magic alone does not make a real .doc: with no readable OLE2
    # directory the family marker cannot be checked, so it must be rejected
    # rather than accepted on the 8 signature bytes (N-08).
    legacy = tmp_path / "old.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    ok, reason = app.validate_office_file(legacy)
    assert not ok and "OLE2" in reason

    bad_legacy = tmp_path / "bad.doc"
    bad_legacy.write_bytes(b"plain text pretending to be a doc")
    ok, reason = app.validate_office_file(bad_legacy)
    assert not ok and "signature" in reason

    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    assert not app.validate_office_file(empty)[0]


def test_office_discovery_skips_lock_files_and_sorts_naturally(tmp_path):
    zip_ooxml(tmp_path / "b2.docx")
    zip_ooxml(tmp_path / "b10.docx")
    zip_ooxml(tmp_path / "~$b2.docx")
    (tmp_path / "notes.txt").write_text("ignore me")
    (tmp_path / "sheet.csv").write_text("a,b\n1,2\n")
    (tmp_path / "sub").mkdir()
    zip_ooxml(tmp_path / "sub" / "nested.docx")     # non-recursive

    names = [p.name for p in app.discover_office_files(tmp_path)]
    assert "~$b2.docx" not in names
    assert "notes.txt" not in names
    assert "nested.docx" not in names
    assert names == ["b2.docx", "b10.docx", "sheet.csv"]


def test_office_lock_file_detection():
    assert app.is_office_lock_file("~$report.docx")
    assert not app.is_office_lock_file("report.docx")


def test_office_family_counts():
    counts = app.family_counts([Path("a.docx"), Path("b.doc"), Path("c.pptx"),
                                Path("d.xlsx"), Path("e.csv"), Path("f.pdf")])
    assert counts == {"word": 2, "powerpoint": 1, "excel": 1, "csv": 1}


@pytest.mark.parametrize("text,delim", [
    ("a,b,c\n1,2,3\n4,5,6\n", ","),
    ("a;b;c\n1;2;3\n4;5;6\n", ";"),
    ("a\tb\tc\n1\t2\t3\n4\t5\t6\n", "\t"),
])
def test_csv_delimiter_detection(tmp_path, text, delim):
    path = tmp_path / "d.csv"
    path.write_text(text, encoding="utf-8")
    dialect = app.detect_csv_dialect(path)
    assert dialect.delimiter == delim
    assert dialect.encoding == "UTF-8"


def test_csv_bom_is_detected(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf" + "name,note\n1,2\n".encode("utf-8"))
    dialect = app.detect_csv_dialect(path)
    assert dialect.encoding == "UTF-8"
    assert "BOM detected" in dialect.notes


def test_csv_quoted_multiline_field(tmp_path):
    path = tmp_path / "q.csv"
    path.write_text('name,note\n"a","line1\nline2"\n', encoding="utf-8")
    dialect = app.detect_csv_dialect(path)
    assert dialect.delimiter == ","


def test_csv_non_utf8_uses_deterministic_fallback(tmp_path):
    path = tmp_path / "latin.csv"
    path.write_bytes("naive,cafe\n1,2\n".encode("cp1252") + b"\xe9\n")
    dialect = app.detect_csv_dialect(path)
    assert dialect.encoding in ("UTF-8", "windows-1252")


# --------------------------------------------------------------------------- #
# B - encrypted-source detection, CSV normalization, runtime resilience
# --------------------------------------------------------------------------- #

def test_encrypted_office_detection_ooxml(tmp_path):
    """A password-to-open OOXML file is an OLE2 container, not a ZIP.

    Detection parses the OLE directory, so a real container is required: loose
    marker bytes must NOT be enough (that was a false-positive source).
    """
    from test_office_validation import make_encrypted_ooxml

    encrypted = make_encrypted_ooxml(tmp_path / "locked.docx")
    assert app.is_encrypted_office_file(encrypted)
    # Validation accepts it so the flow can ask for the password (PF-002).
    assert app.validate_office_file(encrypted)[0]

    # Marker bytes alone are not an encrypted package.
    bogus = tmp_path / "bogus.docx"
    bogus.write_bytes(
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
        + "EncryptedPackage".encode("utf-16-le") + b"\x00" * 64
    )
    assert not app.is_encrypted_office_file(bogus)


def test_encrypted_office_detection_odf(tmp_path):
    import zipfile

    encrypted = tmp_path / "locked.odt"
    with zipfile.ZipFile(encrypted, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("META-INF/manifest.xml",
                    '<manifest><encryption-data checksum="x"/></manifest>')
    assert app.is_encrypted_office_file(encrypted)


def test_plain_office_file_not_flagged_encrypted(tmp_path):
    assert not app.is_encrypted_office_file(zip_ooxml(tmp_path / "plain.docx"))
    plain_csv = tmp_path / "a.csv"
    plain_csv.write_text("a,b\n1,2\n", encoding="utf-8")
    assert not app.is_encrypted_office_file(plain_csv)
    assert not app.is_encrypted_office_file(tmp_path / "missing.docx")


def test_csv_normalization_applies_detected_dialect(tmp_path):
    source = tmp_path / "semi.csv"
    source.write_text("name;count\nalpha;1\nbeta;2\n", encoding="utf-8")
    original = source.read_bytes()

    dialect = app.detect_csv_dialect(source)
    assert dialect.delimiter == ";"
    out = app.normalize_csv_for_import(dialect=dialect, path=source,
                                       out_path=tmp_path / "norm.csv")
    text = out.read_text(encoding="utf-8")
    assert "name,count" in text and "alpha,1" in text
    assert source.read_bytes() == original, "the source CSV must not be modified"


def test_csv_normalization_preserves_quoted_fields(tmp_path):
    source = tmp_path / "q.csv"
    source.write_text('name;note\n"a";"x;y"\n', encoding="utf-8")
    dialect = app.detect_csv_dialect(source)
    out = app.normalize_csv_for_import(source, dialect, tmp_path / "n.csv")
    rows = list(csv.reader(out.open(encoding="utf-8", newline="")))
    assert rows[1] == ["a", "x;y"], rows


def test_bridge_loss_is_classified_for_restart():
    """A dead UNO bridge must be distinguishable from a bad input file."""
    classify = app.office_runtime._classify_convert_error
    for message in ("Binary URP bridge already disposed",
                    "Looks like LibreOffice died",
                    "[WinError 10061] No connection could be made"):
        assert classify(Exception(message)) == app.office_runtime.BRIDGE_LOST_SENTINEL
    assert app.office_runtime.is_bridge_lost(
        app.office_runtime.OfficeRuntimeError(
            app.office_runtime.BRIDGE_LOST_SENTINEL)
    )


def test_password_error_is_classified_for_retry():
    classify = app.office_runtime._classify_convert_error
    assert classify(Exception("wrong password supplied")) == \
        app.office_runtime.PASSWORD_SENTINEL
    generic = classify(Exception("some unrelated failure"))
    assert generic not in (app.office_runtime.PASSWORD_SENTINEL,
                           app.office_runtime.BRIDGE_LOST_SENTINEL)


def test_venv_site_packages_excludes_the_venv_root():
    """Putting sys.prefix on PYTHONPATH breaks LibreOffice's embedded Python."""
    for path in app.office_runtime.venv_site_packages():
        assert Path(path).name.lower() == "site-packages", path


def test_windows_prefers_soffice_exe_not_the_com_shim():
    """soffice.com exits after launching the real binary, which kills the bridge."""
    names = app.office_discovery._soffice_names()
    if os.name == "nt":
        assert names[0] == "soffice.exe"
    else:
        assert names == ["soffice"]


def test_profile_argument_is_a_plain_path(tmp_path):
    """unoserver calls Path(value).as_uri() itself; a URI would be rejected."""
    value = app.office_server._profile_argument(tmp_path)
    assert not value.startswith("file:")
    assert Path(value).is_absolute()


def test_converted_pdfs_remain_discoverable_for_pdf_tools(tmp_path):
    """A6 scoping: convert output is a new source, not a reprocessed output."""
    app.forget_generated_outputs()
    try:
        converted = make_pdf(tmp_path / "report.pdf", 1)   # as convert would write
        names = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
        assert "report.pdf" in names, (
            "a PDF converted from a document must stay available to PDF tools"
        )
        assert converted.exists()
    finally:
        app.forget_generated_outputs()


def test_crashed_libreoffice_marshalling_error_is_bridge_loss():
    """unoserver reports a dead LibreOffice as a traceback-marshalling failure.

    The classifier lower-cases the message, so the needle must be lower-case
    too - otherwise the very first crash is mistaken for a bad input file and
    the runtime is never restarted.
    """
    classify = app.office_runtime._classify_convert_error
    real_message = (
        "<Fault 1: \"<class 'uno.com.sun.star.uno.RuntimeException'>:Couldn't "
        "convert <traceback object at 0x1> to a UNO type; caught exception: "
        "<class 'AttributeError'>: 'traceback' object has no attribute "
        "'getTypes', traceback follows\">"
    )
    assert classify(Exception(real_message)) == \
        app.office_runtime.BRIDGE_LOST_SENTINEL


def test_b_re_enters_the_previous_file(tmp_path, monkeypatch):
    """'b' drops the file added last so it can be entered again."""
    a = make_pdf(tmp_path / "a.pdf", 1)
    b = make_pdf(tmp_path / "b.pdf", 1)
    c = make_pdf(tmp_path / "c.pdf", 1)
    # add a, add b, 'b' (undo b), add c, done
    answers = iter([str(a), str(b), "b", str(c), "done"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "c.pdf"]


def test_b_prompt_number_goes_back(tmp_path, monkeypatch):
    """After 'b' the prompt asks for that same file number again."""
    a = make_pdf(tmp_path / "a.pdf", 1)
    b = make_pdf(tmp_path / "b.pdf", 1)
    seen = []
    answers = iter([str(a), str(b), "b", str(b), "done"])

    def fake_input(prompt):
        seen.append(prompt)
        return next(answers)

    monkeypatch.setattr(app.ops_merge, "_input", fake_input)
    app.ops_merge.prompt_merge_source_files()
    titles = [p.split(". ", 1)[1].split(" (")[0] for p in seen]
    assert titles[:4] == ["PDF file #1", "PDF file #2", "PDF file #3", "PDF file #2"]


def test_b_with_nothing_selected_is_rejected(tmp_path, monkeypatch):
    a = make_pdf(tmp_path / "a.pdf", 1)
    b = make_pdf(tmp_path / "b.pdf", 1)
    answers = iter(["b", str(a), str(b), "done"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "b.pdf"]


def test_finish_keyword_is_gone(tmp_path, monkeypatch):
    """'finish' is no longer a terminator - it is treated as a path."""
    a = make_pdf(tmp_path / "a.pdf", 1)
    b = make_pdf(tmp_path / "b.pdf", 1)
    answers = iter([str(a), str(b), "finish", "done"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "b.pdf"]


def test_office_prompt_supports_b_and_done(tmp_path, monkeypatch):
    first = zip_ooxml(tmp_path / "one.docx")
    second = zip_ooxml(tmp_path / "two.docx")
    answers = iter([str(first), "b", str(second), "done"])
    monkeypatch.setattr(app.ops_office, "_input", lambda _p: next(answers))
    result = app.ops_office.prompt_office_source_files()
    assert [p.name for p in result] == ["two.docx"]
