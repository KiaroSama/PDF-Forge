from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import List, Sequence

from .constants import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['_import_pillow', 'open_render_document', '_validate_image_file',
           'render_pages_to_pngs', 'render_pdf_to_image_pdf']


def _import_pillow():
    """Import Pillow lazily so the core module imports without the dependency."""
    try:
        from PIL import Image  # type: ignore
        return Image
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'Pillow' library is required for image validation but is not "
            "installed. Run the application through Run.ps1 to install dependencies."
        ) from exc


def open_render_document(path: Path, password_prompt=None):
    """Open a PDF for rendering with PyMuPDF, handling encryption.

    Returns:
        A ``(document, page_count)`` tuple. The caller is responsible for
        closing the document with ``document.close()``.

    Raises:
        PdfOpenError: with a clear message on any failure.
    """
    doc = open_source_pdf(path, password_prompt=password_prompt)
    return doc, doc.page_count


def _validate_image_file(path: Path) -> None:
    """Reopen a freshly written image and confirm it is a valid, non-empty file."""
    Image = _import_pillow()
    if path.stat().st_size <= 0:
        raise PdfOpenError("Output validation failed: the image file is empty.")
    with Image.open(path) as image:
        image.verify()  # Raises if the image is truncated or corrupt.


def render_pages_to_pngs(doc, pages_zero_based: Sequence[int], out_dir: Path,
                         dpi: int, progress=None) -> List[Path]:
    """Render the given 0-based pages to PNG files named after their page number.

    Each page is written safely (temporary file -> validate -> atomic rename) and
    never overwrites an existing file. Returns the list of created file paths.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    total = len(pages_zero_based)
    created: List[Path] = []
    logger.debug("Rendering %d page(s) to PNG at %d DPI in '%s'.", total, dpi, out_dir)

    for index, page_index in enumerate(pages_zero_based, start=1):
        page_number = page_index + 1
        pixmap = doc[page_index].get_pixmap(dpi=dpi)
        final_path = unique_file_path(out_dir / build_page_image_name(page_number))

        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp", prefix=".pdfforge_", dir=str(out_dir)
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            pixmap.save(str(tmp_path), output="png")
            _validate_image_file(tmp_path)
            os.replace(tmp_path, final_path)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                logger.warning("Failed to remove temporary file: %s", tmp_path)
            raise
        finally:
            del pixmap

        created.append(final_path)
        logger.debug("Wrote page %d -> '%s'.", page_number, final_path.name)
        if progress is not None:
            progress(index, total)

    elapsed = time.perf_counter() - started
    logger.info(
        "Rendered %d PNG(s) at %d DPI into '%s' in %.2fs.",
        len(created), dpi, out_dir, elapsed,
    )
    return created


def render_pdf_to_image_pdf(doc, page_count: int, out_path: Path, dpi: int,
                            progress=None) -> int:
    """Rasterize every page and assemble the images into one image-only PDF.

    Every page is rendered to an image at the given DPI and placed on a new page
    of the same physical size as the source page (temporary file -> validate ->
    atomic rename). The result contains no selectable text, which is the point:
    the output is not editable. Returns the number of pages written.
    """
    pymupdf = _import_pymupdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    logger.debug(
        "Rasterizing %d page(s) at %d DPI into image-only PDF '%s'.",
        page_count, dpi, out_path,
    )

    out_doc = pymupdf.open()
    try:
        for page_index in range(page_count):
            page = doc[page_index]
            pixmap = page.get_pixmap(dpi=dpi)
            # Keep the original physical page size so the document prints the same.
            new_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, pixmap=pixmap)
            del pixmap
            if progress is not None:
                progress(page_index + 1, page_count)

        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            out_doc.save(str(tmp_path), garbage=3, deflate=True)
            # Reuse the merged-PDF validation: openable, not encrypted, page count.
            _validate_merged_pdf(tmp_path, expected_pages=page_count)
            os.replace(tmp_path, out_path)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                logger.warning("Failed to remove temporary file: %s", tmp_path)
            raise
    finally:
        out_doc.close()

    elapsed = time.perf_counter() - started
    logger.info(
        "Wrote image-only PDF '%s' (%d page(s)) at %d DPI in %.2fs.",
        out_path, page_count, dpi, elapsed,
    )
    return page_count
