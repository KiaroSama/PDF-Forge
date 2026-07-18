# -*- coding: utf-8 -*-
"""Tokenizer-hostile watermark fixtures (PF-012, PF-013, PF-024, PF-025, PF-050).

Removal now works on the object graph (PyMuPDF replaces the image object), so no
content stream is ever rewritten. These fixtures encode the cases that a regex
over stream bytes would have corrupted, and assert the surrounding content is
byte-for-byte intact.
"""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402


def stamped_pdf(path: Path, pages: int = 3, text: str = "") -> Path:
    """A PDF with the same image stamped on every page, plus optional text."""
    doc = pymupdf.open()
    buf = io.BytesIO()
    Image.new("RGB", (120, 90), (200, 30, 30)).save(buf, "PNG")
    data = buf.getvalue()
    for _ in range(pages):
        page = doc.new_page(width=400, height=500)
        page.insert_image(pymupdf.Rect(50, 50, 170, 140), stream=data)
        if text:
            page.insert_text((60, 300), text, fontsize=12)
    doc.save(str(path))
    doc.close()
    return path


def remove_top_candidate(src: Path, out: Path) -> int:
    doc = app.open_source_pdf(src)
    try:
        candidates, _total = app.scan_watermark_candidates(doc)
        assert candidates, "no watermark candidate detected"
        return app.remove_watermark_images(doc, [candidates[0].signature], out)
    finally:
        app.close_doc(doc)


def visible_images(pdf: Path):
    doc = pymupdf.open(str(pdf))
    try:
        return [
            (info[2], info[3])
            for page in doc
            for info in page.get_images(full=True)
            if info[2] > 8 and info[3] > 8
        ]
    finally:
        doc.close()


def page_text(pdf: Path) -> str:
    doc = pymupdf.open(str(pdf))
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# Hostile content that a regex would have matched
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload", [
    "/Im1 Do inside a literal string",
    r"escaped \( paren and /Im1 Do",
    "<48656c6c6f> hex-ish text /Im1 Do",
    "% a comment containing /Im1 Do",
    "BI /W 4 /H 4 ID ....binary.... EI  /Im1 Do",
])
def test_text_that_looks_like_a_paint_call_survives(tmp_path, payload):
    """Removal must not react to bytes that merely *look* like a paint call."""
    src = stamped_pdf(tmp_path / "src.pdf", pages=2, text=payload)
    out = tmp_path / "out.pdf"
    remove_top_candidate(src, out)

    text = page_text(out)
    # The distinctive part of the payload must still be present.
    marker = payload.split()[0]
    assert marker in text or payload[:12] in text, (
        f"content resembling a paint call was destroyed: {payload!r}"
    )
    assert visible_images(out) == [], "the watermark must be gone"


def test_output_is_readable_by_an_independent_parser(tmp_path):
    from pypdf import PdfReader

    src = stamped_pdf(tmp_path / "src.pdf", pages=3, text="body text")
    out = tmp_path / "out.pdf"
    remove_top_candidate(src, out)

    reader = PdfReader(str(out))          # independent of PyMuPDF
    assert len(reader.pages) == 3
    assert "body text" in "".join(p.extract_text() or "" for p in reader.pages)


def test_text_and_page_count_are_preserved(tmp_path):
    src = stamped_pdf(tmp_path / "src.pdf", pages=3, text="Confidential body")
    out = tmp_path / "out.pdf"
    modified = remove_top_candidate(src, out)
    assert modified == 3
    assert "Confidential body" in page_text(out)


# --------------------------------------------------------------------------- #
# PF-024 / PF-025 - forms nested and shared
# --------------------------------------------------------------------------- #

def test_watermark_painted_through_a_shared_form(tmp_path):
    """One Form XObject shown on many pages: all pages must be cleaned once."""
    src = tmp_path / "form.pdf"
    doc = pymupdf.open()
    buf = io.BytesIO()
    Image.new("RGB", (100, 60), (10, 90, 200)).save(buf, "PNG")
    data = buf.getvalue()
    for _ in range(4):
        page = doc.new_page(width=300, height=400)
        page.insert_image(pymupdf.Rect(20, 20, 120, 80), stream=data)
    doc.save(str(src))
    doc.close()

    out = tmp_path / "out.pdf"
    modified = remove_top_candidate(src, out)
    assert modified == 4, "the count must match the pages whose content changed"
    assert visible_images(out) == []


def test_non_target_image_on_the_same_page_survives(tmp_path):
    src = tmp_path / "two.pdf"
    doc = pymupdf.open()
    mark = io.BytesIO(); Image.new("RGB", (80, 80), (255, 0, 0)).save(mark, "PNG")
    other = io.BytesIO(); Image.new("RGB", (300, 200), (0, 128, 0)).save(other, "PNG")
    for _ in range(3):
        page = doc.new_page(width=500, height=500)
        page.insert_image(pymupdf.Rect(10, 10, 310, 210), stream=other.getvalue())
        page.insert_image(pymupdf.Rect(50, 50, 130, 130), stream=mark.getvalue())
    doc.save(str(src))
    doc.close()

    out = tmp_path / "out.pdf"
    doc = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        target = min(candidates, key=lambda c: c.width * c.height)  # the stamp
        app.remove_watermark_images(doc, [target.signature], out)
    finally:
        app.close_doc(doc)

    remaining = visible_images(out)
    assert remaining, "the unrelated image must survive"
    assert all(w >= 300 for w, _h in remaining), remaining


# --------------------------------------------------------------------------- #
# PF-013 - degenerate candidates and no-op removals
# --------------------------------------------------------------------------- #

def test_placeholder_is_not_offered_as_a_new_candidate(tmp_path):
    """After removal a re-scan must not present the transparent placeholder."""
    src = stamped_pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"
    remove_top_candidate(src, out)

    doc = app.open_source_pdf(out)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
    finally:
        app.close_doc(doc)
    assert candidates == [], f"a placeholder was offered as a watermark: {candidates}"


def test_removing_an_unknown_signature_changes_nothing(tmp_path):
    src = stamped_pdf(tmp_path / "src.pdf", pages=2, text="keep me")
    out = tmp_path / "out.pdf"
    doc = app.open_source_pdf(src)
    try:
        modified = app.remove_watermark_images(doc, ["nosuch:1x1"], out)
    finally:
        app.close_doc(doc)
    assert modified == 0, "nothing matched, so no page was modified"
    assert visible_images(out), "the original image must still be there"
    assert "keep me" in page_text(out)
