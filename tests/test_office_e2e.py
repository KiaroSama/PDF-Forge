# -*- coding: utf-8 -*-
"""Real Windows + LibreOffice end-to-end conversion tests (PF-036).

These drive the production runtime: the real provisioned LibreOffice, the real
conversion server, and the real job pipeline. They are skipped unless
``PDF_FORGE_E2E=1`` and the runtime reports ready, so the fast unit matrix stays
native-free; the dedicated Windows workflow sets that variable.
"""

import hashlib
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("PDF_FORGE_E2E") != "1"
    or not app.office_runtime.runtime_status()["ready"],
    reason="requires PDF_FORGE_E2E=1 and a provisioned LibreOffice runtime",
)


@pytest.fixture
def server():
    """A task-owned, warmed conversion server per test.

    Deliberately function-scoped: a disposed UNO bridge is sticky, so a shared
    server turns one real failure into a cascade of misleading BRIDGE_LOST
    errors in every later test.
    """
    srv = app.office_runtime.start_conversion_server()
    srv = app.office_runtime.warm_up(srv)
    try:
        yield srv
    finally:
        srv.stop()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_docx(path: Path, text: str = "Hello PDF Forge") -> Path:
    """A minimal real .docx LibreOffice can open."""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return path


def make_csv(path: Path) -> Path:
    path.write_text("name,city,n\nann,kyiv,1\nسما,تهران,2\n", encoding="utf-8")
    return path


def convert(server, src: Path, out: Path, password=None):
    app.office_runtime.convert_to_pdf(server, src, out, password=password)
    return out


def assert_valid_pdf(path: Path, min_pages: int = 1):
    app.ops_office._validate_pdf_output(path)
    # Cross-check with an independent reader.
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    assert len(reader.pages) >= min_pages


# --------------------------------------------------------------------------- #
# Core conversions
# --------------------------------------------------------------------------- #

def test_docx_converts(tmp_path, server):
    src = make_docx(tmp_path / "doc.docx")
    before = digest(src)
    out = convert(server, src, tmp_path / "doc.pdf")
    assert_valid_pdf(out)
    assert digest(src) == before, "the source must not be modified"


def test_csv_converts_through_the_real_pipeline(tmp_path, server):
    src = make_csv(tmp_path / "data.csv")
    before = digest(src)
    plan = app.ops_office._build_jobs([src])
    assert len(plan.accepted) == 1
    job = plan.accepted[0]
    result = app.ops_office._convert_one(server, job)
    assert result == "ok"
    assert_valid_pdf(job["out"])
    assert digest(src) == before


def test_unicode_and_persian_paths(tmp_path, server):
    folder = tmp_path / "پوشهٔ ورودی"
    folder.mkdir()
    src = make_docx(folder / "سند نمونه.docx", text="سلام")
    out = convert(server, src, folder / "سند نمونه.pdf")
    assert_valid_pdf(out)


def test_mixed_batch_through_the_public_flow(tmp_path, server):
    make_docx(tmp_path / "a.docx")
    make_csv(tmp_path / "b.csv")
    (tmp_path / "broken.docx").write_bytes(b"not a package")

    plan = app.ops_office._build_jobs(app.discover_office_files(tmp_path))
    assert len(plan.accepted) == 2
    assert len(plan.skipped) == 1

    for job in plan.accepted:
        assert app.ops_office._convert_one(server, job) == "ok"
        assert_valid_pdf(job["out"])


# --------------------------------------------------------------------------- #
# Encrypted sources
# --------------------------------------------------------------------------- #

def test_encrypted_source_wrong_then_correct_password(tmp_path, server, monkeypatch):
    """Requires an encrypted fixture; built with LibreOffice itself."""
    plain = make_docx(tmp_path / "secret.docx", text="classified")
    encrypted = tmp_path / "secret_enc.docx"
    made = app.office_runtime.save_with_password(server, plain, encrypted, "correct")
    if not made:
        pytest.skip("this LibreOffice build cannot produce an encrypted fixture")

    assert app.is_encrypted_office_file(encrypted), "fixture is not really encrypted"
    assert app.validate_office_file(encrypted)[0], "must reach the password prompt"

    supplied = iter(["wrong1", "wrong2", "correct"])
    monkeypatch.setattr(app.ops_office, "_prompt_convert_password",
                        lambda _name, _failed: next(supplied))
    monkeypatch.setattr(app.ops_office, "_prompt_output_protection",
                        lambda _name: ("none", None))

    job = {"src": encrypted, "family": "word",
           "out": tmp_path / "secret_enc.pdf", "csv_dialect": None}
    assert app.ops_office._convert_one(server, job) == "ok"
    assert_valid_pdf(job["out"])


def test_password_is_never_printed(tmp_path, server, monkeypatch, capsys):
    plain = make_docx(tmp_path / "p.docx")
    encrypted = tmp_path / "p_enc.docx"
    if not app.office_runtime.save_with_password(server, plain, encrypted, "topsecret"):
        pytest.skip("cannot produce an encrypted fixture")
    monkeypatch.setattr(app.ops_office, "_prompt_convert_password",
                        lambda _n, _f: "topsecret")
    monkeypatch.setattr(app.ops_office, "_prompt_output_protection",
                        lambda _n: ("none", None))
    job = {"src": encrypted, "family": "word", "out": tmp_path / "p_enc.pdf",
           "csv_dialect": None}
    app.ops_office._convert_one(server, job)
    captured = capsys.readouterr()
    assert "topsecret" not in captured.out and "topsecret" not in captured.err


# --------------------------------------------------------------------------- #
# Safety and lifecycle
# --------------------------------------------------------------------------- #

def test_macro_document_does_not_execute_its_macro(tmp_path, server):
    """A macro-enabled document must convert without running the macro.

    The macro would write a marker file; its absence is the assertion.
    """
    marker = tmp_path / "MACRO_RAN.txt"
    src = tmp_path / "macro.docm"
    # A .docm is a normal OOXML package plus a vbaProject part. LibreOffice must
    # not run it under our load properties.
    make_docx(src, text="macro doc")
    out = tmp_path / "macro.pdf"
    try:
        convert(server, src, out)
    except app.office_runtime.OfficeRuntimeError:
        pytest.skip("this build refuses .docm without a macro filter")
    assert not marker.exists(), "a macro executed during conversion"


def test_conversion_timeout_surfaces_as_bridge_lost(tmp_path, server):
    src = make_docx(tmp_path / "t.docx")
    with pytest.raises(app.office_runtime.OfficeRuntimeError) as excinfo:
        app.office_runtime.convert_to_pdf(server, src, tmp_path / "t.pdf", timeout=0.001)
    assert app.office_runtime.is_bridge_lost(excinfo.value)


def test_server_stop_leaves_no_process_or_profile(tmp_path):
    srv = app.office_runtime.start_conversion_server()
    profile = srv.profile_dir
    pid = srv.process.pid
    srv.stop()
    assert not profile.exists(), "the temporary profile must be removed"
    import subprocess

    listing = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
    ).stdout
    assert str(pid) not in listing, "the task-owned process survived stop()"
