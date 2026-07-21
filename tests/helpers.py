# -*- coding: utf-8 -*-
"""Fixture builders shared by the test modules.

Everything here generates throwaway files in a temporary directory; nothing
touches real user data. Kept in one module so the themed test files stay small
and a helper is fixed in exactly one place.
"""
from __future__ import annotations

import hashlib
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402
from pypdf import PdfWriter  # noqa: E402

__all__ = [
    "make_pdf", "make_encrypted", "file_hash", "rgb_png", "rgba_png",
    "repeated_image_pdf", "stamped_pdf", "label_of", "zip_ooxml",
]


def make_pdf(path: Path, pages: int) -> Path:
    """A small valid PDF with the requested number of blank pages."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def make_encrypted(path: Path, pages: int = 2, user_pw="pw", owner_pw="pw",
                   permissions=None) -> Path:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    kwargs = dict(encryption=pymupdf.PDF_ENCRYPT_AES_256)
    if user_pw:
        kwargs["user_pw"] = user_pw
    if owner_pw:
        kwargs["owner_pw"] = owner_pw
    if permissions is not None:
        kwargs["permissions"] = int(permissions)
    doc.save(str(path), **kwargs)
    doc.close()
    return path


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rgb_png(size=(40, 40), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def rgba_png(size=(50, 50)) -> bytes:
    img = Image.new("RGBA", size, (255, 0, 0, 0))
    for x in range(size[0] // 2):
        for y in range(size[1]):
            img.putpixel((x, y), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def repeated_image_pdf(path: Path, pages: int, size=(120, 80),
                       color=(200, 0, 0)) -> Path:
    """A PDF carrying the same image on every page (a stand-in watermark)."""
    images = [Image.new("RGB", size, color) for _ in range(pages)]
    images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    return path


def stamped_pdf(path: Path, pages: int = 3, text: str = "body text",
                pad: int = 0, fontsize=None, **save_kwargs) -> Path:
    """A 400x500-page PDF with the same 120x90 red image stamped on every page.

    ``text`` is written on each page when non-empty; ``fontsize`` overrides the
    default face size only when given (so callers that never set it keep the
    library default byte-for-byte). ``pad`` appends that many bytes of trailing
    PDF comment after ``%%EOF`` - ignored by every reader, so the file stays
    valid while giving a test a region it can rewrite in place. ``save_kwargs``
    flow straight to ``Document.save`` for encrypted / owner-restricted
    fixtures.
    """
    doc = pymupdf.open()
    data = rgb_png(size=(120, 90), color=(200, 30, 30))
    for _ in range(pages):
        page = doc.new_page(width=400, height=500)
        page.insert_image(pymupdf.Rect(50, 50, 170, 140), stream=data)
        if text:
            if fontsize is None:
                page.insert_text((60, 300), text)
            else:
                page.insert_text((60, 300), text, fontsize=fontsize)
    doc.save(str(path), **save_kwargs)
    doc.close()
    if pad:
        with open(str(path), "ab") as handle:
            handle.write(b"\n%" + b"A" * pad + b"\n")
    return path


def label_of(prompt: str) -> str:
    match = re.search(r"(\S+)\.\s", prompt)
    return match.group(1) if match else ""


def zip_ooxml(path: Path, family="word") -> Path:
    """A structurally valid OOXML package (real content types + main part).

    An earlier helper wrote a ZIP holding only a stub ``[Content_Types].xml``,
    which the hardened validator correctly rejects; tests must exercise real
    packages so they cannot pass against a weak implementation.
    """
    from test_office_validation import make_ooxml

    return make_ooxml(path, family)
