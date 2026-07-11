from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .constants import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['open_pypdf_document', 'WatermarkCandidate', '_iter_page_image_xobjects', '_stream_raw_length', '_image_signature', 'scan_watermark_candidates', 'export_watermark_preview', 'remove_watermark_images']


def open_pypdf_document(path: Path, password_prompt=None):
    """Open and validate a source PDF with pypdf, handling encryption.

    The watermark-removal surgery operates on pypdf's object model (content
    streams, XObjects), so this tool keeps its own pypdf-based opener while the
    rest of the application uses the PyMuPDF-based ``open_source_pdf``.

    Returns:
        A ``PdfReader`` ready for reading.

    Raises:
        PdfOpenError: with a clear message on any failure.
    """
    PdfReader, _PdfWriter, PdfReadError = _import_pypdf()

    logger.debug("Opening source PDF (pypdf): '%s'", path)
    try:
        reader = PdfReader(str(path))
    except PdfReadError as exc:
        logger.error("PDF read error for '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF appears to be corrupted or unreadable: {exc}") from exc
    except OSError as exc:
        logger.error("OS error opening '%s': %s", path, exc)
        raise PdfOpenError(f"Could not open the file: {exc}") from exc
    except Exception as exc:  # pypdf can raise various low-level errors
        logger.error("Failed to parse '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF could not be parsed: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        logger.info("Source PDF is encrypted; attempting empty password.")
        decrypted = False
        try:
            # pypdf returns 0 on failure, 1/2 on success.
            if reader.decrypt("") != 0:
                decrypted = True
        except Exception:  # noqa: BLE001 - treat any decrypt error as failure
            decrypted = False

        if not decrypted and password_prompt is not None:
            password = password_prompt()
            if password is not None:
                try:
                    if reader.decrypt(password) != 0:
                        decrypted = True
                except Exception:  # noqa: BLE001
                    decrypted = False
            # Drop the local reference; the password is never logged or stored.
            del password

        if not decrypted:
            raise PdfOpenError(
                "The PDF is encrypted and could not be decrypted with the "
                "provided password."
            )
        logger.info("Source PDF decrypted successfully.")

    try:
        page_count = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not determine page count for '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF page count could not be determined: {exc}") from exc

    if page_count < 1:
        logger.error("Source PDF '%s' contains no pages.", path)
        raise PdfOpenError("The PDF contains no pages.")

    logger.info("Opened source PDF '%s' (%d page(s)).", path, page_count)
    return reader


@dataclass
class WatermarkCandidate:
    """A repeated image XObject that may be a watermark.

    Images are grouped by a lightweight signature ``(width, height, length)``
    where ``length`` is the raw (undecoded) stream length. Identical images
    referenced from many pages therefore collapse into one candidate without
    decoding any pixels, which keeps scanning fast even on large documents.
    """

    signature: Tuple[int, int, int]
    pages: set                 # 1-based page numbers where the image appears.
    width: int
    height: int
    sample_page_index: int     # 0-based page used to render a preview.


def _iter_page_image_xobjects(page):
    """Yield ``(name, image_object)`` for each image XObject on a page."""
    resources = page.get("/Resources")
    if not resources:
        return
    xobjects = resources.get_object().get("/XObject")
    if not xobjects:
        return
    for name, ref in xobjects.get_object().items():
        obj = ref.get_object()
        if obj.get("/Subtype") == "/Image":
            yield str(name), obj


def _stream_raw_length(image_obj) -> int:
    """Return the raw (encoded) stream length without decoding pixels.

    Prefers the stored raw buffer; falls back to the declared ``/Length``.
    Used only to build a cheap image signature, so an occasional 0 is harmless.
    """
    data = getattr(image_obj, "_data", None)
    if data:
        return len(data)
    try:
        length = image_obj.get("/Length")
        if length is not None:
            return int(length.get_object() if hasattr(length, "get_object") else length)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _image_signature(image_obj) -> Tuple[int, int, int]:
    """Return a cheap ``(width, height, raw-length)`` signature for an image.

    Identical images referenced from many pages share the same signature
    without decoding any pixels, which keeps scanning fast on large documents.
    """
    return (
        int(image_obj.get("/Width", 0)),
        int(image_obj.get("/Height", 0)),
        _stream_raw_length(image_obj),
    )


def scan_watermark_candidates(pages, min_pages: int = 2, max_candidates: int = 10):
    """Find image XObjects that repeat across pages (watermark candidates).

    Returns ``(candidates, total_pages)`` where candidates are sorted by page
    coverage (descending) and then by image area. Only images that appear on at
    least ``min_pages`` pages are returned.
    """
    from collections import defaultdict

    groups = defaultdict(lambda: {"pages": set(), "w": 0, "h": 0, "sample": None})
    total = len(pages)
    for index, page in enumerate(pages):
        for _name, obj in _iter_page_image_xobjects(page):
            sig = _image_signature(obj)
            group = groups[sig]
            group["pages"].add(index + 1)
            group["w"], group["h"] = sig[0], sig[1]
            if group["sample"] is None:
                group["sample"] = index

    candidates = [
        WatermarkCandidate(sig, g["pages"], g["w"], g["h"], g["sample"])
        for sig, g in groups.items()
        if len(g["pages"]) >= min_pages
    ]
    candidates.sort(
        key=lambda c: (len(c.pages), c.width * c.height), reverse=True
    )
    logger.debug(
        "Watermark scan: %d repeated image group(s) over %d page(s).",
        len(candidates), total,
    )
    return candidates[:max_candidates], total


def export_watermark_preview(pages, candidate: WatermarkCandidate, out_path: Path) -> bool:
    """Save a PNG preview of a candidate image. Returns True on success.

    The preview is decoded from the candidate's sample page using Pillow via
    pypdf's image extraction, matched by pixel dimensions.
    """
    page = pages[candidate.sample_page_index]
    try:
        for image_file in page.images:
            pil = image_file.image
            if pil is None:
                continue
            if pil.width == candidate.width and pil.height == candidate.height:
                pil.convert("RGB").save(out_path, "PNG")
                return True
    except Exception as exc:  # noqa: BLE001 - preview is best-effort
        logger.warning("Preview export failed for %s: %s", candidate.signature, exc)
    return False


def remove_watermark_images(reader, signatures_to_remove, out_path: Path,
                            progress=None) -> int:
    """Remove the paint calls for the given image signatures from every page.

    For each page, the ``<name> Do`` operators that draw a matching image are
    dropped from the content stream and the image is removed from the page
    resources. Content streams are recompressed to avoid file-size bloat. The
    result is written safely (temporary file -> validate -> atomic rename).
    Returns the number of pages modified.
    """
    _PdfReader, _PdfWriter, _PdfReadError = _import_pypdf()
    from pypdf import PdfWriter
    from pypdf.generic import ContentStream, NameObject

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    targets = set(signatures_to_remove)

    writer = PdfWriter()
    writer.append(reader)

    total = len(writer.pages)
    expected_pages = total
    modified = 0
    dropped_ops = 0

    for index, page in enumerate(writer.pages):
        resources = page.get("/Resources")
        xobjects = resources.get_object().get("/XObject") if resources else None
        names_to_drop = set()
        if xobjects:
            xobjects = xobjects.get_object()
            for name, obj in _iter_page_image_xobjects(page):
                if _image_signature(obj) in targets:
                    names_to_drop.add(name)

        if not names_to_drop:
            if progress is not None:
                progress(index + 1, total)
            continue

        content = ContentStream(page.get_contents(), writer)
        kept_ops = []
        for operands, operator in content.operations:
            if operator == b"Do" and operands and str(operands[0]) in names_to_drop:
                dropped_ops += 1
                continue  # Drop the paint call; balanced q/cm/Q remain harmless.
            kept_ops.append((operands, operator))
        content.operations = kept_ops
        page[NameObject("/Contents")] = writer._add_object(content)

        # Also drop the now-unused image from the page resources.
        for name in names_to_drop:
            key = NameObject(name)
            if key in xobjects:
                del xobjects[key]

        try:
            page.compress_content_streams()
        except Exception:  # noqa: BLE001 - compression is best-effort
            logger.warning("Content compression failed on page %d.", index + 1)

        modified += 1
        if progress is not None:
            progress(index + 1, total)

    # Physically drop the now-unreferenced watermark image(s) and merge any
    # duplicate objects to keep the file compact. This only removes unused
    # objects and dedupes identical ones; it never re-encodes retained image
    # data, so the visible content stays byte-for-byte lossless.
    try:
        writer.compress_identical_objects()
        logger.debug("Compacted objects (removed unreferenced, merged duplicates).")
    except Exception:  # noqa: BLE001 - optimization is best-effort
        logger.warning("Object compaction step failed; writing without it.")

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    tmp_path = Path(tmp_name)
    logger.debug("Temporary watermark-removal file: '%s'", tmp_path)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            writer.write(handle)
        _validate_written_pdf(tmp_path, expected_pages=expected_pages)
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
        "Watermark removal: modified=%d page(s), dropped=%d paint op(s), "
        "output='%s' in %.2fs.",
        modified, dropped_ops, out_path, elapsed,
    )
    return modified
