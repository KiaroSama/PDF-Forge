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


def _xobject_names_for(doc, container_xref: int, targets: Set[int]) -> List[bytes]:
    """Resource names in ``container_xref``'s /Resources /XObject that point at
    one of ``targets``. ``container_xref`` is a Form XObject's own xref."""
    try:
        kind, value = doc.xref_get_key(container_xref, "Resources/XObject")
    except Exception:  # noqa: BLE001
        return []
    if not value or kind == "null":
        return []
    names = []
    for match in _XOBJ_ENTRY_RE.finditer(
        value.encode() if isinstance(value, str) else value
    ):
        name, xref_text = match.group(1), match.group(2)
        if int(xref_text) in targets:
            names.append(name)
    return names


def _strip_paint_calls(data: bytes, names) -> bytes:
    """Remove every ``/Name Do`` paint call for the given resource names.

    Only the paint operator is dropped; the surrounding ``q``/``cm``/``Q`` state
    operators remain and draw nothing on their own.
    """
    for name in names:
        raw = name if isinstance(name, bytes) else name.encode()
        data = re.sub(rb"/" + re.escape(raw) + rb"\s+Do\b", b"", data)
    return data


def _collect_form_xobjects(doc, page, seen=None) -> Set[int]:
    """Every Form XObject xref reachable from a page (recursively, bounded)."""
    if seen is None:
        seen = set()
    try:
        entries = page.get_xobjects()
    except Exception:  # noqa: BLE001
        return seen
    for entry in entries:
        xref = int(entry[0])
        if xref in seen:
            continue
        seen.add(xref)
        # Recurse into nested forms via their own resource dictionaries.
        for nested in _nested_form_xrefs(doc, xref):
            if nested not in seen:
                seen.add(nested)
    return seen


def _nested_form_xrefs(doc, form_xref: int) -> Set[int]:
    """Form XObject xrefs referenced from another form's resources."""
    try:
        kind, value = doc.xref_get_key(form_xref, "Resources/XObject")
    except Exception:  # noqa: BLE001
        return set()
    if not value or kind == "null":
        return set()
    found = set()
    for match in _XOBJ_ENTRY_RE.finditer(
        value.encode() if isinstance(value, str) else value
    ):
        found.add(int(match.group(2)))
    return found


def remove_watermark_images(doc, signatures_to_remove, out_path: Path,
                            progress=None) -> int:
    """Remove the chosen repeated images from every page and save a new PDF.

    The paint call (``/Name Do``) that draws a matching image is deleted from
    the page's content stream **and** from any Form XObject that paints it, so a
    watermark drawn through a form is really removed rather than silently
    missed. The page's resources are then sanitized so the now-unused image
    object is dropped and garbage-collected on save.

    This targets *only* the chosen image: any other image on the page - even one
    the watermark is stamped on top of - and all text and vector graphics are
    left untouched. Written safely (temp file -> validate -> atomic rename).
    Returns the number of pages modified.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    targets = set(signatures_to_remove)
    total = doc.page_count
    modified = 0

    # Resolve the chosen content identities to the concrete xrefs carrying them.
    target_xrefs: Set[int] = set()
    for page_index in range(total):
        for item in _painted_images(doc, page_index):
            if _image_identity(item) in targets:
                xref = int(item.get("xref", 0) or 0)
                if xref:
                    target_xrefs.add(xref)

    for page_index in range(total):
        page = doc[page_index]
        changed = False

        # 1) Paint calls in the page's own content streams.
        page_names = [
            entry[7] for entry in page.get_images(full=True)
            if int(entry[0]) in target_xrefs
        ]
        if page_names:
            for content_xref in page.get_contents():
                data = doc.xref_stream(content_xref)
                if data is None:
                    continue
                new_data = _strip_paint_calls(data, page_names)
                if new_data != data:
                    doc.update_stream(content_xref, new_data)
                    changed = True

        # 2) Paint calls inside Form XObjects reachable from this page.
        for form_xref in _collect_form_xobjects(doc, page):
            form_names = _xobject_names_for(doc, form_xref, target_xrefs)
            if not form_names:
                continue
            try:
                data = doc.xref_stream(form_xref)
            except Exception:  # noqa: BLE001
                continue
            if data is None:
                continue
            new_data = _strip_paint_calls(data, form_names)
            if new_data != data:
                doc.update_stream(form_xref, new_data)
                changed = True

        if changed:
            # Prune the now-unreferenced image from the page resources so it is
            # garbage-collected on save and never re-detected as a watermark.
            try:
                page.clean_contents(sanitize=True)
            except Exception as exc:  # noqa: BLE001 - keep going on odd pages
                logger.warning("clean_contents failed on page %d: %s",
                               page_index + 1, exc)
            modified += 1
        if progress is not None:
            progress(page_index + 1, total)

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    logger.debug("Temporary watermark-removal file: '%s'", tmp_path)
    # Preserve an open-password source's protection on the cleaned copy.
    policy = detect_protection(doc)
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
