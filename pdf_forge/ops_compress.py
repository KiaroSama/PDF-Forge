from __future__ import annotations

from typing import Optional, Tuple

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .compress import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403

__all__ = ['operation_compress_pdf', 'operation_compress_pdf_batch',
           '_prompt_compression_level', '_format_size', '_warn_if_cap_above_max',
           '_folder_dpi_stats']


def _format_size(num_bytes: int) -> str:
    """Render a byte count as a human-friendly size string."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


def _prompt_compression_level() -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """Ask for the compression level. Returns (label, jpeg_quality, dpi_target).

    Ultra returns (label, None, None) = lossless-only. Returns None to go back.
    """
    prompt = question_prompt(
        "Compression level",
        details=(
            "1=Very low (smallest file), 2=Low, 3=Medium, 4=High, "
            "5=Very high (near-invisible change), "
            "6=Ultra (zero quality change), 7=Custom"
        ),
        default="5",
    )
    choices = {
        "1": "very low", "2": "low", "3": "medium",
        "4": "high", "5": "very high", "6": "ultra",
    }
    while True:
        raw = _input(prompt).strip().lower()
        if raw == "":
            raw = "5"  # Enter selects Very high (least visible change with real savings).
        if raw == "0":
            return None
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        if raw in choices:
            label = choices[raw]
            preset = COMPRESSION_PRESETS[label]
            if preset is None:
                return label, None, None
            quality, dpi = preset
            return label, quality, dpi
        if raw in ("7", "custom"):
            custom = _prompt_custom_compression()
            if custom is None:
                continue  # 0 = back to the level selection.
            quality, dpi = custom
            return "custom", quality, dpi
        print_error("Invalid level. Please choose 1-7.")


def _prompt_custom_compression() -> Optional[Tuple[int, int]]:
    """Ask for a custom (jpeg_quality, dpi_target). Returns None to go back."""
    quality_prompt = question_prompt("JPEG quality", details="1-100", default="80")
    while True:
        raw = _input(quality_prompt).strip().lower()
        if raw == "":
            raw = "80"
        if raw == "0":
            return None
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        try:
            quality = int(raw)
        except ValueError:
            print_error("Please enter a whole number between 1 and 100.")
            continue
        if not 1 <= quality <= 100:
            print_error("Quality must be between 1 and 100.")
            continue
        break

    dpi_prompt = question_prompt("Target image DPI", details="50-600", default="150")
    while True:
        raw = _input(dpi_prompt).strip().lower()
        if raw == "":
            raw = "150"
        if raw == "0":
            return None
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        try:
            dpi = int(raw)
        except ValueError:
            print_error("Please enter a whole number between 50 and 600.")
            continue
        if not 50 <= dpi <= 600:
            print_error("DPI must be between 50 and 600.")
            continue
        return quality, dpi


def operation_compress_pdf() -> None:
    """Compress a PDF into a smaller new file; the original is never modified."""
    reset_questions()
    print_heading("\nCompress PDF (reduce file size)")
    logger.info("Operation started: Compress PDF.")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    # Open once at configuration time: validates the file, handles the
    # password prompt early, and reads the page count for the summary.
    try:
        doc = open_source_pdf(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s': %s", source, exc)
        return
    total_pages = doc.page_count
    dpi_stats = scan_image_dpi_stats(doc)
    # Capture the working password so the queued runner reopens silently (A13).
    pw = source_password(doc)
    close_doc(doc)

    original_size = source.stat().st_size
    print_success(
        f"Loaded '{source.name}' - {total_pages} page(s), {_format_size(original_size)}."
    )
    if dpi_stats is not None:
        print_kv(
            "Current image DPI",
            f"~{dpi_stats['median']} median (min {dpi_stats['min']}, "
            f"max {dpi_stats['max']}; {dpi_stats['count']} image(s) measured)",
            Color.GOLD,
        )
        logger.info("Image DPI stats for '%s': %s", source, dpi_stats)
    else:
        print_note(
            "No raster images found - this is a text/vector PDF. Text is never "
            "degraded by compression: every level applies the same lossless "
            "work (font subsetting, deduplication, stream compression), so "
            "Ultra is effectively equal to the lossy levels here."
        )
    print_note(
        "Ultra only optimizes structure and fonts (zero quality change). The "
        "other levels also downsample and re-encode embedded images - on "
        "scanned/image-only PDFs that affects the whole page, so savings are "
        "big but quality loss is visible at low levels."
    )

    level = _prompt_compression_level()
    if level is None:
        return
    label, jpeg_quality, dpi_target = level

    _warn_if_cap_above_max(dpi_target, jpeg_quality, dpi_stats)

    default_path = unique_file_path(source.parent / f"{source.stem}_compressed.pdf")

    print_heading("\nSummary")
    print_kv("Source file", source.name, Color.CYAN)
    print_kv("Total source pages", total_pages, Color.GOLD)
    print_kv("Current size", _format_size(original_size), Color.MAGENTA)
    if jpeg_quality is None:
        print_kv("Level", "ultra (lossless only, zero quality change)", Color.LIME)
    else:
        print_kv(
            "Level",
            f"{label} (JPEG quality {jpeg_quality}, images capped at "
            f"{dpi_target} DPI)",
            Color.LIME,
        )
    print_kv("Default Output Path", default_path, Color.AQUA)

    out_path = _choose_output_file(default_path, source)
    if out_path is None:
        print_warning("Returning to menu.")
        return

    def _run():
        try:
            stats = compress_pdf(
                source, out_path, jpeg_quality, dpi_target, password=pw,
            )
        except Exception as exc:  # noqa: BLE001 - clean message, log details
            print_error(f"Failed to compress the PDF: {exc}")
            logger.exception("Compression failed for output '%s'", out_path)
            return
        old, new = stats["original_size"], stats["new_size"]
        saved = old - new
        if saved > 0:
            percent = 100.0 * saved / old
            print_success(
                f"Done. {_format_size(old)} -> {_format_size(new)} "
                f"(saved {_format_size(saved)}, {percent:.1f}%):\n  {out_path}"
            )
        else:
            print_warning(
                f"Done, but no size reduction was possible "
                f"({_format_size(old)} -> {_format_size(new)}). The file was "
                f"already efficiently compressed:\n  {out_path}"
            )

    queue_task(
        f"Compress {source.name} ({label}) -> {out_path.name}",
        _run,
    )


def _warn_if_cap_above_max(dpi_target, jpeg_quality, dpi_stats) -> None:
    """Warn when the chosen DPI cap can't downsample anything.

    The DPI cap is the criterion: images above it come down (in
    quality-preserving steps, never below the cap), images at or below it are
    left as-is - so no image is ever enlarged. When the cap is at or above the
    document's own maximum image DPI, nothing needs downsampling.
    """
    if jpeg_quality is None or dpi_stats is None or "max" not in dpi_stats:
        return
    if dpi_target >= dpi_stats["max"]:
        print_warning(
            f"Your {dpi_target} DPI cap is at or above the maximum image "
            f"resolution present (~{dpi_stats['max']} DPI): no image will be "
            "downsampled (none is above the cap). Only JPEG re-encoding "
            f"(quality {jpeg_quality}) and the lossless optimizations apply."
        )


def _folder_dpi_stats(pdfs) -> dict:
    """Aggregate image-DPI stats across every PDF in a folder.

    Opens each file once (empty password only - encrypted files are not
    prompted here) and sorts every file into one of three honest buckets:
      * ``files_with_images``  - scanned, raster images found,
      * ``files_text_only``    - scanned, no raster images (text/vector),
      * ``files_not_scanned``  - could not be inspected (encrypted / unreadable).

    Always returns a dict with those three counts. ``min``/``median``/``max``
    are present only when at least one file contributed image DPIs. A folder of
    only-encrypted files therefore reports "not scanned", never a false
    "no raster images".
    """
    all_dpis = []
    files_with_images = 0
    files_text_only = 0
    files_not_scanned = 0
    for src in pdfs:
        try:
            doc = open_source_pdf(src)
        except (PdfOpenError, RuntimeError) as exc:
            logger.warning("DPI scan skipped for '%s': %s", src, exc)
            files_not_scanned += 1
            continue
        try:
            stats = scan_image_dpi_stats(doc)
        finally:
            doc.close()
        if stats is None:
            files_text_only += 1
        else:
            files_with_images += 1
            # Reconstruct representative points: min, median, max per file.
            all_dpis += [stats["min"], stats["median"], stats["max"]]
    result = {
        "files_with_images": files_with_images,
        "files_text_only": files_text_only,
        "files_not_scanned": files_not_scanned,
    }
    if all_dpis:
        all_dpis.sort()
        result["min"] = all_dpis[0]
        result["max"] = all_dpis[-1]
        result["median"] = all_dpis[len(all_dpis) // 2]
    return result


def operation_compress_pdf_batch() -> None:
    """Compress every PDF in a folder into its own ``<name>_compressed.pdf``.

    A failure on one file is reported and skipped without stopping the batch.
    """
    reset_questions()
    print_heading("\nCompress PDF: batch folder")
    logger.info("Operation started: Compress PDF (batch folder).")

    pdfs = prompt_source_folder_pdfs()
    if pdfs is None:
        return

    folder = pdfs[0].parent
    print_info(f"Scanning image resolution across {len(pdfs)} file(s)...")
    dpi_stats = _folder_dpi_stats(pdfs)

    not_scanned = dpi_stats["files_not_scanned"]
    print_heading("\nSummary")
    print_kv("Folder", folder, Color.CYAN)
    print_kv("PDF files", len(pdfs), Color.MAGENTA)
    if "max" in dpi_stats:
        print_kv(
            "Image DPI across folder",
            f"min {dpi_stats['min']}, median {dpi_stats['median']}, "
            f"max {dpi_stats['max']}",
            Color.GOLD,
        )
        parts = [f"{dpi_stats['files_with_images']} file(s) contain images"]
        if dpi_stats["files_text_only"]:
            parts.append(f"{dpi_stats['files_text_only']} are text/vector only")
        if not_scanned:
            parts.append(f"{not_scanned} could not be scanned (encrypted/unreadable)")
        print_note("; ".join(parts) + ".")
        logger.info("Folder DPI stats for '%s': %s", folder, dpi_stats)
    elif dpi_stats["files_text_only"] and not not_scanned:
        print_note(
            "No raster images found in any scanned file - these are text/vector "
            "PDFs. Compression is lossless for text; every level behaves like Ultra."
        )
    elif dpi_stats["files_text_only"] and not_scanned:
        print_note(
            f"No raster images in the {dpi_stats['files_text_only']} scanned "
            f"file(s) (text/vector); {not_scanned} could not be scanned "
            "(encrypted/unreadable) and were not inspected."
        )
    else:
        # Nothing could be inspected at all (e.g. every file is encrypted).
        print_warning(
            f"None of the {not_scanned} file(s) could be scanned for image "
            "resolution (encrypted or unreadable). Image-DPI stats are "
            "unavailable; compression will still be attempted per file."
        )

    level = _prompt_compression_level()
    if level is None:
        return
    label, jpeg_quality, dpi_target = level

    _warn_if_cap_above_max(dpi_target, jpeg_quality, dpi_stats)

    print_kv("Level", label, Color.LIME)
    print_kv("Per-file output", "<name>_compressed.pdf beside each PDF", Color.AQUA)
    print_note(BATCH_PASSWORD_NOTICE)
    print(colorize("\n  Files:", Color.GRAY))
    _print_merge_order(pdfs)

    def _run():
        logger.info(
            "Batch compress start: folder='%s' files=%d level=%s",
            folder, len(pdfs), label,
        )
        ok = failed = 0
        total_old = total_new = 0
        for index, src in enumerate(pdfs, start=1):
            print_info(f"[{index}/{len(pdfs)}] {src.name}")
            out_path = unique_file_path(src.parent / f"{src.stem}_compressed.pdf")
            if resolves_to_same_file(out_path, src):
                out_path = unique_file_path(src.parent / f"{src.stem}_smaller.pdf")
            try:
                stats = compress_pdf(
                    src, out_path, jpeg_quality, dpi_target,
                    password_prompt=prompt_password,
                )
            except Exception as exc:  # noqa: BLE001 - keep the batch going
                print_error(f"  Failed: {exc}")
                logger.exception("Batch compress failed for '%s'", src)
                failed += 1
                continue
            old, new = stats["original_size"], stats["new_size"]
            total_old += old
            total_new += new
            ok += 1
            # Size change: negative means the file got smaller.
            delta_pct = (100.0 * (new - old) / old) if old else 0.0
            print_success(
                f"  -> {_format_size(old)} -> {_format_size(new)} "
                f"({delta_pct:+.1f}%) {out_path.name}"
            )

        saved_total = total_old - total_new
        head = (
            f"Done. Compressed {ok} file(s), {failed} failed. "
            f"Total {_format_size(total_old)} -> {_format_size(total_new)}"
        )
        if total_old and saved_total > 0:
            pct = 100.0 * saved_total / total_old
            print_success(f"{head} (saved {_format_size(saved_total)}, {pct:.1f}%).")
        elif total_old and saved_total < 0:
            # Aggregate output grew: never render a negative "saved" size. Report
            # the increase as a warning, not a success claim.
            grew = total_new - total_old
            pct = 100.0 * grew / total_old
            print_warning(
                f"{head} (increased by {_format_size(grew)}, +{pct:.1f}%). "
                "The set was already efficiently compressed; some outputs got "
                "larger. The originals are unchanged - you can discard the outputs."
            )
        else:
            print_success(f"{head} (no net size change).")
        logger.info(
            "Batch compress complete: ok=%d failed=%d saved=%d bytes.",
            ok, failed, saved_total,
        )

    queue_task(
        f"Compress batch: {len(pdfs)} file(s) in {folder.name} ({label})",
        _run,
    )
