from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from .constants import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['compress_pdf']


def compress_pdf(path: Path, out_path: Path, jpeg_quality: Optional[int],
                 dpi_target: Optional[int], password_prompt=None,
                 password=None) -> dict:
    """Compress a PDF into a new file; the source is never modified.

    Always applied (lossless, zero quality change):
      * font subsetting (embedded fonts keep only the used glyphs),
      * object deduplication / garbage collection (``garbage=4``),
      * stream deflate and PDF object streams (``use_objstms``).

    When ``jpeg_quality`` is given (all levels except Ultra), embedded images
    whose effective resolution exceeds ~1.33x ``dpi_target`` are downsampled to
    ``dpi_target`` and re-encoded as JPEG at that quality. Bitonal (fax/scan
    B/W) images are left untouched - recompressing them usually hurts.

    Returns a stats dict: pages, original_size, new_size (bytes).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    original_size = Path(path).stat().st_size

    doc = open_source_pdf(path, password_prompt=password_prompt, password=password)
    try:
        page_count = doc.page_count
        # Preserve an open-password source's protection on the compressed copy;
        # an owner-restricted source cannot be reproduced (no owner password),
        # so it is written unprotected - the caller warns about that up front.
        policy = detect_protection(doc)
        protect_kwargs = policy.save_kwargs()

        if jpeg_quality is not None:
            # Consider reducing any image above the target DPI, and never below
            # it or above the original.
            # ponytail: MuPDF subsamples in quality-preserving (roughly halving)
            # steps, so the result lands at or above dpi_target - not always
            # exactly on it. Good enough; exact per-image resampling would mean
            # re-implementing colorspace/mask handling MuPDF already does.
            logger.info(
                "Compress: rewriting images (quality=%d, target dpi %d).",
                jpeg_quality, dpi_target,
            )
            doc.rewrite_images(
                dpi_threshold=dpi_target + 1,
                dpi_target=dpi_target,
                quality=jpeg_quality,
                lossy=True,
                lossless=True,
                bitonal=False,
                color=True,
                gray=True,
            )
        else:
            logger.info("Compress: lossless-only mode (images untouched).")

        # Lossless: embedded fonts keep only the glyphs the document uses.
        try:
            doc.subset_fonts()
        except Exception as exc:  # noqa: BLE001 - font subsetting is best-effort
            logger.warning("Font subsetting skipped: %s", exc)

        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            doc.save(str(tmp_path), garbage=4, deflate=True, use_objstms=1,
                     **protect_kwargs)
            _validate_written_pdf(tmp_path, expected_pages=page_count,
                                  password=policy.password if protect_kwargs else None)
            os.replace(tmp_path, out_path)
            record_generated_output(out_path)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                logger.warning("Failed to remove temporary file: %s", tmp_path)
            raise
    finally:
        doc.close()

    new_size = out_path.stat().st_size
    elapsed = time.perf_counter() - started
    logger.info(
        "Compressed '%s' -> '%s': %d -> %d bytes (%.1f%%) in %.2fs.",
        path, out_path, original_size, new_size,
        (100.0 * new_size / original_size) if original_size else 0.0, elapsed,
    )
    return {
        "pages": page_count,
        "original_size": original_size,
        "new_size": new_size,
    }
