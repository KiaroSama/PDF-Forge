# -*- coding: utf-8 -*-
"""Regression tests for operation-specific output validation (PF-035).

A page-count check passes for a file holding the right *number* of the wrong
pages. These tests build deliberately wrong-but-openable outputs and assert the
validators reject them.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
import pymupdf  # noqa: E402


def distinct_pdf(path: Path, pages: int = 5) -> Path:
    """Pages with different text so page identity is meaningful."""
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=300, height=400)
        page.insert_text((50, 100), f"PAGE NUMBER {index + 1}", fontsize=20)
    doc.save(str(path))
    doc.close()
    return path


def build_from(src: Path, out: Path, pages_zero_based) -> Path:
    doc = pymupdf.open(str(src))
    new = pymupdf.open()
    try:
        for index in pages_zero_based:
            new.insert_pdf(doc, from_page=index, to_page=index)
        new.save(str(out))
    finally:
        new.close()
        doc.close()
    return out


# --------------------------------------------------------------------------- #
# Page selection and order
# --------------------------------------------------------------------------- #

def test_correct_selection_passes(tmp_path):
    src = distinct_pdf(tmp_path / "src.pdf")
    out = build_from(src, tmp_path / "out.pdf", [0, 2, 4])
    doc = app.open_source_pdf(src)
    try:
        app.validate_page_selection_output(out, doc, [0, 2, 4])
    finally:
        app.close_doc(doc)


def test_wrong_pages_with_the_right_count_are_rejected(tmp_path):
    """The exact defect a page-count check cannot see."""
    src = distinct_pdf(tmp_path / "src.pdf")
    out = build_from(src, tmp_path / "out.pdf", [1, 2, 3])   # asked for 0,2,4
    doc = app.open_source_pdf(src)
    try:
        with pytest.raises(app.PdfOpenError) as excinfo:
            app.validate_page_selection_output(out, doc, [0, 2, 4])
    finally:
        app.close_doc(doc)
    assert "does not match" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# N-07 - page identity must not be length-only. Two textless vector pages whose
# draw commands are the same byte length collide under a dimensions+text+length
# fingerprint, so a swapped/wrong page slips past. Hashing the stream bytes
# (plus rotation) distinguishes them.
# --------------------------------------------------------------------------- #

def textless_colored_pdf(path: Path, colors) -> Path:
    """Same-size, textless pages differing only by fill colour - equal-length
    draw ops, so a length-only fingerprint cannot tell them apart."""
    doc = pymupdf.open()
    for c in colors:
        page = doc.new_page(width=200, height=200)
        page.draw_rect(pymupdf.Rect(50, 50, 150, 150), color=c, fill=c)
    doc.save(str(path))
    doc.close()
    return path


def test_swapped_textless_pages_are_rejected(tmp_path):
    src = textless_colored_pdf(tmp_path / "src.pdf", [(1, 0, 0), (0, 1, 0)])
    out = build_from(src, tmp_path / "out.pdf", [1, 0])   # swapped; asked for [0, 1]
    doc = app.open_source_pdf(src)
    try:
        with pytest.raises(app.PdfOpenError):
            app.validate_page_selection_output(out, doc, [0, 1])
    finally:
        app.close_doc(doc)


def test_wrong_textless_page_is_rejected(tmp_path):
    src = textless_colored_pdf(tmp_path / "src.pdf", [(1, 0, 0), (0, 1, 0)])
    out = build_from(src, tmp_path / "out.pdf", [1, 1])   # asked for [0, 1]
    doc = app.open_source_pdf(src)
    try:
        with pytest.raises(app.PdfOpenError):
            app.validate_page_selection_output(out, doc, [0, 1])
    finally:
        app.close_doc(doc)


def test_correct_textless_selection_still_passes(tmp_path):
    src = textless_colored_pdf(tmp_path / "src.pdf", [(1, 0, 0), (0, 1, 0)])
    out = build_from(src, tmp_path / "out.pdf", [0, 1])
    doc = app.open_source_pdf(src)
    try:
        app.validate_page_selection_output(out, doc, [0, 1])   # must not raise
    finally:
        app.close_doc(doc)


# --------------------------------------------------------------------------- #
# N-07 (round 2) - a content stream references images/XObjects by NAME, so two
# pages can carry byte-identical streams ("/fzImg0 Do") while that name resolves
# to a different image. The fingerprint must fold in the resolved resources, not
# just the stream bytes, or a swapped/wrong page slips past.
# --------------------------------------------------------------------------- #

def image_xobject_pdf(path: Path, colors) -> Path:
    """Same-size pages each drawing ONE image through a resource reference. The
    content streams are byte-identical ('/fzImg0 Do'); only the image behind the
    name differs, so a content-stream-only fingerprint collides on them."""
    doc = pymupdf.open()
    for c in colors:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.set_rect(pix.irect, c)
        page = doc.new_page(width=100, height=100)
        page.insert_image(pymupdf.Rect(10, 10, 50, 50), stream=pix.tobytes("png"))
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()
    return path


def test_swapped_pages_sharing_a_resource_name_are_rejected(tmp_path):
    src = image_xobject_pdf(tmp_path / "src.pdf", [(255, 0, 0), (0, 255, 0)])
    out = build_from(src, tmp_path / "out.pdf", [1, 0])   # swapped; asked for [0, 1]
    doc = app.open_source_pdf(src)
    try:
        with pytest.raises(app.PdfOpenError):
            app.validate_page_selection_output(out, doc, [0, 1])
    finally:
        app.close_doc(doc)


def test_correct_image_xobject_selection_passes(tmp_path):
    """The identity must round-trip through insert_pdf: a correct extract of the
    same pages still validates (no false rejection)."""
    src = image_xobject_pdf(tmp_path / "src.pdf", [(255, 0, 0), (0, 255, 0)])
    out = build_from(src, tmp_path / "out.pdf", [0, 1])
    doc = app.open_source_pdf(src)
    try:
        app.validate_page_selection_output(out, doc, [0, 1])   # must not raise
    finally:
        app.close_doc(doc)


def test_page_order_mismatch_is_rejected(tmp_path):
    src = distinct_pdf(tmp_path / "src.pdf")
    out = build_from(src, tmp_path / "out.pdf", [4, 2, 0])   # reversed
    doc = app.open_source_pdf(src)
    try:
        with pytest.raises(app.PdfOpenError):
            app.validate_page_selection_output(out, doc, [0, 2, 4])
    finally:
        app.close_doc(doc)


def test_truncated_output_is_rejected(tmp_path):
    src = distinct_pdf(tmp_path / "src.pdf")
    out = build_from(src, tmp_path / "out.pdf", [0, 2])      # one page short
    doc = app.open_source_pdf(src)
    try:
        with pytest.raises(app.PdfOpenError) as excinfo:
            app.validate_page_selection_output(out, doc, [0, 2, 4])
    finally:
        app.close_doc(doc)
    assert "expected 3" in str(excinfo.value)


def test_extract_through_the_real_writer_validates(tmp_path):
    """The public writer must run the deep check itself."""
    src = distinct_pdf(tmp_path / "src.pdf")
    doc = app.open_source_pdf(src)
    try:
        out = tmp_path / "extract.pdf"
        app.write_pages_to_pdf(doc, [1, 3], out)
    finally:
        app.close_doc(doc)
    check = pymupdf.open(str(out))
    try:
        assert check.page_count == 2
        assert "PAGE NUMBER 2" in check[0].get_text()
        assert "PAGE NUMBER 4" in check[1].get_text()
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# Protection postconditions
# --------------------------------------------------------------------------- #

def test_protection_loss_is_detected(tmp_path):
    plain = distinct_pdf(tmp_path / "plain.pdf", 2)
    policy = app.ProtectionPolicy(kind="password", password="pw",
                                  permissions=app.all_permissions())
    with pytest.raises(app.PdfOpenError) as excinfo:
        app.validate_protection_postcondition(plain, policy)
    assert "protection was lost" in str(excinfo.value)


def test_unexpected_protection_is_detected(tmp_path):
    encrypted = tmp_path / "enc.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(encrypted), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw")
    doc.close()
    with pytest.raises(app.PdfOpenError) as excinfo:
        app.validate_protection_postcondition(encrypted, app.ProtectionPolicy(kind="none"))
    assert "unexpectedly requires a password" in str(excinfo.value)


def test_preserved_protection_passes(tmp_path):
    encrypted = tmp_path / "enc.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(encrypted), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw")
    doc.close()
    policy = app.ProtectionPolicy(kind="password", password="pw",
                                  permissions=app.all_permissions())
    app.validate_protection_postcondition(encrypted, policy)


# --------------------------------------------------------------------------- #
# Watermark postcondition
# --------------------------------------------------------------------------- #

def test_watermark_still_present_is_detected(tmp_path):
    """A no-op removal must not be reported as success."""
    import io

    from PIL import Image

    src = tmp_path / "stamped.pdf"
    doc = pymupdf.open()
    buf = io.BytesIO()
    Image.new("RGB", (100, 70), (200, 0, 0)).save(buf, "PNG")
    for _ in range(3):
        page = doc.new_page(width=300, height=400)
        page.insert_image(pymupdf.Rect(20, 20, 120, 90), stream=buf.getvalue())
    doc.save(str(src))
    doc.close()

    opened = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(opened)
        signature = candidates[0].signature
    finally:
        app.close_doc(opened)

    # The file still contains the watermark, so validation must fail.
    with pytest.raises(app.PdfOpenError) as excinfo:
        app.validate_watermark_removed(src, [signature])
    assert "still present" in str(excinfo.value)


def test_watermark_removal_passes_its_own_postcondition(tmp_path):
    import io

    from PIL import Image

    src = tmp_path / "stamped.pdf"
    doc = pymupdf.open()
    buf = io.BytesIO()
    Image.new("RGB", (100, 70), (0, 90, 200)).save(buf, "PNG")
    for _ in range(3):
        page = doc.new_page(width=300, height=400)
        page.insert_image(pymupdf.Rect(20, 20, 120, 90), stream=buf.getvalue())
    doc.save(str(src))
    doc.close()

    opened = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(opened)
        out = tmp_path / "clean.pdf"
        # remove_watermark_images validates internally; reaching here means the
        # postcondition held.
        modified = app.remove_watermark_images(opened, [candidates[0].signature], out)
    finally:
        app.close_doc(opened)
    assert modified.count == 3
    app.validate_watermark_removed(out, [candidates[0].signature])
