from __future__ import annotations

import os
import re
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set

from .constants import *  # noqa: F401,F403
from .safeio import promote_atomically
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['WatermarkCandidate', '_image_identity', 'scan_watermark_candidates',
           'export_watermark_preview', 'remove_watermark_images']


@dataclass
class WatermarkCandidate:
    """A repeated image that may be a watermark.

    Images are grouped by a **content identity** - MuPDF's own hash of the
    decoded image (``get_image_info(hashes=True)``) combined with its pixel
    dimensions. This is what makes the grouping trustworthy:

      * the same visual image is often stored as a *different* xref on each
        page, so grouping by xref alone would miss it;
      * two genuinely different images can share width, height, and encoded
        stream length, so the old ``(w, h, raw_length)`` triple could merge
        unrelated images and delete the wrong one.

    Only images actually **painted** on a page are considered (unused resource
    entries are ignored), including images painted through Form XObjects.
    """

    signature: str             # Content identity (see above); kept as the
                               # public field name used by callers/tests.
    pages: Set[int]            # 1-based page numbers where the image is painted.
    width: int
    height: int
    sample_xref: int           # An xref of this image, used to render a preview.


def _image_identity(item) -> str:
    """Robust content identity for one painted-image record.

    ``item`` is an entry from ``page.get_image_info(hashes=True, xrefs=True)``.
    Uses MuPDF's decoded-image digest plus the pixel dimensions, so visually
    identical images stored under different xrefs collapse into one candidate
    while same-size-but-different images stay separate. Falls back to the xref
    when a digest is unavailable, which errs toward *not* grouping.
    """
    digest = item.get("digest")
    width, height = item.get("width", 0), item.get("height", 0)
    if digest:
        text = digest.hex() if isinstance(digest, (bytes, bytearray)) else str(digest)
        return f"{text}:{width}x{height}"
    return f"xref{item.get('xref', 0)}:{width}x{height}"


def _painted_images(doc, page_index: int) -> List[dict]:
    """Painted image occurrences on a page (never unused resource entries)."""
    try:
        return doc[page_index].get_image_info(hashes=True, xrefs=True)
    except Exception as exc:  # noqa: BLE001 - a broken page must not kill the scan
        logger.warning("Image scan failed on page %d: %s", page_index + 1, exc)
        return []


# Images smaller than this on either side cannot be a meaningful watermark.
# It also excludes the tiny transparent placeholder that replaces a removed
# image, so a re-scan after removal does not offer it as a new candidate.
_MIN_WATERMARK_SIDE = 8


def scan_watermark_candidates(doc, min_pages: int = 2, max_candidates: int = 10):
    """Find images that repeat across pages (watermark candidates).

    Returns ``(candidates, total_pages)`` where candidates are sorted by page
    coverage (descending) and then by image area. Only images painted on at
    least ``min_pages`` pages are returned.
    """
    groups: Dict[str, dict] = defaultdict(
        lambda: {"pages": set(), "w": 0, "h": 0, "xref": None}
    )
    total = doc.page_count
    for page_index in range(total):
        for item in _painted_images(doc, page_index):
            if (item.get("width", 0) < _MIN_WATERMARK_SIDE
                    or item.get("height", 0) < _MIN_WATERMARK_SIDE):
                continue  # degenerate/placeholder image, never a watermark
            identity = _image_identity(item)
            group = groups[identity]
            group["pages"].add(page_index + 1)
            group["w"] = item.get("width", 0) or group["w"]
            group["h"] = item.get("height", 0) or group["h"]
            xref = int(item.get("xref", 0) or 0)
            if xref and group["xref"] is None:
                group["xref"] = xref

    candidates = [
        WatermarkCandidate(
            signature=identity,
            pages=g["pages"],
            width=g["w"],
            height=g["h"],
            sample_xref=g["xref"] or 0,
        )
        for identity, g in groups.items()
        if len(g["pages"]) >= min_pages
    ]
    candidates.sort(key=lambda c: (len(c.pages), c.width * c.height), reverse=True)
    logger.debug(
        "Watermark scan: %d repeated painted image group(s) over %d page(s).",
        len(candidates), total,
    )
    return candidates[:max_candidates], total


def export_watermark_preview(doc, candidate: WatermarkCandidate, out_path: Path) -> bool:
    """Save a PNG preview of a candidate image. Returns True on success.

    The preview is rendered from one of the exact xrefs that will be removed, so
    what the user sees is what disappears.
    """
    pymupdf = _import_pymupdf()
    if not candidate.sample_xref:
        return False
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


_XOBJ_ENTRY_RE = re.compile(rb"/([^\s/\[\]<>()]+)\s+(\d+)\s+0\s+R")



def remove_watermark_images(doc, signatures_to_remove, out_path: Path,
                            progress=None, protection=None) -> int:
    """Remove the chosen repeated images from every page and save a new PDF.

    Removal works on the **object graph**, not on content-stream text: each
    matching image XObject is replaced through PyMuPDF's ``Page.delete_image``,
    which swaps it for a tiny transparent placeholder. Nothing in any content
    stream is rewritten, so this cannot corrupt a PDF - the previous
    regex-over-bytes approach could match ``/Name Do`` inside literal strings,
    hex strings, comments, or inline-image data and destroy unrelated content
    (PF-012).

    Because the image *object* is replaced, the watermark disappears wherever it
    is painted - directly on the page, or through Form XObjects nested to any
    depth, including forms shared by several pages (PF-024, PF-025). No manual
    traversal is required and no paint call is touched, so surrounding text,
    vector graphics, and other images are left exactly as they were.

    Returns the number of pages whose visible content actually changed - counted
    from painted occurrences before mutation, not from the number of objects
    written (PF-025).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    targets = set(signatures_to_remove)
    total = doc.page_count

    # Resolve the chosen content identities to concrete xrefs, and record which
    # pages actually show them. Both are computed BEFORE any mutation.
    target_xrefs: Set[int] = set()
    affected_pages: Set[int] = set()
    for page_index in range(total):
        for item in _painted_images(doc, page_index):
            if _image_identity(item) in targets:
                xref = int(item.get("xref", 0) or 0)
                if xref:
                    target_xrefs.add(xref)
                    affected_pages.add(page_index)

    modified = len(affected_pages)

    # Replace each target image object once per page that references it.
    for page_index in range(total):
        page = doc[page_index]
        page_xrefs = {
            int(entry[0]) for entry in page.get_images(full=True)
        } & target_xrefs
        for xref in page_xrefs:
            try:
                page.delete_image(xref)
            except Exception as exc:  # noqa: BLE001 - one page must not abort
                logger.warning(
                    "Could not remove image xref %d on page %d: %s",
                    xref, page_index + 1, exc,
                )
        if progress is not None:
            progress(page_index + 1, total)

    logger.info(
        "Watermark removal: %d target image(s) across %d affected page(s).",
        len(target_xrefs), modified,
    )

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    logger.debug("Temporary watermark-removal file: '%s'", tmp_path)
    # Decided during configuration and passed in, so this writer can never
    # silently drop a protected source's policy (PF-007).
    policy = protection if protection is not None else detect_protection(doc)
    protect_kwargs = policy.save_kwargs()
    try:
        # garbage=4 drops the now-unreferenced watermark object; use_objstms
        # keeps the output as compact as the original.
        doc.save(str(tmp_path), garbage=4, deflate=True, use_objstms=1,
                 **protect_kwargs)
        _validate_written_pdf(tmp_path, expected_pages=total,
                              password=policy.password if protect_kwargs else None)
        out_path = promote_atomically(tmp_path, out_path)
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
