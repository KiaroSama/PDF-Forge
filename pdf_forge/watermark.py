from __future__ import annotations

import os
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple

from .constants import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['WatermarkCandidate', '_image_signature', 'scan_watermark_candidates',
           'export_watermark_preview', 'remove_watermark_images']


@dataclass
class WatermarkCandidate:
    """A repeated image that may be a watermark.

    Images are grouped by a lightweight signature ``(width, height, length)``
    where ``length`` is the raw (undecoded) stream length. The same visual image
    is often stored as a *different* xref on each page, so grouping by content
    signature (not xref) is what collapses it into one candidate - without
    decoding any pixels, which keeps scanning fast on large documents.
    """

    signature: Tuple[int, int, int]
    pages: set                 # 1-based page numbers where the image appears.
    width: int
    height: int
    sample_xref: int           # An xref of this image, used to render a preview.


def _raw_stream_len(doc, xref: int) -> int:
    """Raw (encoded) stream length for an image xref, or 0 if unavailable."""
    try:
        raw = doc.xref_stream_raw(xref)
        return len(raw) if raw else 0
    except Exception:  # noqa: BLE001 - only used for a cheap signature
        return 0


def _image_signature(doc, image_entry) -> Tuple[int, int, int]:
    """Return a cheap ``(width, height, raw-length)`` signature for an image.

    ``image_entry`` is one tuple from ``page.get_images(full=True)``:
    ``(xref, smask, width, height, bpc, colorspace, alt_cs, name, filter, ...)``.
    """
    xref, _smask, width, height = image_entry[0], image_entry[1], image_entry[2], image_entry[3]
    return (int(width), int(height), _raw_stream_len(doc, xref))


def scan_watermark_candidates(doc, min_pages: int = 2, max_candidates: int = 10):
    """Find images that repeat across pages (watermark candidates).

    Returns ``(candidates, total_pages)`` where candidates are sorted by page
    coverage (descending) and then by image area. Only images that appear on at
    least ``min_pages`` pages are returned.
    """
    groups = defaultdict(lambda: {"pages": set(), "w": 0, "h": 0, "xref": None})
    total = doc.page_count
    for page_index in range(total):
        for entry in doc[page_index].get_images(full=True):
            sig = _image_signature(doc, entry)
            group = groups[sig]
            group["pages"].add(page_index + 1)
            group["w"], group["h"] = sig[0], sig[1]
            if group["xref"] is None:
                group["xref"] = entry[0]

    candidates = [
        WatermarkCandidate(sig, g["pages"], g["w"], g["h"], g["xref"])
        for sig, g in groups.items()
        if len(g["pages"]) >= min_pages
    ]
    candidates.sort(key=lambda c: (len(c.pages), c.width * c.height), reverse=True)
    logger.debug(
        "Watermark scan: %d repeated image group(s) over %d page(s).",
        len(candidates), total,
    )
    return candidates[:max_candidates], total


def export_watermark_preview(doc, candidate: WatermarkCandidate, out_path: Path) -> bool:
    """Save a PNG preview of a candidate image. Returns True on success."""
    pymupdf = _import_pymupdf()
    try:
        pix = pymupdf.Pixmap(doc, candidate.sample_xref)
        try:
            # Normalize CMYK / alpha to plain RGB for a portable PNG preview.
            if pix.alpha or (pix.colorspace and pix.colorspace.n > 3):
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            pix.save(str(out_path))
        finally:
            pix = None
        return True
    except Exception as exc:  # noqa: BLE001 - preview is best-effort
        logger.warning("Preview export failed for %s: %s", candidate.signature, exc)
        return False


def remove_watermark_images(doc, signatures_to_remove, out_path: Path,
                            progress=None) -> int:
    """Remove the chosen repeated images from every page and save a new PDF.

    On each page, images whose signature matches are removed by redacting only
    their bounding boxes with image removal enabled while text and vector
    graphics are explicitly preserved - so a watermark stamped over text
    disappears without touching the text underneath. The result is written
    safely (temporary file -> validate -> atomic rename). Returns the number of
    pages modified.
    """
    pymupdf = _import_pymupdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    targets = set(signatures_to_remove)
    total = doc.page_count
    modified = 0

    for page_index in range(total):
        page = doc[page_index]
        added = False
        for entry in page.get_images(full=True):
            if _image_signature(doc, entry) in targets:
                for rect in page.get_image_rects(entry[0]):
                    page.add_redact_annot(rect)
                    added = True
        if added:
            # Remove only images; keep text and line art untouched.
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                text=pymupdf.PDF_REDACT_TEXT_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            )
            modified += 1
        if progress is not None:
            progress(page_index + 1, total)

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    logger.debug("Temporary watermark-removal file: '%s'", tmp_path)
    try:
        # garbage=4 drops the now-unreferenced watermark image objects; deflate
        # keeps the file compact without re-encoding retained image pixels.
        doc.save(str(tmp_path), garbage=4, deflate=True)
        _validate_written_pdf(tmp_path, expected_pages=total)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise

    elapsed = time.perf_counter() - started
    logger.info(
        "Watermark removal: modified=%d page(s), output='%s' in %.2fs.",
        modified, out_path, elapsed,
    )
    return modified
