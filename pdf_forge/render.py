from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import List, Sequence

from .constants import *  # noqa: F401,F403
from .safeio import promote_atomically
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['_import_pillow', 'open_render_document', '_validate_image_file',
           'render_pages_to_pngs', 'render_pdf_to_image_pdf',
           'count_embedded_images', 'extract_embedded_images']

# Embedded images smaller than this (either side, in pixels) are treated as
# placeholders/artifacts and skipped by the extraction tool.
_MIN_EXTRACT_SIDE = 16


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


def open_render_document(path: Path, password_prompt=None, password=None):
    """Open a PDF for rendering with PyMuPDF, handling encryption.

    ``password`` is an optional known password tried silently first, so a queued
    runner can reopen an encrypted source without prompting again.

    Returns:
        A ``(document, page_count)`` tuple. The caller is responsible for
        closing the document with :func:`close_doc`.

    Raises:
        PdfOpenError: with a clear message on any failure.
    """
    doc = open_source_pdf(path, password_prompt=password_prompt, password=password)
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
            final_path = promote_atomically(tmp_path, final_path, record=False)
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
                            progress=None, protection=None) -> int:
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
        protect_kwargs = protection.save_kwargs() if protection is not None else {}
        try:
            out_doc.save(str(tmp_path), garbage=3, deflate=True, **protect_kwargs)
            # Reuse the merged-PDF validation: openable, page count, and (when
            # deliberately protected) reopenable with its own password.
            _validate_merged_pdf(
                tmp_path, expected_pages=page_count,
                password=protection.password if protect_kwargs else None,
            )
            out_path = promote_atomically(tmp_path, out_path)
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


def _image_content_key(item) -> str:
    """Content identity for a painted image (MuPDF digest + pixel size).

    Deduplicating on this instead of the xref means the *same* picture stored
    under several different xrefs is extracted once, which is what "distinct
    images" promises.
    """
    digest = item.get("digest")
    width, height = item.get("width", 0), item.get("height", 0)
    if digest:
        text = digest.hex() if isinstance(digest, (bytes, bytearray)) else str(digest)
        return f"{text}:{width}x{height}"
    return f"xref{item.get('xref', 0)}:{width}x{height}"


def _iter_unique_images(doc):
    """Yield ``(xref, first_page_number, per_page_index)`` for each distinct
    embedded image, in first-appearance order.

    Only images actually **painted** on a page are considered, so unused
    resource entries are never reported as extractable. Distinctness is by
    image *content*, so the same picture referenced from many pages (e.g. a
    watermark) - or stored under several xrefs - is yielded once, for the first
    page it appears on. Tiny placeholder images are skipped.
    """
    seen = set()
    for page_index in range(doc.page_count):
        counter = 0
        try:
            infos = doc[page_index].get_image_info(hashes=True, xrefs=True)
        except Exception as exc:  # noqa: BLE001 - a broken page must not stop it
            logger.warning("Image scan failed on page %d: %s", page_index + 1, exc)
            continue
        for item in infos:
            xref = int(item.get("xref", 0) or 0)
            width, height = item.get("width", 0), item.get("height", 0)
            if width < _MIN_EXTRACT_SIDE or height < _MIN_EXTRACT_SIDE:
                continue
            if not xref:
                continue  # inline image: no extractable object
            counter += 1
            key = _image_content_key(item)
            if key in seen:
                continue
            seen.add(key)
            yield xref, page_index + 1, counter


def _smask_xref(doc, xref: int) -> int:
    """Return the image's soft-mask (/SMask) xref, or 0 when it has none."""
    try:
        kind, value = doc.xref_get_key(xref, "SMask")
    except Exception:  # noqa: BLE001
        return 0
    if kind != "xref" or not value:
        return 0
    try:
        return int(str(value).split()[0])
    except (ValueError, IndexError):
        return 0


def _write_image_with_alpha(doc, xref: int, smask_xref: int, out_dir: Path,
                            final_path: Path) -> None:
    """Write a PNG whose alpha channel is rebuilt from the image's soft mask."""
    pymupdf = _import_pymupdf()
    base = pymupdf.Pixmap(doc, xref)
    mask = pymupdf.Pixmap(doc, smask_xref)
    try:
        if base.colorspace and base.colorspace.n > 3:
            base = pymupdf.Pixmap(pymupdf.csRGB, base)
        if base.alpha:  # already carries alpha; the mask would be redundant
            combined = base
        else:
            combined = pymupdf.Pixmap(base, mask)
        _atomic_pixmap_save(combined, out_dir, final_path, "png")
    finally:
        base = mask = None


def _composite_on_white(pymupdf, doc, pixmap, smask_xref: int):
    """Composite a soft-masked image over an opaque white background."""
    try:
        mask = pymupdf.Pixmap(doc, smask_xref)
        if pixmap.colorspace and pixmap.colorspace.n > 3:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        with_alpha = pixmap if pixmap.alpha else pymupdf.Pixmap(pixmap, mask)
        white = pymupdf.Pixmap(with_alpha.colorspace, with_alpha.irect, False)
        white.clear_with(255)
        # Draw the transparent image onto the white sheet.
        white.copy(with_alpha, with_alpha.irect)
        return white
    except Exception as exc:  # noqa: BLE001 - fall back to the plain pixmap
        logger.warning("Soft-mask compositing failed for xref %d: %s", smask_xref, exc)
        return pixmap


def _atomic_pixmap_save(pixmap, out_dir: Path, final_path: Path, fmt: str,
                        jpg_quality=None) -> None:
    """Save a pixmap via temp file -> validate -> atomic rename."""
    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_dir)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        if jpg_quality is None:
            pixmap.save(str(tmp_path), output=fmt)
        else:
            pixmap.save(str(tmp_path), output=fmt, jpg_quality=jpg_quality)
        _validate_image_file(tmp_path)
        final_path = promote_atomically(tmp_path, final_path, record=False)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise


def count_embedded_images(doc) -> int:
    """Number of distinct extractable images in the document."""
    return sum(1 for _ in _iter_unique_images(doc))


def extract_embedded_images(doc, out_dir: Path, jpeg_quality=None,
                            progress=None) -> List[Path]:
    """Extract every distinct embedded image into ``out_dir``.

    With ``jpeg_quality=None`` (Original mode) the raw embedded bytes are
    written untouched in their native format (JPEG stays JPEG, etc.) - zero
    quality loss. Otherwise each image is decoded and re-encoded as JPEG at
    the given quality. Files are named ``p<page>_<n>.<ext>`` after the first
    page the image appears on. Never overwrites existing files. Returns the
    list of created paths.
    """
    pymupdf = _import_pymupdf()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    items = list(_iter_unique_images(doc))
    total = len(items)
    created: List[Path] = []
    logger.debug(
        "Extracting %d distinct image(s) to '%s' (quality=%s).",
        total, out_dir, jpeg_quality if jpeg_quality is not None else "original",
    )

    for index, (xref, page_number, per_page) in enumerate(items, start=1):
        smask_xref = _smask_xref(doc, xref)
        if jpeg_quality is None and smask_xref:
            # Transparent image: the stored bytes carry no alpha (it lives in a
            # separate /SMask object), so copying them raw would silently drop
            # the transparency. Rebuild alpha from the mask and write a PNG.
            final_path = unique_file_path(out_dir / f"p{page_number}_{per_page}.png")
            _write_image_with_alpha(doc, xref, smask_xref, out_dir, final_path)
            created.append(final_path)
            if progress is not None:
                progress(index, total)
            continue
        if jpeg_quality is None:
            info = doc.extract_image(xref)
            data, ext = info.get("image"), info.get("ext", "png")
            if not data:
                logger.warning("Image xref %d yielded no data; skipped.", xref)
                continue
            final_path = unique_file_path(
                out_dir / f"p{page_number}_{per_page}.{ext}"
            )
            tmp_fd, tmp_name = tempfile.mkstemp(
                suffix=".tmp", prefix=".pdfforge_", dir=str(out_dir)
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(tmp_fd, "wb") as handle:
                    handle.write(data)
                if tmp_path.stat().st_size <= 0:
                    raise PdfOpenError("Extracted image is empty.")
                final_path = promote_atomically(tmp_path, final_path, record=False)
            except Exception:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    logger.warning("Failed to remove temporary file: %s", tmp_path)
                raise
        else:
            pixmap = pymupdf.Pixmap(doc, xref)
            # JPEG cannot store transparency. Composite the soft mask over a
            # white background first (documented), so a transparent logo comes
            # out looking like it does on the page instead of with its masked
            # areas filled by whatever raw pixels sat underneath.
            if smask_xref:
                pixmap = _composite_on_white(pymupdf, doc, pixmap, smask_xref)
            # JPEG needs plain RGB or grayscale without alpha. Converting the
            # colorspace keeps an alpha channel, so drop alpha separately.
            if pixmap.colorspace and pixmap.colorspace.n > 3:
                pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
            if pixmap.alpha:
                pixmap = pymupdf.Pixmap(pixmap, 0)  # remove the alpha channel
            final_path = unique_file_path(
                out_dir / f"p{page_number}_{per_page}.jpg"
            )
            tmp_fd, tmp_name = tempfile.mkstemp(
                suffix=".tmp", prefix=".pdfforge_", dir=str(out_dir)
            )
            os.close(tmp_fd)
            tmp_path = Path(tmp_name)
            try:
                pixmap.save(str(tmp_path), output="jpg", jpg_quality=jpeg_quality)
                _validate_image_file(tmp_path)
                final_path = promote_atomically(tmp_path, final_path, record=False)
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
        if progress is not None:
            progress(index, total)

    elapsed = time.perf_counter() - started
    logger.info(
        "Extracted %d image(s) into '%s' in %.2fs.", len(created), out_dir, elapsed
    )
    return created
