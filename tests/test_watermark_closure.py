# -*- coding: utf-8 -*-
"""Closure tests for the watermark operation (C-13, C-14).

C-13 - the removal operation must decide the protection policy during
configuration, with the user's consent, and hand that decided policy to the
writer. Nothing may be written before the question is answered.

C-14 - an inline image (``BI ... ID ... EI``) has no xref, so it can never be
removed. It must not be offered as a candidate, the writer must refuse a
selection it cannot act on before writing anything, and target-absence
validation must run on the staging file, before promotion.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
import pymupdf  # noqa: E402
from helpers import stamped_pdf  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def restricted_pdf(path: Path, pages: int = 3) -> Path:
    """Owner-restricted: opens without a password but forbids most actions."""
    perms = int(pymupdf.PDF_PERM_ACCESSIBILITY)
    return stamped_pdf(path, pages,
                       encryption=pymupdf.PDF_ENCRYPT_AES_256,
                       owner_pw="owner", permissions=perms)


def inline_image_pdf(path: Path, pages: int = 3) -> Path:
    """A PDF whose repeated image is an INLINE image (BI ... ID ... EI).

    Inline images live in the content stream, not in the object graph, so MuPDF
    reports them with ``xref == 0`` and they cannot be deleted by object
    replacement.
    """
    raw = bytes(range(256))                       # 16x16 grayscale samples
    inline = (b"q 100 0 0 100 20 20 cm\n"
              b"BI /W 16 /H 16 /CS /G /BPC 8 /F /AHx ID\n"
              + raw.hex().encode("ascii") + b">\nEI\nQ\n")
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=200, height=200)
        page.insert_text((10, 180), "keep me")
    for page in doc:
        xref = page.get_contents()[0]
        doc.update_stream(xref, inline + (doc.xref_stream(xref) or b""))
    doc.save(str(path))
    doc.close()
    return path


def drive(monkeypatch, answers, password=None, ask=None):
    """Run the real operation through the real prompts. True when it queued."""
    supplied = iter(answers)
    for module in (app.ops_watermark, app.prompts):
        monkeypatch.setattr(module, "_input", lambda _p: next(supplied),
                            raising=False)
    if password is not None:
        monkeypatch.setattr(app.ops_watermark, "prompt_password",
                            lambda previous_failed=False: password)
    if ask is not None:
        monkeypatch.setattr(app.prompts, "ask_yes_no", ask)
    try:
        app.operation_remove_watermark()
    except app.taskqueue._TaskQueued:
        return True
    except StopIteration:  # pragma: no cover - test wiring problem
        pytest.fail("the operation asked more questions than the script supplied")
    return False


def outputs(folder: Path, source: Path):
    return sorted(p.name for p in folder.iterdir() if p != source)


# --------------------------------------------------------------------------- #
# C-13 - the protection policy is decided during configuration, with consent
# --------------------------------------------------------------------------- #

def test_open_password_source_keeps_its_password(tmp_path, monkeypatch):
    """The decided policy - not a run-time re-detection - protects the output."""
    src = stamped_pdf(tmp_path / "enc.pdf", 3,
                      encryption=pymupdf.PDF_ENCRYPT_AES_256,
                      user_pw="pw", owner_pw="pw")
    assert drive(monkeypatch, [str(src), "", ""], password="pw")

    # Sabotage the writer's run-time fallback: if it is still what decides,
    # the output loses its password.
    monkeypatch.setattr(app.watermark, "detect_protection",
                        lambda _doc: app.ProtectionPolicy(kind="none"))
    app.taskqueue._task_queue[-1].run()

    out = tmp_path / "enc_no_watermark.pdf"
    assert out.exists(), "the removal produced no output"
    check = pymupdf.open(str(out))
    try:
        assert check.needs_pass, "the source password was silently dropped"
        assert check.authenticate("pw") > 0
    finally:
        check.close()


def test_owner_restricted_source_asks_before_queueing(tmp_path, monkeypatch):
    asked = []

    def ask(question, default_yes=True):
        asked.append(question)
        return True

    src = restricted_pdf(tmp_path / "restricted.pdf")
    queued = drive(monkeypatch, [str(src), "", ""], ask=ask)
    assert asked, "the lost owner restrictions were never raised with the user"
    assert queued, "consent was given, so the task must be queued"


def test_declining_leaves_no_task_and_no_output(tmp_path, monkeypatch):
    src = restricted_pdf(tmp_path / "restricted.pdf")
    before = outputs(tmp_path, src)
    queued = drive(monkeypatch, [str(src), "", ""],
                   ask=lambda *_a, **_k: False)
    assert not queued, "a declined operation must not be queued"
    assert app.taskqueue._task_queue == []
    assert outputs(tmp_path, src) == before, "a declined operation wrote a file"


def test_accepted_downgrade_produces_an_unprotected_output(tmp_path, monkeypatch):
    src = restricted_pdf(tmp_path / "restricted.pdf")
    assert drive(monkeypatch, [str(src), "", ""], ask=lambda *_a, **_k: True)
    app.taskqueue._task_queue[-1].run()

    out = tmp_path / "restricted_no_watermark.pdf"
    assert out.exists()
    check = pymupdf.open(str(out))
    try:
        assert not check.needs_pass, "the user chose an unprotected output"
    finally:
        check.close()


def test_consent_is_asked_before_anything_is_written(tmp_path, monkeypatch):
    src = restricted_pdf(tmp_path / "restricted.pdf")
    seen = {}

    def ask(question, default_yes=True):
        seen["files"] = outputs(tmp_path, src)
        return True

    drive(monkeypatch, [str(src), "", ""], ask=ask)
    assert "files" in seen, "no consent question was asked at all"
    assert seen["files"] == [], (
        f"files existed in the output folder at consent time: {seen['files']}"
    )


# --------------------------------------------------------------------------- #
# C-14 - inline candidates and no-op outputs
# --------------------------------------------------------------------------- #

def test_the_inline_fixture_really_is_an_inline_image(tmp_path):
    """Fixture validity: no xref, invisible to the object graph."""
    src = inline_image_pdf(tmp_path / "inline.pdf")
    doc = pymupdf.open(str(src))
    try:
        for page in doc:
            info = page.get_image_info(hashes=True, xrefs=True)
            assert len(info) == 1, info
            assert info[0]["xref"] == 0, "an inline image has no xref"
            assert info[0]["width"] >= 8 and info[0]["height"] >= 8
            assert page.get_images(full=True) == [], \
                "an inline image must not appear in the object graph"
    finally:
        doc.close()


def test_inline_only_images_are_not_selectable_and_are_reported(tmp_path):
    src = inline_image_pdf(tmp_path / "inline.pdf")
    doc = app.open_source_pdf(src)
    try:
        candidates, _total, skipped = app.scan_watermark_candidates(
            doc, with_skipped=True)
    finally:
        app.close_doc(doc)
    assert candidates == [], \
        "an inline image cannot be removed, so it must not be offered"
    assert skipped == 1, "the user must be told how many were skipped"


def test_the_operation_reports_skipped_inline_images(tmp_path, monkeypatch, capsys):
    src = inline_image_pdf(tmp_path / "inline.pdf")
    queued = drive(monkeypatch, [str(src)])
    assert not queued, "nothing removable was found, so nothing may be queued"
    output = capsys.readouterr().out
    assert "inline" in output.lower(), (
        "the skipped inline image was never mentioned to the user"
    )


def test_an_unknown_signature_creates_no_output(tmp_path):
    src = stamped_pdf(tmp_path / "src.pdf", pages=2)
    out = tmp_path / "out.pdf"
    doc = app.open_source_pdf(src)
    try:
        with pytest.raises(ValueError):
            app.remove_watermark_images(doc, ["nosuch:1x1"], out)
    finally:
        app.close_doc(doc)
    assert not out.exists(), "a no-op must not leave a file at the chosen path"
    assert outputs(tmp_path, src) == [], "a no-op must write nothing at all"
    assert app.load_generated_outputs() == set(), \
        "a no-op must not enter the generated-output manifest"


def test_target_absence_is_validated_on_the_staging_file(tmp_path, monkeypatch):
    """The postcondition must run on the staging bytes, before promotion."""
    src = stamped_pdf(tmp_path / "src.pdf", pages=2)
    out = tmp_path / "out.pdf"
    order = []

    real_validate = app.watermark.validate_watermark_removed
    real_promote = app.watermark.promote_atomically

    def validate(path, signatures, password=None):
        order.append(("validate", Path(path)))
        return real_validate(path, signatures, password=password)

    def promote(tmp, final, **kwargs):
        order.append(("promote", Path(tmp)))
        return real_promote(tmp, final, **kwargs)

    monkeypatch.setattr(app.watermark, "validate_watermark_removed", validate)
    monkeypatch.setattr(app.watermark, "promote_atomically", promote)

    doc = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        app.remove_watermark_images(doc, [candidates[0].signature], out)
    finally:
        app.close_doc(doc)

    assert [step for step, _p in order] == ["validate", "promote"], order
    assert order[0][1] == order[1][1], \
        "validation must inspect the same staging file that is promoted"


def test_a_rejected_output_never_reaches_the_user(tmp_path, monkeypatch):
    src = stamped_pdf(tmp_path / "src.pdf", pages=2)
    out = tmp_path / "out.pdf"

    def reject(*_a, **_k):
        raise app.PdfOpenError("the selected watermark is still present")

    monkeypatch.setattr(app.watermark, "validate_watermark_removed", reject)

    doc = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        with pytest.raises(app.PdfOpenError):
            app.remove_watermark_images(doc, [candidates[0].signature], out)
    finally:
        app.close_doc(doc)

    assert outputs(tmp_path, src) == [], \
        "a rejected output must not be left on disk"
    assert app.load_generated_outputs() == set(), \
        "a rejected output must not enter the generated-output manifest"


def test_the_written_path_is_reported_not_the_configured_one(tmp_path):
    """Promotion may allocate a sibling name; the result must carry the truth."""
    src = stamped_pdf(tmp_path / "src.pdf", pages=2)
    out = tmp_path / "out.pdf"
    out.write_bytes(b"taken")          # the requested name is already in use

    doc = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        result = app.remove_watermark_images(doc, [candidates[0].signature], out)
    finally:
        app.close_doc(doc)

    assert result.path != out and result.path.exists()
    assert result.count == 2
    assert out.read_bytes() == b"taken", "an existing file must not be clobbered"
