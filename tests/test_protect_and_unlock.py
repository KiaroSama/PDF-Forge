# -*- coding: utf-8 -*-
"""Setting a password / restrictions, and removing them again.

Split out of the former single test_pdf_forge module. Tests use temporary
directories and generated small PDFs only; they never touch real user files.
"""

import sys
from pathlib import Path

import pytest  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402,F401
from helpers import file_hash, make_pdf  # noqa: E402,F401
from pypdf import PdfReader, PdfWriter  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# Protect / encrypt PDF
# --------------------------------------------------------------------------- #

def test_protect_open_password(tmp_path):
    import pymupdf

    src = make_pdf(tmp_path / "doc.pdf", 3)
    out = tmp_path / "protected.pdf"
    doc = app.open_source_pdf(src)
    try:
        written = app.save_encrypted_pdf(
            doc, out, user_pw="openme", owner_pw="openme",
            permissions=app.all_permissions(),
        )
    finally:
        doc.close()

    assert written.count == 3
    assert written.path == out          # destination was free, so no suffix
    # The output needs the password to open.
    locked = pymupdf.open(str(out))
    assert locked.needs_pass
    assert locked.authenticate("openme") != 0
    assert locked.page_count == 3
    locked.close()
    # Source is untouched (still opens freely).
    assert pymupdf.open(str(src)).needs_pass == 0


def test_protect_restrict_permissions(tmp_path):
    import pymupdf

    src = make_pdf(tmp_path / "doc.pdf", 2)
    # Block only editing + copying.
    actions = dict(app.restrictable_actions())
    blocked = actions["editing content"] | actions["copying text/images"]
    allowed = app.all_permissions() & ~blocked

    out = tmp_path / "restricted.pdf"
    doc = app.open_source_pdf(src)
    try:
        app.save_encrypted_pdf(doc, out, owner_pw="owner", permissions=allowed)
    finally:
        doc.close()

    check = pymupdf.open(str(out))
    try:
        assert check.needs_pass == 0            # opens freely (no user password)
        denied = app.denied_permissions(check)
        assert "editing content" in denied
        assert "copying text/images" in denied
        assert "printing" not in denied         # printing was left allowed
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# Unlock PDF (remove password & restrictions)
# --------------------------------------------------------------------------- #

def _make_owner_restricted_pdf(path, pages=2):
    """A PDF that opens freely but forbids copying/editing (owner password)."""
    import pymupdf

    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), "Protected document body text.")
    allowed = int(pymupdf.PDF_PERM_ACCESSIBILITY | pymupdf.PDF_PERM_PRINT)
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
             owner_pw="ownersecret", permissions=allowed)
    doc.close()
    return path


def test_denied_permissions_detects_restrictions(tmp_path):
    import pymupdf

    src = _make_owner_restricted_pdf(tmp_path / "owner.pdf")
    doc = pymupdf.open(str(src))
    try:
        denied = app.denied_permissions(doc)
    finally:
        doc.close()
    assert "copying text/images" in denied
    assert "editing content" in denied
    # A plain PDF has no restrictions.
    plain = make_pdf(tmp_path / "plain.pdf", 2)
    doc = pymupdf.open(str(plain))
    try:
        assert app.denied_permissions(doc) == []
    finally:
        doc.close()


def test_unlock_removes_owner_restrictions(tmp_path):
    import pymupdf

    src = _make_owner_restricted_pdf(tmp_path / "owner.pdf", pages=3)
    doc = pymupdf.open(str(src))          # opens freely (owner restriction only)
    out = tmp_path / "unlocked.pdf"
    try:
        written = app.unlock_pdf_doc(doc, out)
    finally:
        doc.close()

    assert written.count == 3
    assert written.path == out
    check = pymupdf.open(str(out))
    try:
        assert check.page_count == 3
        assert app.denied_permissions(check) == []   # all restrictions lifted
    finally:
        check.close()


def test_unlock_removes_open_password(tmp_path):
    import pymupdf

    base = make_pdf(tmp_path / "base.pdf", 2)
    doc = pymupdf.open(str(base))
    src = tmp_path / "locked.pdf"
    doc.save(str(src), encryption=pymupdf.PDF_ENCRYPT_AES_256,
             user_pw="openme", owner_pw="owner")
    doc.close()

    # Must authenticate before unlocking.
    doc = pymupdf.open(str(src))
    assert doc.needs_pass
    doc.authenticate("openme")
    out = tmp_path / "unlocked.pdf"
    try:
        app.unlock_pdf_doc(doc, out)
    finally:
        doc.close()

    check = pymupdf.open(str(out))
    try:
        assert check.needs_pass == 0     # no password needed anymore
        assert check.page_count == 2
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# N-06 - the writer must prove the requested permission mask actually took
# effect (bit-by-bit), not just that the output re-opens with the right page
# count. A restrict that dropped the mask, or an unlock that kept restrictions,
# must fail before promotion and leave no output.
# --------------------------------------------------------------------------- #

def test_restrict_output_that_drops_the_mask_fails_validation(tmp_path, monkeypatch):
    import pymupdf

    src = make_pdf(tmp_path / "doc.pdf", 1)
    out = tmp_path / "restricted.pdf"
    allowed = int(pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ACCESSIBILITY)
    doc = app.open_source_pdf(src)
    real_save = doc.save

    def sabotaged(path, **kw):          # honour encryption, ignore the mask
        kw["permissions"] = app.all_permissions()
        return real_save(path, **kw)

    monkeypatch.setattr(doc, "save", sabotaged)
    try:
        with pytest.raises(app.PdfOpenError):
            app.save_encrypted_pdf(doc, out, user_pw=None,
                                   owner_pw="owner", permissions=allowed)
    finally:
        doc.close()
    assert not out.exists(), "a restrict output that ignored the mask was promoted"
    assert list(tmp_path.glob(".pdfforge_*")) == []


def test_unlock_output_that_keeps_restrictions_fails_validation(tmp_path, monkeypatch):
    import pymupdf

    src = _make_owner_restricted_pdf(tmp_path / "owner.pdf")
    doc = pymupdf.open(str(src))        # opens freely, restricted
    out = tmp_path / "unlocked.pdf"
    real_save = doc.save

    def sabotaged(path, **kw):          # ignore PDF_ENCRYPT_NONE, keep restrictions
        return real_save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                         owner_pw="ownersecret",
                         permissions=int(pymupdf.PDF_PERM_PRINT))

    monkeypatch.setattr(doc, "save", sabotaged)
    try:
        with pytest.raises(app.PdfOpenError):
            app.unlock_pdf_doc(doc, out)
    finally:
        doc.close()
    assert not out.exists(), "a still-restricted 'unlock' output was promoted"
    assert list(tmp_path.glob(".pdfforge_*")) == []


def test_restrict_output_bits_match_requested_mask(tmp_path):
    """Positive: a correct restrict output verifies bit-by-bit (pymupdf 1.28.0)."""
    import pymupdf

    src = make_pdf(tmp_path / "doc.pdf", 1)
    out = tmp_path / "restricted.pdf"
    expected = app.all_permissions() & ~int(
        pymupdf.PDF_PERM_MODIFY | pymupdf.PDF_PERM_COPY)
    doc = app.open_source_pdf(src)
    try:
        app.save_encrypted_pdf(doc, out, user_pw=None,
                               owner_pw="owner", permissions=expected)
    finally:
        doc.close()
    check = pymupdf.open(str(out))
    try:
        assert not check.needs_pass
        for bit in app.permission_bits().values():          # BIT-BY-BIT, not raw int
            assert bool(check.permissions & int(bit)) == bool(expected & int(bit))
    finally:
        check.close()


def test_compress_temp_cleanup_on_failure(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "doc.pdf", 3)
    out = tmp_path / "fail.pdf"

    def boom(*_args, **_kwargs):
        raise app.PdfOpenError("simulated compression validation failure")

    monkeypatch.setattr(app.compress, "_validate_written_pdf", boom)

    with pytest.raises(app.PdfOpenError):
        app.compress_pdf(src, out, None, None)

    assert not out.exists()
    assert list(tmp_path.glob(".pdfforge_*")) == []
