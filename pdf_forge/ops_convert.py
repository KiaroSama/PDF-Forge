from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .render import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403

__all__ = ['operation_images_all_pages', 'operation_images_selected_pages', '_render_pngs_and_report', 'operation_images_batch_folder', 'operation_pdf_to_image_pdf', 'operation_image_pdf_batch_folder', '_warn_if_dpi_exceeds_source', '_prompt_extract_quality', 'operation_extract_images']


def _warn_if_dpi_exceeds_source(doc, dpi: int) -> None:
    """Warn when rendering a scanned PDF above its own image resolution.

    Only fires for image-only documents: a text/vector PDF genuinely gains
    sharpness from a higher render DPI, so no warning is shown there.
    """
    stats = scan_image_dpi_stats(doc)
    if stats is None or has_meaningful_text(doc):
        return
    if dpi > stats["max"]:
        print_warning(
            f"This looks like a scanned/image-only PDF at ~{stats['max']} DPI. "
            f"Rendering at {dpi} DPI cannot add detail beyond the source - it "
            "only produces larger files."
        )
        logger.info(
            "DPI warning: render dpi=%d exceeds source image max dpi=%d.",
            dpi, stats["max"],
        )


def operation_images_all_pages() -> None:
    """Render every page of a PDF to a PNG named after its page number."""
    reset_questions()
    print_heading("\nPDF to images: all pages")
    logger.info("Operation started: PDF to images (all pages).")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    try:
        pdf, total_pages = open_render_document(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s' for rendering: %s", source, exc)
        return

    try:
        # Capture the working password now so the queued runner can reopen the
        # source silently - no password prompt during queue execution (A13).
        pw = source_password(pdf)
        print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
        _warn_if_dpi_exceeds_source(pdf, dpi)
        default_folder = unique_dir_path(default_images_output_dir(source))

        print_heading("\nSummary")
        print_kv("Source file", source.name, Color.CYAN)
        print_kv("Total source pages", total_pages, Color.GOLD)
        print_kv("Pages to export", f"all ({total_pages})", Color.LIME)
        print_kv("Image format", "PNG", Color.ORANGE)
        print_kv("Quality", f"{dpi} DPI", Color.PINK)
        print_kv("Output directory", default_folder, Color.AQUA)

        out_dir = _choose_output_dir(default_folder)
        if out_dir is None:
            print_warning("Returning to menu.")
            return

        pages_zero_based = list(range(total_pages))

        def _run():
            try:
                rpdf, _count = open_render_document(source, password=pw)
            except (PdfOpenError, RuntimeError) as exc:
                print_error(str(exc))
                logger.error("Failed to reopen '%s' for rendering: %s", source, exc)
                return
            try:
                _render_pngs_and_report(rpdf, pages_zero_based, out_dir, dpi)
            finally:
                rpdf.close()

        queue_task(
            f"PDF to PNG (all {total_pages} page(s)) of {source.name} "
            f"-> {out_dir.name}",
            _run,
            # Identity of every source this task was configured against;
            # the queue re-verifies it just before running (C-06).
            sources=[capture_file_source(source)],
        )
    finally:
        pdf.close()


def operation_images_selected_pages() -> None:
    """Render a chosen selection of pages to PNGs named after their page number."""
    reset_questions()
    print_heading("\nPDF to images: selected pages")
    logger.info("Operation started: PDF to images (selected pages).")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    try:
        pdf, total_pages = open_render_document(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s' for rendering: %s", source, exc)
        return

    try:
        # Capture the working password for a silent reopen in the runner (A13).
        pw = source_password(pdf)
        print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
        _warn_if_dpi_exceeds_source(pdf, dpi)

        selection_prompt = question_prompt(
            "Pages to export as images",
            details="e.g. 5 or 10-20 or 10-20,25,30-50",
        )
        while True:
            expression = _input(selection_prompt).strip()
            if expression == "0":
                return
            if expression.lower() in ("exit", "quit"):
                raise _ExitRequested()
            try:
                result = parse_page_selection(expression, total_pages)
                break
            except PageSelectionError as exc:
                print_error(f"Invalid selection: {exc}")

        if result.duplicates_removed:
            print_warning("Duplicate pages were removed; first occurrence kept.")
        logger.info(
            "Image selection parsed: expression='%s' pages=%d",
            expression, len(result.pages),
        )

        default_folder = unique_dir_path(default_images_output_dir(source))

        print_heading("\nSummary")
        print_kv("Source file", source.name, Color.CYAN)
        print_kv("Total source pages", total_pages, Color.GOLD)
        print_kv("Pages to export", summarize_ranges(result.pages), Color.LIME)
        print_kv("Image count", len(result.pages), Color.MAGENTA)
        print_kv("Image format", "PNG", Color.ORANGE)
        print_kv("Quality", f"{dpi} DPI", Color.PINK)
        print_kv("Output directory", default_folder, Color.AQUA)

        out_dir = _choose_output_dir(default_folder)
        if out_dir is None:
            print_warning("Returning to menu.")
            return

        pages_zero_based = [p - 1 for p in result.pages]
        selection_label = summarize_ranges(result.pages)

        def _run():
            try:
                rpdf, _count = open_render_document(source, password=pw)
            except (PdfOpenError, RuntimeError) as exc:
                print_error(str(exc))
                logger.error("Failed to reopen '%s' for rendering: %s", source, exc)
                return
            try:
                _render_pngs_and_report(rpdf, pages_zero_based, out_dir, dpi)
            finally:
                close_doc(rpdf)

        queue_task(
            f"PDF to PNG ({selection_label}) of {source.name} -> {out_dir.name}",
            _run,
            # Identity of every source this task was configured against;
            # the queue re-verifies it just before running (C-06).
            sources=[capture_file_source(source)],
        )
    finally:
        pdf.close()


def _render_pngs_and_report(pdf, pages_zero_based: Sequence[int], out_dir: Path,
                            dpi: int) -> None:
    """Render pages to PNGs, then report the result (shared by both PNG flows)."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print_error(f"Could not create output directory: {exc}")
        logger.error("Failed to create output dir '%s': %s", out_dir, exc)
        return

    try:
        created = render_pages_to_pngs(
            pdf,
            pages_zero_based,
            out_dir,
            dpi,
            progress=lambda c, t: _print_progress("Rendering pages", c, t),
        )
    except Exception as exc:  # noqa: BLE001 - present a clean message, log details
        print_error(f"Failed to render images: {exc}")
        logger.exception("PNG rendering failed for output '%s'", out_dir)
        return

    print_success(
        f"Done. Created {len(created)} image(s) in:\n  {out_dir}"
    )
    logger.info("PDF-to-images complete: images=%d dir='%s'", len(created), out_dir)


def operation_images_batch_folder() -> None:
    """Batch: render every page of every PDF in a folder to PNG images.

    Each PDF is converted independently into its own ``<name>_images`` folder.
    A failure on one file is reported and skipped without stopping the batch.
    """
    reset_questions()
    print_heading("\nPDF to images: batch folder")
    logger.info("Operation started: PDF to images (batch folder).")

    pdfs = prompt_source_folder_pdfs()
    if pdfs is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    folder = pdfs[0].parent
    print_heading("\nSummary")
    print_kv("Folder", folder, Color.CYAN)
    print_kv("PDF files", len(pdfs), Color.MAGENTA)
    print_kv("Image format", "PNG", Color.ORANGE)
    print_kv("Quality", f"{dpi} DPI", Color.PINK)
    print_kv("Per-file output", "<name>_images folder beside each PDF", Color.AQUA)
    print_note(BATCH_PASSWORD_NOTICE)
    print(colorize("\n  Files:", Color.GRAY))
    _print_merge_order(pdfs)

    def _run():
        logger.info(
            "Batch image start: folder='%s' files=%d dpi=%d", folder, len(pdfs), dpi
        )
        ok = failed = total_images = 0
        for index, src in enumerate(pdfs, start=1):
            print_info(f"[{index}/{len(pdfs)}] {src.name}")
            try:
                pdf, page_count = open_render_document(
                    src, password_prompt=prompt_password
                )
            except (PdfOpenError, RuntimeError) as exc:
                print_error(f"  Skipped (could not open): {exc}")
                logger.error("Batch image: failed to open '%s': %s", src, exc)
                failed += 1
                continue
            try:
                out_dir = unique_dir_path(default_images_output_dir(src))
                created = render_pages_to_pngs(
                    pdf, list(range(page_count)), out_dir, dpi,
                    progress=lambda c, t: _print_progress("  Rendering", c, t),
                )
                total_images += len(created)
                ok += 1
                print_success(f"  -> {len(created)} image(s) in {out_dir.name}")
            except Exception as exc:  # noqa: BLE001 - keep the batch going
                print_error(f"  Failed: {exc}")
                logger.exception("Batch image render failed for '%s'", src)
                failed += 1
            finally:
                pdf.close()

        print_success(
            f"Done. Converted {ok} file(s), {failed} failed, "
            f"{total_images} image(s) total."
        )
        logger.info(
            "Batch image complete: ok=%d failed=%d images=%d", ok, failed, total_images
        )

    queue_task(
        f"PDF to PNG (batch: {len(pdfs)} file(s) in {folder.name})",
        _run,
        # Identity of every source this task was configured against;
        # the queue re-verifies it just before running (C-06).
        sources=[capture_file_source(p) for p in pdfs],
    )


def operation_pdf_to_image_pdf() -> None:
    """Rasterize a single PDF and rebuild it as an image-only (non-editable) PDF."""
    reset_questions()
    print_heading("\nPDF to image-only PDF: single file")
    logger.info("Operation started: PDF to image-only PDF (single file).")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    try:
        pdf, total_pages = open_render_document(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s' for rendering: %s", source, exc)
        return

    try:
        # Capture the working password for a silent reopen in the runner (A13).
        pw = source_password(pdf)
        protection = detect_protection(pdf)
        print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
        _warn_if_dpi_exceeds_source(pdf, dpi)
        default_path = unique_file_path(default_image_pdf_output(source))

        print_heading("\nSummary")
        print_kv("Source file", source.name, Color.CYAN)
        print_kv("Total source pages", total_pages, Color.GOLD)
        print_kv("Quality", f"{dpi} DPI", Color.PINK)
        print_kv("Result", "image-only PDF (not editable)", Color.LIME)
        print_kv("Default Output Path", default_path, Color.AQUA)

        protection = resolve_protection(protection, context="image-only PDF")
        if protection is None:
            print_warning("Cancelled. Returning to menu.")
            return

        out_path = _choose_output_file(default_path, source)
        if out_path is None:
            print_warning("Returning to menu.")
            return

        print_warning(
            "The output will be rasterized: text becomes images and is no longer "
            "selectable or editable. This usually increases the file size."
        )
        def _run():
            try:
                rpdf, count = open_render_document(source, password=pw)
            except (PdfOpenError, RuntimeError) as exc:
                print_error(str(exc))
                logger.error("Failed to reopen '%s' for rendering: %s", source, exc)
                return
            logger.info(
                "Image-only PDF start: pages=%d dpi=%d output='%s'",
                count, dpi, out_path,
            )
            try:
                result = render_pdf_to_image_pdf(
                    rpdf,
                    count,
                    out_path,
                    dpi,
                    progress=lambda c, t: _print_progress("Rasterizing pages", c, t),
                    protection=protection,
                )
                # The written path, not the configured one: promotion may have
                # had to allocate a suffixed sibling.
                print_success(
                    f"Done. Wrote {result.count} rasterized page(s) to:"
                    f"\n  {result.path}"
                )
                logger.info(
                    "Image-only PDF complete: output='%s' pages=%d",
                    result.path, result.count,
                )
            except Exception as exc:  # noqa: BLE001 - clean message, log details
                print_error(f"Failed to create the image-only PDF: {exc}")
                logger.exception("Image-only PDF failed for output '%s'", out_path)
            finally:
                rpdf.close()

        queue_task(
            f"Image-only PDF of {source.name} -> {out_path.name}",
            _run,
            # Identity of every source this task was configured against;
            # the queue re-verifies it just before running (C-06).
            sources=[capture_file_source(source)],
        )
    finally:
        pdf.close()


def operation_image_pdf_batch_folder() -> None:
    """Batch: rasterize every PDF in a folder into its own image-only PDF.

    Each PDF becomes ``<name>_image.pdf`` beside it. A failure on one file is
    reported and skipped without stopping the batch.
    """
    reset_questions()
    print_heading("\nPDF to image-only PDF: batch folder")
    logger.info("Operation started: PDF to image-only PDF (batch folder).")

    pdfs = prompt_source_folder_pdfs()
    if pdfs is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    folder = pdfs[0].parent
    print_heading("\nSummary")
    print_kv("Folder", folder, Color.CYAN)
    print_kv("PDF files", len(pdfs), Color.MAGENTA)
    print_kv("Quality", f"{dpi} DPI", Color.PINK)
    print_kv("Result", "image-only PDF per file (not editable)", Color.LIME)
    print_kv("Per-file output", "<name>_image.pdf beside each PDF", Color.AQUA)
    print_note(BATCH_PASSWORD_NOTICE)
    print(colorize("\n  Files:", Color.GRAY))
    _print_merge_order(pdfs)

    print_warning(
        "Each output will be rasterized: text becomes images and is no longer "
        "selectable or editable. This usually increases the file size."
    )
    def _run():
        logger.info(
            "Batch image-only PDF start: folder='%s' files=%d dpi=%d",
            folder, len(pdfs), dpi,
        )
        ok = failed = total_pages = 0
        unprotected_notes = []
        for index, src in enumerate(pdfs, start=1):
            print_info(f"[{index}/{len(pdfs)}] {src.name}")
            try:
                pdf, page_count = open_render_document(
                    src, password_prompt=prompt_password
                )
            except (PdfOpenError, RuntimeError) as exc:
                print_error(f"  Skipped (could not open): {exc}")
                logger.error("Batch image-only PDF: failed to open '%s': %s", src, exc)
                failed += 1
                continue
            try:
                out_path = unique_file_path(default_image_pdf_output(src))
                # Per-file policy without mid-batch prompting: keep an open
                # password; an owner-restricted source cannot be reproduced.
                file_policy = detect_protection(pdf)
                if file_policy.kind == "restricted":
                    file_policy = None
                    unprotected_notes.append(src.name)
                result = render_pdf_to_image_pdf(
                    pdf, page_count, out_path, dpi,
                    progress=lambda c, t: _print_progress("  Rasterizing", c, t),
                    protection=file_policy,
                )
                total_pages += result.count
                ok += 1
                print_success(f"  -> {result.path.name} ({result.count} page(s))")
            except Exception as exc:  # noqa: BLE001 - keep the batch going
                print_error(f"  Failed: {exc}")
                logger.exception("Batch image-only PDF failed for '%s'", src)
                failed += 1
            finally:
                pdf.close()

        print_success(
            f"Done. Converted {ok} file(s), {failed} failed, "
            f"{total_pages} page(s) total."
        )
        if unprotected_notes:
            print_warning(
                f"{len(unprotected_notes)} file(s) had owner restrictions that "
                "cannot be reproduced; their outputs are unprotected: "
                + ", ".join(unprotected_notes[:5])
                + (" ..." if len(unprotected_notes) > 5 else "")
            )
        logger.info(
            "Batch image-only PDF complete: ok=%d failed=%d pages=%d",
            ok, failed, total_pages,
        )

    queue_task(
        f"Image-only PDF (batch: {len(pdfs)} file(s) in {folder.name})",
        _run,
        # Identity of every source this task was configured against;
        # the queue re-verifies it just before running (C-06).
        sources=[capture_file_source(p) for p in pdfs],
    )


def _prompt_extract_quality():
    """Ask how to save extracted images.

    Returns ``("original", None)`` for a lossless raw-bytes copy, ``(label,
    jpeg_quality)`` for a JPEG re-encode, or ``None`` to go back. Enter selects
    Original (no quality loss).
    """
    prompt = question_prompt(
        "Output quality",
        details=(
            "1=Original (no re-encode, lossless), 2=Very low (JPEG 40), "
            "3=Low (60), 4=Medium (75), 5=High (85), 6=Very high (90), "
            "7=Ultra (95), 8=Custom"
        ),
        default="1",
    )
    choices = {
        "2": ("very low", 40), "3": ("low", 60), "4": ("medium", 75),
        "5": ("high", 85), "6": ("very high", 90), "7": ("ultra", 95),
    }
    while True:
        raw = _input(prompt).strip().lower()
        if raw == "":
            raw = "1"  # Enter keeps the original quality (lossless copy).
        if raw == "0":
            return None
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        if raw in ("1", "original"):
            return "original", None
        if raw in choices:
            return choices[raw]
        if raw in ("8", "custom"):
            q_prompt = question_prompt("JPEG quality", details="1-100", default="80")
            while True:
                q_raw = _input(q_prompt).strip().lower()
                if q_raw == "":
                    q_raw = "80"
                if q_raw == "0":
                    break  # back to the quality selection
                if q_raw in ("exit", "quit"):
                    raise _ExitRequested()
                try:
                    quality = int(q_raw)
                except ValueError:
                    print_error("Please enter a whole number between 1 and 100.")
                    continue
                if not 1 <= quality <= 100:
                    print_error("Quality must be between 1 and 100.")
                    continue
                return "custom", quality
            continue
        print_error("Invalid quality. Please choose 1-8.")


def operation_extract_images() -> None:
    """Extract the distinct embedded images of a PDF into a folder."""
    reset_questions()
    print_heading("\nExtract images from PDF")
    logger.info("Operation started: Extract images from PDF.")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise

    if source is None:
        return

    try:
        pdf, total_pages = open_render_document(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s' for image extraction: %s", source, exc)
        return

    try:
        # Capture the working password for a silent reopen in the runner (A13).
        pw = source_password(pdf)
        image_count = count_embedded_images(pdf)
        inline_count = count_inline_images(pdf)
        print_success(
            f"Loaded '{source.name}' - {total_pages} page(s), "
            f"{image_count} distinct image(s)."
        )
        if image_count == 0:
            print_warning(
                "No embedded images were found. This tool extracts raster "
                "images stored inside the PDF; text/vector content has none."
            )
            return
        print_note(
            "The same image reused on many pages (e.g. a watermark) is "
            "extracted once, named after the first page it appears on."
        )
        if inline_count:
            # Say so up front rather than quietly producing fewer files.
            print_warning(
                f"{inline_count} inline image(s) are drawn directly inside the "
                "page content and have no separate image object, so they cannot "
                "be extracted. Use 'PDF to images (PNG)' to capture those pages."
            )

        quality = _prompt_extract_quality()
        if quality is None:
            return
        label, jpeg_quality = quality

        default_folder = unique_dir_path(
            source.parent / f"{source.stem}_extracted_images"
        )

        print_heading("\nSummary")
        print_kv("Source file", source.name, Color.CYAN)
        print_kv("Total source pages", total_pages, Color.GOLD)
        print_kv("Images to extract", image_count, Color.MAGENTA)
        if jpeg_quality is None:
            print_kv(
                "Quality",
                "original (raw bytes, no quality loss; transparent images are "
                "rebuilt as PNG with their alpha)",
                Color.LIME,
            )
        else:
            print_kv("Quality", f"{label} (JPEG quality {jpeg_quality})", Color.LIME)
        print_kv("Output directory", default_folder, Color.AQUA)

        out_dir = _choose_output_dir(default_folder)
        if out_dir is None:
            print_warning("Returning to menu.")
            return

        def _run():
            try:
                rpdf, _count = open_render_document(source, password=pw)
            except (PdfOpenError, RuntimeError) as exc:
                print_error(str(exc))
                logger.error("Failed to reopen '%s' for extraction: %s", source, exc)
                return
            try:
                created = extract_embedded_images(
                    rpdf, out_dir, jpeg_quality,
                    progress=lambda c, t: _print_progress("Extracting images", c, t),
                )
            except Exception as exc:  # noqa: BLE001 - clean message, log details
                print_error(f"Failed to extract images: {exc}")
                logger.exception("Image extraction failed for '%s'", source)
                return
            finally:
                rpdf.close()
            print_success(f"Done. Extracted {len(created)} image(s) in:\n  {out_dir}")
            logger.info(
                "Image extraction complete: images=%d dir='%s'", len(created), out_dir
            )

        queue_task(
            f"Extract {image_count} image(s) ({label}) from {source.name} "
            f"-> {out_dir.name}",
            _run,
            # Identity of every source this task was configured against;
            # the queue re-verifies it just before running (C-06).
            sources=[capture_file_source(source)],
        )
    finally:
        pdf.close()
