from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from .constants import *  # noqa: F401,F403
from .safeio import promote_atomically
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['unlock_pdf_doc']  # permission helpers live in pdf_io


def unlock_pdf_doc(doc, out_path: Path) -> int:
    """Save an already-opened (and authenticated) document with no encryption.

    Removes the open password and every permission restriction, producing a
    fully unlocked copy. The source is never modified. Written safely
    (temporary file -> validate -> atomic rename). Returns the page count.
    """
    pymupdf = _import_pymupdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    total = doc.page_count

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        doc.save(
            str(tmp_path),
            encryption=pymupdf.PDF_ENCRYPT_NONE,
            garbage=3,
            deflate=True,
            use_objstms=1,
        )
        _validate_written_pdf(tmp_path, expected_pages=total)
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
        "Unlocked '%s' (%d page(s)) in %.2fs.", out_path, total, elapsed
    )
    return total
