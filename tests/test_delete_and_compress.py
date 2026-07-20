# -*- coding: utf-8 -*-
"""Deleting pages and compressing, including source-integrity guarantees.

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
# Delete pages: parsing, deletion computation, naming, integrity
# --------------------------------------------------------------------------- #

def test_parse_delete_pages():
    assert app.parse_delete_pages("5") == [5]
    assert app.parse_delete_pages("3,1,2") == [1, 2, 3]           # sorted + unique
    assert app.parse_delete_pages("10-12,11") == [10, 11, 12]     # overlap dedup
    # No upper bound: large page numbers are allowed (checked per file later).
    assert app.parse_delete_pages("999") == [999]
    for bad in ["", "  ", "0", "-1", "abc", "1,,2", "20-10", "1-2-3"]:
        with pytest.raises(app.PageSelectionError):
            app.parse_delete_pages(bad)


def test_compute_deletion():
    present, missing, kept = app.compute_deletion(10, [2, 4, 99])
    assert present == [2, 4]
    assert missing == [99]
    assert kept == [0, 2, 4, 5, 6, 7, 8, 9]  # 0-based, pages 2 and 4 removed

    # Deleting everything leaves nothing to keep.
    present, missing, kept = app.compute_deletion(3, [1, 2, 3])
    assert present == [1, 2, 3] and missing == [] and kept == []

    # Nothing requested exists in the document.
    present, missing, kept = app.compute_deletion(3, [7, 8])
    assert present == [] and missing == [7, 8] and kept == [0, 1, 2]


def test_build_delete_output_name():
    assert app.build_delete_output_name("Doc", "10-20, 25") == "Doc_deleted_10-20_25.pdf"
    long_sel = ", ".join(str(i) for i in range(1, 80))
    assert app.build_delete_output_name("Doc", long_sel) == "Doc_pages_deleted.pdf"


def test_delete_pages_end_to_end(tmp_path):
    src = make_pdf(tmp_path / "doc.pdf", 6)
    original_hash = file_hash(src)
    reader = app.open_source_pdf(src)

    _present, _missing, kept = app.compute_deletion(6, [2, 5])  # delete pages 2 and 5
    out = tmp_path / "trimmed.pdf"
    written = app.write_pages_to_pdf(reader, kept, out).count

    assert written == 4
    assert len(PdfReader(str(out)).pages) == 4
    # Source untouched.
    assert file_hash(src) == original_hash


def test_remove_watermark_preserves_other_pages(tmp_path):
    # Two shared images: one on all 3 pages (watermark), one only on page 1.
    src = repeated_image_pdf(tmp_path / "wm.pdf", 3)
    doc = app.open_source_pdf(src)
    out = tmp_path / "clean.pdf"
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        app.remove_watermark_images(doc, [candidates[0].signature], out)
    finally:
        doc.close()
    # Output still opens and keeps its page count (no pages dropped).
    assert len(PdfReader(str(out)).pages) == 3


def test_remove_watermark_keeps_text(tmp_path):
    # A watermark image stamped over real text must vanish without harming text.
    import pymupdf
    from PIL import Image

    stamp = tmp_path / "stamp.png"
    Image.new("RGB", (200, 60), (0, 120, 255)).save(stamp)
    src = tmp_path / "stamped.pdf"
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page(width=400, height=300)
        page.insert_text((50, 120), "Confidential report body text stays here.")
        page.insert_image(pymupdf.Rect(40, 90, 340, 180), filename=str(stamp))
    doc.save(str(src))
    doc.close()

    doc = app.open_source_pdf(src)
    out = tmp_path / "unstamped.pdf"
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        assert len(candidates) >= 1
        app.remove_watermark_images(doc, [candidates[0].signature], out)
    finally:
        doc.close()

    check = pymupdf.open(str(out))
    try:
        assert check.page_count == 3
        # The watermark image is gone but the underlying text survives.
        # Only transparent placeholders may remain; no visible image survives.
        visible = [
            info for info in check[0].get_images(full=True)
            if info[2] > 8 and info[3] > 8
        ]
        assert visible == [], f"a visible image survived removal: {visible}"
        assert "Confidential report body text" in check[0].get_text()
    finally:
        check.close()


def test_remove_watermark_keeps_other_overlapping_image(tmp_path):
    # A watermark stamped ON TOP of a full-page illustration must be removed
    # without taking the illustration with it (regression: redaction removed
    # any image touching the watermark's box).
    import pymupdf
    from PIL import Image

    wm = tmp_path / "wm.png"        # small repeated watermark
    Image.new("RGB", (120, 80), (0, 90, 200)).save(wm)
    illo = tmp_path / "illo.png"    # unique full-page illustration
    Image.new("RGB", (600, 800), (30, 160, 60)).save(illo)

    src = tmp_path / "book.pdf"
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page(width=300, height=400)
        page.insert_image(page.rect, filename=str(illo))            # full page
        page.insert_image(pymupdf.Rect(90, 150, 210, 230), filename=str(wm))  # on top
    doc.save(str(src))
    doc.close()

    doc = app.open_source_pdf(src)
    out = tmp_path / "clean.pdf"
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        # The watermark repeats on all 3 pages; the illustration is unique.
        wm_sig = next(c.signature for c in candidates if c.width == 120)
        app.remove_watermark_images(doc, [wm_sig], out)
    finally:
        doc.close()

    check = pymupdf.open(str(out))
    try:
        # Exactly one image (the illustration) survives on each page.
        sizes = sorted((im[2], im[3]) for im in check[0].get_images(full=True))
        # The overlapping non-target image must survive at full size; only
        # transparent placeholders may remain alongside it.
        real_sizes = [size for size in sizes if size[0] > 8 and size[1] > 8]
        assert real_sizes == [(600, 800)], sizes
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# Compress PDF: presets, lossy shrink, lossless mode, source integrity
# --------------------------------------------------------------------------- #

def make_photo_pdf(path: Path, pages: int = 2, size=(1600, 1200)) -> Path:
    """Create an image-heavy PDF (a smooth gradient photo on every page)."""
    from PIL import Image

    images = []
    for _ in range(pages):
        img = Image.new("RGB", size)
        px = img.load()
        for x in range(0, size[0], 4):
            for y in range(0, size[1], 4):
                c = (x * 255 // size[0], y * 255 // size[1], 128)
                for dx in range(4):
                    for dy in range(4):
                        px[x + dx, y + dy] = c
        images.append(img)
    # Pillow embeds RGB images losslessly by default via its PDF writer.
    images[0].save(path, "PDF", save_all=True, append_images=images[1:],
                   resolution=150.0)
    return path


def test_compression_presets_shape():
    # Six named levels; ultra is the lossless-only sentinel.
    assert set(app.COMPRESSION_PRESETS) == {
        "very low", "low", "medium", "high", "very high", "ultra"
    }
    assert app.COMPRESSION_PRESETS["ultra"] is None
    # Lossy presets are (jpeg_quality, dpi_target) with sane ordering.
    q_low, dpi_low = app.COMPRESSION_PRESETS["very low"]
    q_high, dpi_high = app.COMPRESSION_PRESETS["very high"]
    assert q_low < q_high and dpi_low < dpi_high


def test_format_size():
    assert app._format_size(512) == "512 B"
    assert app._format_size(2048) == "2.0 KB"
    assert app._format_size(3 * 1024 * 1024) == "3.00 MB"


def test_compress_pdf_lossy_shrinks_image_pdf(tmp_path):
    src = make_photo_pdf(tmp_path / "photos.pdf")
    original_hash = file_hash(src)
    out = tmp_path / "photos_compressed.pdf"

    quality, dpi = app.COMPRESSION_PRESETS["medium"]
    stats = app.compress_pdf(src, out, quality, dpi)

    assert out.exists()
    assert stats["pages"] == 2
    assert stats["new_size"] < stats["original_size"]
    assert len(PdfReader(str(out)).pages) == 2
    # Source must remain byte-for-byte unchanged.
    assert file_hash(src) == original_hash


def test_compress_pdf_ultra_lossless_valid_output(tmp_path):
    src = make_pdf(tmp_path / "text.pdf", 5)
    out = tmp_path / "text_compressed.pdf"

    stats = app.compress_pdf(src, out, None, None)  # ultra: lossless only

    assert out.exists()
    assert stats["pages"] == 5
    assert len(PdfReader(str(out)).pages) == 5


def test_scan_image_dpi_stats(tmp_path):
    # Image-heavy PDF embedded at 150 DPI -> stats reflect that resolution.
    src = make_photo_pdf(tmp_path / "photos.pdf")
    doc = app.open_source_pdf(src)
    try:
        stats = app.scan_image_dpi_stats(doc)
        assert stats is not None
        assert stats["count"] == 2
        assert 140 <= stats["median"] <= 160
        assert 140 <= stats["max"] <= 160
    finally:
        doc.close()

    # Blank text-free PDF has no raster images at all.
    blank = make_pdf(tmp_path / "blank.pdf", 2)
    doc = app.open_source_pdf(blank)
    try:
        assert app.scan_image_dpi_stats(doc) is None
    finally:
        doc.close()


def test_has_meaningful_text(tmp_path):
    import pymupdf

    # A real text page is detected as text content.
    text_pdf = tmp_path / "text.pdf"
    new_doc = pymupdf.open()
    page = new_doc.new_page()
    page.insert_text((72, 100), "A real paragraph of extractable text for testing.")
    new_doc.save(str(text_pdf))
    new_doc.close()
    doc = app.open_source_pdf(text_pdf)
    try:
        assert app.has_meaningful_text(doc) is True
    finally:
        doc.close()

    # An image-only PDF has no extractable text.
    photo = make_photo_pdf(tmp_path / "photos.pdf")
    doc = app.open_source_pdf(photo)
    try:
        assert app.has_meaningful_text(doc) is False
    finally:
        doc.close()


def _make_hires_image_pdf(path, pixels=900, box_pt=200):
    """One page with a single high-resolution image (~324 DPI by default)."""
    import pymupdf
    from PIL import Image

    stamp = path.with_suffix(".png")
    Image.new("RGB", (pixels, pixels), (200, 50, 50)).save(stamp)
    doc = pymupdf.open()
    page = doc.new_page(width=box_pt + 40, height=box_pt + 40)
    page.insert_image(pymupdf.Rect(20, 20, 20 + box_pt, 20 + box_pt),
                      filename=str(stamp))
    doc.save(str(path))
    doc.close()
    stamp.unlink()
    return path


def test_compress_never_upscales_and_caps_high_dpi(tmp_path):
    import pymupdf

    src = _make_hires_image_pdf(tmp_path / "hi.pdf")  # ~324 DPI
    doc = pymupdf.open(str(src))
    before_px = doc[0].get_images(full=True)[0][2]
    doc.close()

    # Cap well below the source: the image must shrink, never grow.
    out = tmp_path / "hi_low.pdf"
    app.compress_pdf(src, out, jpeg_quality=75, dpi_target=96)
    doc = pymupdf.open(str(out))
    after_px = doc[0].get_images(full=True)[0][2]
    doc.close()
    assert after_px < before_px      # downsampled
    assert after_px <= before_px     # never upscaled (by definition here)


def test_compress_leaves_low_dpi_image_alone(tmp_path):
    import pymupdf

    # A 150-DPI photo compressed with a 300 cap: resolution must not change.
    src = make_photo_pdf(tmp_path / "photo.pdf")
    doc = pymupdf.open(str(src))
    before_px = sorted(im[2] for im in doc[0].get_images(full=True))
    doc.close()

    out = tmp_path / "photo_c.pdf"
    app.compress_pdf(src, out, jpeg_quality=90, dpi_target=300)
    doc = pymupdf.open(str(out))
    after_px = sorted(im[2] for im in doc[0].get_images(full=True))
    doc.close()
    assert after_px == before_px     # cap above source DPI -> no resolution change


def test_folder_dpi_stats(tmp_path):
    make_photo_pdf(tmp_path / "a.pdf")           # 150-DPI images
    make_pdf(tmp_path / "b.pdf", 2)              # text/vector, no images
    stats = app._folder_dpi_stats([tmp_path / "a.pdf", tmp_path / "b.pdf"])
    assert stats is not None
    assert stats["files_with_images"] == 1
    assert stats["files_text_only"] == 1
    assert 140 <= stats["max"] <= 160

    # A15: a folder of only text PDFs yields no image-DPI keys but still reports
    # the honest per-file buckets (no false "no images" when nothing was skipped).
    make_pdf(tmp_path / "c.pdf", 1)
    text_only = app._folder_dpi_stats([tmp_path / "b.pdf", tmp_path / "c.pdf"])
    assert "max" not in text_only
    assert text_only["files_text_only"] == 2
    assert text_only["files_with_images"] == 0
    assert text_only["files_not_scanned"] == 0
