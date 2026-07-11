from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Sequence

from .constants import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403

__all__ = ['_import_pymupdf', '_import_pypdf', 'PdfOpenError', 'open_source_pdf',
           'write_pages_to_pdf', '_validate_written_pdf', '_validate_merged_pdf',
           'write_merged_pdfs_to_pdf', 'resolves_to_same_file']


def _import_pymupdf():
    """Import PyMuPDF lazily so the core module imports without the dependency."""
    try:
        import pymupdf  # type: ignore
        return pymupdf
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'pymupdf' library is required but not installed. "
            "Run the application through Run.ps1 to install dependencies."
        ) from exc


def _import_pypdf():
    """Import pypdf lazily (used only by the watermark-removal surgery)."""
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
    """Open and validate a source PDF with PyMuPDF, handling encryption.

    Args:
        path: Path to the source PDF.
        password_prompt: Optional callable returning a password string when the
            PDF is encrypted and the empty password fails.

    Returns:
        A ``pymupdf.Document`` ready for reading. The caller may close it with
        ``document.close()`` when done (module teardown also closes safely).

    Raises:
        PdfOpenError: with a clear message on any failure.
    """
    pymupdf = _import_pymupdf()

    logger.debug("Opening source PDF: '%s'", path)
    try:
        doc = pymupdf.open(str(path))
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        logger.error("PDF read error for '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF appears to be corrupted or unreadable: {exc}") from exc
    except OSError as exc:
        logger.error("OS error opening '%s': %s", path, exc)
        raise PdfOpenError(f"Could not open the file: {exc}") from exc

    if doc.needs_pass:
        logger.info("Source PDF is encrypted; attempting empty password.")
        # authenticate() returns 0 on failure, positive on success.
        decrypted = doc.authenticate("") > 0
        if not decrypted and password_prompt is not None:
            password = password_prompt()
            if password is not None:
                decrypted = doc.authenticate(password) > 0
            # Drop the local reference; the password is never logged or stored.
            del password
        if not decrypted:
            doc.close()
            raise PdfOpenError(
                "The PDF is encrypted and could not be decrypted with the "
                "provided password."
            )
        logger.info("Source PDF decrypted successfully.")

    try:
        page_count = doc.page_count
    except Exception as exc:  # noqa: BLE001
        doc.close()
        logger.error("Could not determine page count for '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF page count could not be determined: {exc}") from exc

    if page_count < 1:
        doc.close()
        logger.error("Source PDF '%s' contains no pages.", path)
        raise PdfOpenError("The PDF contains no pages.")

    logger.info("Opened source PDF '%s' (%d page(s)).", path, page_count)
    return doc


def _save_doc_to_path_safely(out_doc, out_path: Path, expected_pages: int,
                             validate) -> None:
    """Save a document via temp file -> validate -> atomic rename."""
    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    logger.debug("Temporary write file: '%s'", tmp_path)
    try:
        # garbage=3 deduplicates unused/identical objects; deflate compresses
        # streams. Cheap wins on every output, identical visual result.
        out_doc.save(str(tmp_path), garbage=3, deflate=True)
        validate(tmp_path, expected_pages=expected_pages)
        logger.debug("Validated temporary output (%d page(s)).", expected_pages)
        # Atomic promotion. The final path is guaranteed unique by the caller.
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
                logger.debug("Removed temporary file after failure: '%s'", tmp_path)
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise


def write_pages_to_pdf(doc, pages_zero_based: Sequence[int], out_path: Path,
                       progress=None) -> int:
    """Write the given 0-based pages to ``out_path`` using a safe temp file.

    The data is first written to a temporary file in the destination directory,
    validated, then atomically renamed to the final path. Temporary files are
    removed on failure. Returns the number of pages written.
    """
    pymupdf = _import_pymupdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    total = len(pages_zero_based)
    logger.debug("Writing %d page(s) to '%s'.", total, out_path)

    out_doc = pymupdf.open()
    try:
        for index, page_index in enumerate(pages_zero_based, start=1):
            out_doc.insert_pdf(doc, from_page=page_index, to_page=page_index)
            if progress is not None:
                progress(index, total)
        _save_doc_to_path_safely(out_doc, out_path, total, _validate_written_pdf)
    finally:
        out_doc.close()

    elapsed = time.perf_counter() - started
    logger.info("Wrote '%s' (%d page(s)) in %.2fs.", out_path, total, elapsed)
    return total


def _validate_written_pdf(path: Path, expected_pages: int) -> None:
    """Reopen a freshly written PDF and confirm its page count."""
    pymupdf = _import_pymupdf()
    check = pymupdf.open(str(path))
    try:
        actual = check.page_count
    finally:
        check.close()
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
    pymupdf = _import_pymupdf()
    check = pymupdf.open(str(path))
    try:
        if check.needs_pass:
            raise PdfOpenError("Output validation failed: the merged PDF is encrypted.")
        actual = check.page_count
    finally:
        check.close()
    if actual != expected_pages:
        raise PdfOpenError(
            f"Output validation failed: expected {expected_pages} pages, "
            f"found {actual}."
        )


def write_merged_pdfs_to_pdf(docs, out_path: Path, progress=None) -> int:
    """Merge already-opened PDF documents into a single PDF at ``out_path``.

    Pages from each document are appended in order. The data is written to a
    temporary file in the destination directory, validated (openable, not
    encrypted, correct page count), then atomically renamed to the final path.
    Temporary files are removed on failure. Returns the total pages written.
    """
    pymupdf = _import_pymupdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total = sum(d.page_count for d in docs)
    started = time.perf_counter()
    logger.debug(
        "Merging %d source document(s), %d total page(s) into '%s'.",
        len(docs), total, out_path,
    )

    out_doc = pymupdf.open()
    written = 0
    try:
        for doc_index, doc in enumerate(docs, start=1):
            page_count = doc.page_count
            out_doc.insert_pdf(doc)
            written += page_count
            if progress is not None:
                progress(written, total)
            logger.debug(
                "Appended source %d/%d (%d page(s); running total=%d).",
                doc_index, len(docs), page_count, written,
            )
        _save_doc_to_path_safely(out_doc, out_path, total, _validate_merged_pdf)
    finally:
        out_doc.close()

    elapsed = time.perf_counter() - started
    logger.info(
        "Merged %d source(s) -> '%s' (%d page(s)) in %.2fs.",
        len(docs), out_path, total, elapsed,
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
