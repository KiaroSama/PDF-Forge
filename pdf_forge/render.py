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

__all__ = ['_import_pdfium', '_import_pillow', 'open_pdfium_document', '_validate_image_file', '_render_page_to_rgb_image', 'render_pages_to_pngs', 'render_pdf_to_image_pdf']

_PDFIUM_ERR_PASSWORD = 4


def _import_pdfium():
    """Import pypdfium2 lazily so the core module imports without the dependency."""
    try:
        import pypdfium2 as pdfium  # type: ignore
        return pdfium
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'pypdfium2' library is required for image conversion but is not "
            "installed. Run the application through Run.ps1 to install dependencies."
        ) from exc


def _import_pillow():
    """Import Pillow lazily so the core module imports without the dependency."""
    try:
        from PIL import Image  # type: ignore
        return Image
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'Pillow' library is required for image conversion but is not "
            "installed. Run the application through Run.ps1 to install dependencies."
        ) from exc


def open_pdfium_document(path: Path, password_prompt=None):
    """Open a PDF for rendering with pypdfium2, handling encryption.

    Returns:
        A ``(document, page_count)`` tuple. The caller is responsible for
        closing the document with ``document.close()``.

    Raises:
        PdfOpenError: with a clear message on any failure.
    """
    pdfium = _import_pdfium()

    logger.debug("Opening PDF for rendering: '%s'", path)
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        # A password-required document is the only case we can recover from.
        if getattr(exc, "err_code", None) == _PDFIUM_ERR_PASSWORD and password_prompt is not None:
            logger.info("PDF is encrypted; prompting for a password (render path).")
            password = password_prompt()
            if password is None:
                raise PdfOpenError(
                    "The PDF is encrypted and no password was provided."
                ) from exc
            try:
                pdf = pdfium.PdfDocument(str(path), password=password)
            except pdfium.PdfiumError as exc2:
                logger.error("Render open failed after password for '%s': %s", path, exc2)
                raise PdfOpenError(
                    "The PDF is encrypted and could not be opened with the "
                    "provided password."
                ) from exc2
            finally:
                # The password is never logged or stored.
                del password
        else:
            logger.error("Render open failed for '%s': %s", path, exc)
            raise PdfOpenError(
                f"The PDF could not be opened for rendering: {exc}"
            ) from exc
    except OSError as exc:
        logger.error("OS error opening '%s' for rendering: %s", path, exc)
        raise PdfOpenError(f"Could not open the file: {exc}") from exc

    try:
        page_count = len(pdf)
    except Exception as exc:  # noqa: BLE001
        pdf.close()
        raise PdfOpenError(f"The PDF page count could not be determined: {exc}") from exc

    if page_count < 1:
        pdf.close()
        raise PdfOpenError("The PDF contains no pages.")

    logger.info("Opened PDF for rendering '%s' (%d page(s)).", path, page_count)
    return pdf, page_count


def _validate_image_file(path: Path) -> None:
    """Reopen a freshly written image and confirm it is a valid, non-empty file."""
    Image = _import_pillow()
    if path.stat().st_size <= 0:
        raise PdfOpenError("Output validation failed: the image file is empty.")
    with Image.open(path) as image:
        image.verify()  # Raises if the image is truncated or corrupt.


def _render_page_to_rgb_image(pdf, page_index: int, dpi: int):
    """Render one 0-based page to an RGB PIL image at the given DPI."""
    Image = _import_pillow()  # Ensure Pillow is present before rendering.
    scale = dpi / 72.0
    page = pdf[page_index]
    try:
        bitmap = page.render(scale=scale)
        try:
            image = bitmap.to_pil().convert("RGB")
        finally:
            bitmap.close()
    finally:
        page.close()
    return image


def render_pages_to_pngs(pdf, pages_zero_based: Sequence[int], out_dir: Path,
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
        image = _render_page_to_rgb_image(pdf, page_index, dpi)
        final_path = unique_file_path(out_dir / build_page_image_name(page_number))

        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp", prefix=".pdfforge_", dir=str(out_dir)
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            image.save(tmp_path, "PNG")
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
            image.close()

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


def render_pdf_to_image_pdf(pdf, page_count: int, out_path: Path, dpi: int,
                            progress=None) -> int:
    """Rasterize every page and assemble the images into one image-only PDF.

    Every page is rendered to an image at the given DPI, then Pillow writes all
    images into a single PDF (temporary file -> validate -> atomic rename). The
    result contains no selectable text, which is the point: the output is not
    editable. Returns the number of pages written.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    logger.debug(
        "Rasterizing %d page(s) at %d DPI into image-only PDF '%s'.",
        page_count, dpi, out_path,
    )

    images = []
    try:
        for page_index in range(page_count):
            images.append(_render_page_to_rgb_image(pdf, page_index, dpi))
            if progress is not None:
                progress(page_index + 1, page_count)

        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            # 'resolution' sets the DPI metadata so each rasterized page keeps
            # its original physical size (pixels / dpi == original inches).
            images[0].save(
                tmp_path,
                "PDF",
                save_all=True,
                append_images=images[1:],
                resolution=float(dpi),
            )
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
        for image in images:
            try:
                image.close()
            except Exception:  # noqa: BLE001 - closing must never mask errors
                pass

    elapsed = time.perf_counter() - started
    logger.info(
        "Wrote image-only PDF '%s' (%d page(s)) at %d DPI in %.2fs.",
        out_path, page_count, dpi, elapsed,
    )
    return page_count
