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

__all__ = ['_import_pypdf', 'PdfOpenError', 'open_source_pdf', 'write_pages_to_pdf', '_validate_written_pdf', '_validate_merged_pdf', 'write_merged_pdfs_to_pdf', 'resolves_to_same_file']

def _import_pypdf():
    """Import pypdf lazily so the core module imports without the dependency."""
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore
        from pypdf.errors import PdfReadError  # type: ignore
        return PdfReader, PdfWriter, PdfReadError
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'pypdf' library is required but not installed. "
            "Run the application through Run.ps1 to install dependencies."
        ) from exc


class PdfOpenError(Exception):
    """Raised when a PDF cannot be opened or is unusable."""


def open_source_pdf(path: Path, password_prompt=None):
    """Open and validate a source PDF, handling encryption.

    Args:
        path: Path to the source PDF.
        password_prompt: Optional callable returning a password string when the
            PDF is encrypted and the empty password fails.

    Returns:
        A ``PdfReader`` ready for reading.

    Raises:
        PdfOpenError: with a clear message on any failure.
    """
    PdfReader, _PdfWriter, PdfReadError = _import_pypdf()

    logger.debug("Opening source PDF: '%s'", path)
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


def write_pages_to_pdf(reader, pages_zero_based: Sequence[int], out_path: Path,
                       progress=None) -> int:
    """Write the given 0-based pages to ``out_path`` using a safe temp file.

    The data is first written to a temporary file in the destination directory,
    validated, then atomically renamed to the final path. Temporary files are
    removed on failure. Returns the number of pages written.
    """
    _PdfReader, PdfWriter, _PdfReadError = _import_pypdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    writer = PdfWriter()
    total = len(pages_zero_based)
    logger.debug("Writing %d page(s) to '%s'.", total, out_path)
    for index, page_index in enumerate(pages_zero_based, start=1):
        writer.add_page(reader.pages[page_index])
        if progress is not None:
            progress(index, total)

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    tmp_path = Path(tmp_name)
    logger.debug("Temporary write file: '%s'", tmp_path)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            writer.write(handle)

        # Validate the temporary PDF before promoting it to the final name.
        _validate_written_pdf(tmp_path, expected_pages=total)
        logger.debug("Validated temporary output (%d page(s)).", total)

        # Atomic promotion. The final path is guaranteed unique by the caller,
        # so os.replace will not clobber an unrelated file.
        os.replace(tmp_path, out_path)
    except Exception:
        # Clean up only the temporary file created by this operation.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
                logger.debug("Removed temporary file after failure: '%s'", tmp_path)
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise
    finally:
        writer.close()

    elapsed = time.perf_counter() - started
    logger.info(
        "Wrote '%s' (%d page(s)) in %.2fs.", out_path, total, elapsed
    )
    return total


def _validate_written_pdf(path: Path, expected_pages: int) -> None:
    """Reopen a freshly written PDF and confirm its page count."""
    PdfReader, _PdfWriter, _PdfReadError = _import_pypdf()
    check = PdfReader(str(path))
    actual = len(check.pages)
    if actual != expected_pages:
        raise PdfOpenError(
            f"Output validation failed: expected {expected_pages} pages, "
            f"found {actual}."
        )


def _validate_merged_pdf(path: Path, expected_pages: int) -> None:
    """Reopen a freshly merged PDF and confirm it is usable.

    Verifies the output can be opened, is not encrypted, and contains exactly
    the expected total page count.
    """
    PdfReader, _PdfWriter, _PdfReadError = _import_pypdf()
    check = PdfReader(str(path))
    if getattr(check, "is_encrypted", False):
        raise PdfOpenError("Output validation failed: the merged PDF is encrypted.")
    actual = len(check.pages)
    if actual != expected_pages:
        raise PdfOpenError(
            f"Output validation failed: expected {expected_pages} pages, "
            f"found {actual}."
        )


def write_merged_pdfs_to_pdf(readers, out_path: Path, progress=None) -> int:
    """Merge already-opened PDF readers into a single PDF at ``out_path``.

    Pages from each reader are appended in order using ``PdfWriter.add_page``.
    The data is written to a temporary file in the destination directory,
    validated (openable, not encrypted, correct page count), then atomically
    renamed to the final path. Temporary files are removed on failure. Returns
    the total number of pages written.
    """
    _PdfReader, PdfWriter, _PdfReadError = _import_pypdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Expected total used for progress and post-write validation.
    total = sum(len(reader.pages) for reader in readers)
    started = time.perf_counter()
    logger.debug(
        "Merging %d source reader(s), %d total page(s) into '%s'.",
        len(readers), total, out_path,
    )

    writer = PdfWriter()
    written = 0
    for reader_index, reader in enumerate(readers, start=1):
        page_count = len(reader.pages)
        for page in reader.pages:
            writer.add_page(page)
            written += 1
            if progress is not None:
                progress(written, total)
        logger.debug(
            "Appended source %d/%d (%d page(s); running total=%d).",
            reader_index, len(readers), page_count, written,
        )

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    tmp_path = Path(tmp_name)
    logger.debug("Temporary merge file: '%s'", tmp_path)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            writer.write(handle)

        # Validate the temporary PDF before promoting it to the final name.
        _validate_merged_pdf(tmp_path, expected_pages=total)
        logger.debug("Validated merged output (%d page(s)).", total)

        # Atomic promotion. The final path is guaranteed unique by the caller.
        os.replace(tmp_path, out_path)
    except Exception:
        # Clean up only the temporary file created by this operation.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
                logger.debug("Removed temporary merge file after failure: '%s'", tmp_path)
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise
    finally:
        writer.close()

    elapsed = time.perf_counter() - started
    logger.info(
        "Merged %d source(s) -> '%s' (%d page(s)) in %.2fs.",
        len(readers), out_path, total, elapsed,
    )
    return total


def resolves_to_same_file(a: Path, b: Path) -> bool:
    """Return True when two paths resolve to the same file on disk."""
    try:
        return os.path.realpath(str(a)).lower() == os.path.realpath(str(b)).lower() \
            if os.name == "nt" else \
            os.path.realpath(str(a)) == os.path.realpath(str(b))
    except OSError:
        return False
