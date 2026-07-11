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
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .render import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403

__all__ = ['operation_images_all_pages', 'operation_images_selected_pages', '_render_pngs_and_report', 'operation_images_batch_folder', 'operation_pdf_to_image_pdf', 'operation_image_pdf_batch_folder']

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
        print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
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
                rpdf, _count = open_render_document(
                    source, password_prompt=prompt_password
                )
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
        print_success(f"Loaded '{source.name}' - {total_pages} page(s).")

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
                rpdf, _count = open_render_document(
                    source, password_prompt=prompt_password
                )
            except (PdfOpenError, RuntimeError) as exc:
                print_error(str(exc))
                logger.error("Failed to reopen '%s' for rendering: %s", source, exc)
                return
            try:
                _render_pngs_and_report(rpdf, pages_zero_based, out_dir, dpi)
            finally:
                rpdf.close()

        queue_task(
            f"PDF to PNG ({selection_label}) of {source.name} -> {out_dir.name}",
            _run,
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
        print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
        default_path = unique_file_path(default_image_pdf_output(source))

        print_heading("\nSummary")
        print_kv("Source file", source.name, Color.CYAN)
        print_kv("Total source pages", total_pages, Color.GOLD)
        print_kv("Quality", f"{dpi} DPI", Color.PINK)
        print_kv("Result", "image-only PDF (not editable)", Color.LIME)
        print_kv("Default Output Path", default_path, Color.AQUA)

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
                rpdf, count = open_render_document(
                    source, password_prompt=prompt_password
                )
            except (PdfOpenError, RuntimeError) as exc:
                print_error(str(exc))
                logger.error("Failed to reopen '%s' for rendering: %s", source, exc)
                return
            logger.info(
                "Image-only PDF start: pages=%d dpi=%d output='%s'",
                count, dpi, out_path,
            )
            try:
                written = render_pdf_to_image_pdf(
                    rpdf,
                    count,
                    out_path,
                    dpi,
                    progress=lambda c, t: _print_progress("Rasterizing pages", c, t),
                )
                print_success(
                    f"Done. Wrote {written} rasterized page(s) to:\n  {out_path}"
                )
                logger.info(
                    "Image-only PDF complete: output='%s' pages=%d", out_path, written
                )
            except Exception as exc:  # noqa: BLE001 - clean message, log details
                print_error(f"Failed to create the image-only PDF: {exc}")
                logger.exception("Image-only PDF failed for output '%s'", out_path)
            finally:
                rpdf.close()

        queue_task(
            f"Image-only PDF of {source.name} -> {out_path.name}",
            _run,
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
                written = render_pdf_to_image_pdf(
                    pdf, page_count, out_path, dpi,
                    progress=lambda c, t: _print_progress("  Rasterizing", c, t),
                )
                total_pages += written
                ok += 1
                print_success(f"  -> {out_path.name} ({written} page(s))")
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
        logger.info(
            "Batch image-only PDF complete: ok=%d failed=%d pages=%d",
            ok, failed, total_pages,
        )

    queue_task(
        f"Image-only PDF (batch: {len(pdfs)} file(s) in {folder.name})",
        _run,
    )
