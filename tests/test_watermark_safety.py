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
from helpers import rgb_png, stamped_pdf  # noqa: E402


def remove_top_candidate(src: Path, out: Path):
    """Remove the top candidate; returns the writer's ``OutputResult``."""
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
    src = stamped_pdf(tmp_path / "src.pdf", pages=2, text=payload, fontsize=12)
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

    src = stamped_pdf(tmp_path / "src.pdf", pages=3, text="body text", fontsize=12)
    out = tmp_path / "out.pdf"
    remove_top_candidate(src, out)

    reader = PdfReader(str(out))          # independent of PyMuPDF
    assert len(reader.pages) == 3
    assert "body text" in "".join(p.extract_text() or "" for p in reader.pages)


def test_text_and_page_count_are_preserved(tmp_path):
    src = stamped_pdf(tmp_path / "src.pdf", pages=3, text="Confidential body",
                      fontsize=12)
    out = tmp_path / "out.pdf"
    result = remove_top_candidate(src, out)
    assert result.count == 3
    assert "Confidential body" in page_text(out)


# --------------------------------------------------------------------------- #
# PF-024 / PF-025 - forms nested and shared
# --------------------------------------------------------------------------- #

def test_watermark_painted_through_a_shared_form(tmp_path):
    """One Form XObject, nested and shared: every page must be cleaned once.

    The image is reached only through ``page -> form -> form -> image``, and the
    inner forms are the *same* objects on all four pages. A per-page copy would
    make this pass by accident, so the fixture asserts both properties before
    exercising removal.
    """
    data = rgb_png(size=(80, 40), color=(10, 90, 200))

    inner = pymupdf.open()                       # the image itself
    inner.new_page(width=80, height=40).insert_image(
        pymupdf.Rect(0, 0, 80, 40), stream=data)
    inner_path = tmp_path / "inner.pdf"
    inner.save(str(inner_path))
    inner.close()

    inner_doc = pymupdf.open(str(inner_path))    # form #1 wraps the image
    middle = pymupdf.open()
    middle.new_page(width=100, height=60).show_pdf_page(
        pymupdf.Rect(0, 0, 80, 40), inner_doc, 0)
    middle_path = tmp_path / "middle.pdf"
    middle.save(str(middle_path))
    middle.close()
    inner_doc.close()

    middle_doc = pymupdf.open(str(middle_path))  # form #2 wraps form #1
    doc = pymupdf.open()
    for _ in range(4):
        page = doc.new_page(width=300, height=400)
        page.insert_text((20, 350), "keep this text")
        page.show_pdf_page(pymupdf.Rect(20, 20, 120, 80), middle_doc, 0)
    src = tmp_path / "form.pdf"
    doc.save(str(src))
    doc.close()
    middle_doc.close()

    check = pymupdf.open(str(src))
    try:
        per_page = [{entry[0] for entry in page.get_xobjects()} for page in check]
        assert all(len(x) >= 2 for x in per_page), \
            f"the fixture must nest Form XObjects: {per_page}"
        shared = set.intersection(*per_page)
        assert shared, f"no Form XObject is shared between pages: {per_page}"
    finally:
        check.close()

    out = tmp_path / "out.pdf"
    result = remove_top_candidate(src, out)
    assert result.count == 4, "the count must match the pages whose content changed"
    assert visible_images(out) == []
    assert "keep this text" in page_text(out)


def test_non_target_image_on_the_same_page_survives(tmp_path):
    src = tmp_path / "two.pdf"
    doc = pymupdf.open()
    mark = io.BytesIO()
    Image.new("RGB", (80, 80), (255, 0, 0)).save(mark, "PNG")
    other = io.BytesIO()
    Image.new("RGB", (300, 200), (0, 128, 0)).save(other, "PNG")
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
    src = stamped_pdf(tmp_path / "src.pdf", pages=3, text="")
    out = tmp_path / "out.pdf"
    remove_top_candidate(src, out)

    doc = app.open_source_pdf(out)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
    finally:
        app.close_doc(doc)
    assert candidates == [], f"a placeholder was offered as a watermark: {candidates}"


def test_removing_an_unknown_signature_writes_nothing(tmp_path):
    """A zero-match run is a no-op, and a no-op must not produce a file.

    The earlier version of this test asserted the opposite - that an output
    existed and was readable - which is exactly the defect: a run that removed
    nothing handed the user a file at their chosen path (C-14).
    """
    src = stamped_pdf(tmp_path / "src.pdf", pages=2, text="keep me", fontsize=12)
    out = tmp_path / "out.pdf"
    doc = app.open_source_pdf(src)
    try:
        with pytest.raises(ValueError):
            app.remove_watermark_images(doc, ["nosuch:1x1"], out)
    finally:
        app.close_doc(doc)
    assert not out.exists(), "a no-op must not leave a file at the chosen path"
    assert list(tmp_path.glob("*.pdf")) == [src], "a no-op must write nothing"
