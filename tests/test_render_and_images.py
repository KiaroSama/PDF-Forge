# -*- coding: utf-8 -*-
"""Page rendering, image-only PDFs, watermark removal, embedded-image extraction.

Split out of the former single test_pdf_forge module. Tests use temporary
directories and generated small PDFs only; they never touch real user files.
"""

import sys
from pathlib import Path

import pytest  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402,F401
from helpers import file_hash, make_pdf, repeated_image_pdf  # noqa: E402,F401
from pypdf import PdfReader, PdfWriter  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# Image conversion: quality mapping, naming, PNG rendering, image-only PDF
# --------------------------------------------------------------------------- #

def test_build_page_image_name():
    # Page number is used verbatim, with no zero-padding.
    assert app.build_page_image_name(2) == "2.png"
    assert app.build_page_image_name(10) == "10.png"


def test_default_image_output_names(tmp_path):
    src = tmp_path / "Report.pdf"
    assert app.default_images_output_dir(src) == tmp_path / "Report_images"
    assert app.default_image_pdf_output(src) == tmp_path / "Report_image.pdf"


def test_render_all_pages_to_pngs(tmp_path):
    from PIL import Image

    src = make_pdf(tmp_path / "doc.pdf", 3)
    pdf, n = app.open_render_document(src)
    try:
        assert n == 3
        created = app.render_pages_to_pngs(pdf, list(range(n)), tmp_path / "imgs", dpi=96)
    finally:
        pdf.close()

    assert sorted(p.name for p in created) == ["1.png", "2.png", "3.png"]
    for path in created:
        with Image.open(path) as im:
            im.verify()  # Confirms each PNG is valid and not truncated.


def test_render_selected_pages_named_by_page_number(tmp_path):
    src = make_pdf(tmp_path / "doc.pdf", 12)
    pdf, n = app.open_render_document(src)
    try:
        # Pages 2 and 10 (0-based indices 1 and 9).
        created = app.render_pages_to_pngs(pdf, [1, 9], tmp_path / "imgs", dpi=96)
    finally:
        pdf.close()
    # Each file is named after its own page number.
    assert sorted(p.name for p in created) == ["10.png", "2.png"]


def test_render_pdf_to_image_pdf(tmp_path):
    src = make_pdf(tmp_path / "doc.pdf", 4)
    original_hash = file_hash(src)
    pdf, n = app.open_render_document(src)
    out = tmp_path / "image_only.pdf"
    try:
        written = app.render_pdf_to_image_pdf(pdf, n, out, dpi=96)
    finally:
        pdf.close()

    assert written == 4
    reader = PdfReader(str(out))
    assert len(reader.pages) == 4
    assert reader.is_encrypted is False
    # The rasterized output has no extractable text (non-editable).
    assert reader.pages[0].extract_text().strip() == ""
    # Source is untouched.
    assert file_hash(src) == original_hash


def test_png_temp_cleanup_on_failure(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "doc.pdf", 2)
    pdf, _n = app.open_render_document(src)
    out_dir = tmp_path / "imgs"

    def boom(*_args, **_kwargs):
        raise app.PdfOpenError("simulated image validation failure")

    monkeypatch.setattr(app.render, "_validate_image_file", boom)
    try:
        with pytest.raises(app.PdfOpenError):
            app.render_pages_to_pngs(pdf, [0], out_dir, dpi=96)
    finally:
        pdf.close()

    # No final image and no leftover temp files.
    assert list(out_dir.glob("*.png")) == []
    assert list(out_dir.glob(".pdfforge_*")) == []


def test_image_conversion_unicode_persian_paths(tmp_path):
    folder = tmp_path / "اسناد"  # "documents"
    folder.mkdir()
    src = make_pdf(folder / "گزارش.pdf", 2)  # "report"
    pdf, n = app.open_render_document(src)
    try:
        created = app.render_pages_to_pngs(pdf, list(range(n)), folder / "تصاویر", dpi=96)
    finally:
        pdf.close()
    assert sorted(p.name for p in created) == ["1.png", "2.png"]


# --------------------------------------------------------------------------- #
# Watermark removal: index parsing, candidate scanning, removal
# --------------------------------------------------------------------------- #

def test_parse_index_list():
    assert app.parse_index_list("1", 3) == [1]
    assert app.parse_index_list("1,3", 3) == [1, 3]
    assert app.parse_index_list("3,1,3", 3) == [1, 3]  # dedup + sort
    for bad in ["", "   ", "0", "4", "abc", "1,,2", "-1", "1.5"]:
        with pytest.raises(ValueError):
            app.parse_index_list(bad, 3)


def test_scan_watermark_candidates(tmp_path):
    src = repeated_image_pdf(tmp_path / "wm.pdf", 3)
    doc = app.open_source_pdf(src)
    try:
        candidates, total = app.scan_watermark_candidates(doc)
    finally:
        doc.close()
    assert total == 3
    assert len(candidates) >= 1
    top = candidates[0]
    assert top.pages == {1, 2, 3}          # image repeats on every page
    assert (top.width, top.height) == (120, 80)


def test_remove_watermark_images(tmp_path):
    src = repeated_image_pdf(tmp_path / "wm.pdf", 3)
    doc = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        target_sig = candidates[0].signature
        out = tmp_path / "clean.pdf"
        modified = app.remove_watermark_images(doc, [target_sig], out)
    finally:
        doc.close()

    assert modified.count == 3
    result = PdfReader(str(out))
    assert len(result.pages) == 3
    assert result.is_encrypted is False
    # The repeated image is gone: rescanning finds no repeated image at all.
    check = app.open_source_pdf(out)
    try:
        candidates_after, _ = app.scan_watermark_candidates(check, min_pages=2)
    finally:
        check.close()
    assert candidates_after == []


# --------------------------------------------------------------------------- #
# Extract embedded images
# --------------------------------------------------------------------------- #

def _make_two_image_pdf(tmp_path):
    """Two pages: a unique photo on page 1, a repeated logo on both pages."""
    import pymupdf
    from PIL import Image

    photo = tmp_path / "photo.png"
    Image.new("RGB", (400, 300), (180, 40, 40)).save(photo)
    logo = tmp_path / "logo.png"
    Image.new("RGB", (100, 50), (40, 40, 180)).save(logo)

    src = tmp_path / "twoimg.pdf"
    doc = pymupdf.open()
    for page_no in range(2):
        page = doc.new_page(width=500, height=400)
        if page_no == 0:
            page.insert_image(pymupdf.Rect(50, 50, 450, 350), filename=str(photo))
        page.insert_image(pymupdf.Rect(10, 10, 110, 60), filename=str(logo))
    doc.save(str(src))
    doc.close()
    return src


def test_extract_images_original_dedupes(tmp_path):
    src = _make_two_image_pdf(tmp_path)
    doc = app.open_source_pdf(src)
    out_dir = tmp_path / "out"
    try:
        assert app.count_embedded_images(doc) == 2  # logo deduped across pages
        created = app.extract_embedded_images(doc, out_dir, jpeg_quality=None)
    finally:
        doc.close()

    assert len(created) == 2
    for path in created:
        assert path.stat().st_size > 0
    # Named after the first page each image appears on.
    assert all(p.name.startswith("p1_") for p in created)


def test_extract_images_jpeg_reencode(tmp_path):
    from PIL import Image

    src = _make_two_image_pdf(tmp_path)
    doc = app.open_source_pdf(src)
    out_dir = tmp_path / "out"
    try:
        created = app.extract_embedded_images(doc, out_dir, jpeg_quality=75)
    finally:
        doc.close()

    assert len(created) == 2
    for path in created:
        assert path.suffix == ".jpg"
        with Image.open(path) as im:
            im.verify()


def test_extract_images_jpeg_handles_alpha(tmp_path):
    # An embedded RGBA image (alpha) must re-encode to JPEG without errors
    # (regression: 'jpg' cannot have alpha).
    import pymupdf
    from PIL import Image

    rgba = tmp_path / "rgba.png"
    Image.new("RGBA", (200, 100), (255, 0, 0, 128)).save(rgba)
    src = tmp_path / "alpha.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_image(pymupdf.Rect(20, 20, 220, 120), filename=str(rgba))
    doc.save(str(src))
    doc.close()

    doc = app.open_source_pdf(src)
    try:
        created = app.extract_embedded_images(doc, tmp_path / "out", jpeg_quality=80)
    finally:
        doc.close()
    assert len(created) == 1
    assert created[0].suffix == ".jpg"


def test_extract_images_none_in_text_pdf(tmp_path):
    src = make_pdf(tmp_path / "text.pdf", 3)
    doc = app.open_source_pdf(src)
    try:
        assert app.count_embedded_images(doc) == 0
    finally:
        doc.close()
