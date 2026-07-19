# -*- coding: utf-8 -*-
"""Image regressions: image identity, painted occurrences, form XObjects,
and soft-mask/transparency handling.

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
# A4 / A7 / A8 / A9 - image identity, painted occurrences, form xobjects
# --------------------------------------------------------------------------- #

def test_different_images_are_not_grouped(tmp_path):
    doc = pymupdf.open()
    red, blue = rgb_png(color=(255, 0, 0)), rgb_png(color=(0, 0, 255))
    for _ in range(3):
        page = doc.new_page(width=200, height=200)
        page.insert_image(pymupdf.Rect(10, 10, 60, 60), stream=red)
        page.insert_image(pymupdf.Rect(80, 10, 130, 60), stream=blue)
    src = tmp_path / "two.pdf"
    doc.save(str(src))
    doc.close()

    doc = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        assert len(candidates) == 2, "distinct images must not share an identity"
        assert len({c.signature for c in candidates}) == 2
    finally:
        app.close_doc(doc)


def test_same_image_across_pages_groups_once(tmp_path):
    src = repeated_image_pdf(tmp_path / "wm.pdf", 4)
    doc = app.open_source_pdf(src)
    try:
        candidates, total = app.scan_watermark_candidates(doc)
        assert total == 4
        assert candidates and len(candidates[0].pages) == 4
        assert len(candidates[0].pages) >= 4
        assert candidates[0].sample_xref > 0
    finally:
        app.close_doc(doc)


def test_watermark_in_form_xobject_detected_and_removed(tmp_path):
    stamp = pymupdf.open()
    page = stamp.new_page(width=80, height=40)
    page.insert_image(pymupdf.Rect(0, 0, 80, 40), stream=rgb_png(size=(80, 40)))
    stamp_path = tmp_path / "stamp.pdf"
    stamp.save(str(stamp_path))
    stamp.close()

    stamp_doc = pymupdf.open(str(stamp_path))
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page(width=300, height=300)
        page.insert_text((20, 200), "keep this text")
        page.show_pdf_page(pymupdf.Rect(20, 20, 100, 60), stamp_doc, 0)
    src = tmp_path / "formwm.pdf"
    doc.save(str(src))
    doc.close()
    stamp_doc.close()

    doc = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        assert candidates, "a watermark painted via a Form XObject must be found"
        target = candidates[0]
        assert len(target.pages) == 3
        out = tmp_path / "clean.pdf"
        modified = app.remove_watermark_images(doc, [target.signature], out)
        assert modified > 0, "modified==0 would be a misleading success message"
    finally:
        app.close_doc(doc)

    check = app.open_source_pdf(out)
    try:
        after, _ = app.scan_watermark_candidates(check, min_pages=2)
        assert target.signature not in {c.signature for c in after}
        assert "keep this text" in check[0].get_text()
    finally:
        app.close_doc(check)


def test_extract_dedupes_identical_content_across_xrefs(tmp_path):
    png = rgb_png(size=(60, 60), color=(10, 120, 200))
    parts = []
    for name in ("one.pdf", "two.pdf"):
        d = pymupdf.open()
        p = d.new_page(width=200, height=200)
        p.insert_image(pymupdf.Rect(10, 10, 70, 70), stream=png)
        path = tmp_path / name
        d.save(str(path))
        d.close()
        parts.append(path)

    merged = pymupdf.open()
    for path in parts:
        piece = pymupdf.open(str(path))
        merged.insert_pdf(piece)
        piece.close()
    src = tmp_path / "merged.pdf"
    merged.save(str(src))
    merged.close()

    doc = app.open_source_pdf(src)
    try:
        xrefs = set()
        for page in doc:
            for entry in page.get_images(full=True):
                xrefs.add(entry[0])
        assert len(xrefs) >= 2, "fixture must hold the image under two xrefs"
        assert app.count_embedded_images(doc) == 1
        created = app.extract_embedded_images(doc, tmp_path / "imgs")
        assert len(created) == 1, "identical content must be extracted once"
    finally:
        app.close_doc(doc)


# --------------------------------------------------------------------------- #
# A10 - soft mask / transparency
# --------------------------------------------------------------------------- #

def transparent_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.insert_image(pymupdf.Rect(10, 10, 110, 110), stream=rgba_png())
    doc.save(str(path))
    doc.close()
    return path


def test_original_mode_rebuilds_alpha_from_soft_mask(tmp_path):
    src = transparent_pdf(tmp_path / "alpha.pdf")
    doc = app.open_source_pdf(src)
    try:
        created = app.extract_embedded_images(doc, tmp_path / "orig", jpeg_quality=None)
        assert created, "no image extracted"
        with Image.open(created[0]) as img:
            assert img.mode in ("RGBA", "LA"), "transparency must survive"
            assert img.getchannel("A").getextrema()[0] == 0, "alpha channel lost"
    finally:
        app.close_doc(doc)


def test_jpeg_mode_composites_soft_mask(tmp_path):
    src = transparent_pdf(tmp_path / "alpha.pdf")
    doc = app.open_source_pdf(src)
    try:
        created = app.extract_embedded_images(doc, tmp_path / "jpg", jpeg_quality=85)
        assert created and created[0].suffix == ".jpg"
        with Image.open(created[0]) as img:
            assert img.mode == "RGB"     # JPEG cannot carry alpha
    finally:
        app.close_doc(doc)
