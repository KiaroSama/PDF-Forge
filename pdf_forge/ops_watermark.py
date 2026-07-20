from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import List

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .watermark import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403

__all__ = ['_temp_dir', 'cleanup_temp_dir', 'operation_remove_watermark']

def _temp_dir() -> Path:
    """The project-local scratch folder (``PDF Forge/temp``).

    Used for transient files such as watermark preview images. It lives at the
    project root (the parent of the pdf_forge/ package) so it is always in a
    known, writable location regardless of where the source PDF is.
    """
    return Path(__file__).resolve().parent.parent / "temp"


def cleanup_temp_dir() -> None:
    """Remove the project-local temp folder and its contents at startup.

    Ensures any preview images left behind by a previous run (for example after
    an unexpected exit) are cleared. The folder is recreated on demand.
    """
    temp = _temp_dir()
    if temp.exists():
        shutil.rmtree(temp, ignore_errors=True)
        if not temp.exists():
            logger.info("Cleared temp folder at startup: %s", temp)


def operation_remove_watermark() -> None:
    """Detect repeated image watermarks, preview them, and remove the chosen one.

    Only image-based watermarks that repeat across pages can be removed. The
    text layer and all other content are preserved. Preview images are written
    to the project-local ``temp`` folder (``PDF Forge/temp``) so you can confirm
    which image to remove before any change is made; that folder is removed
    automatically when the operation finishes, and any leftovers are cleared on
    the next launch. The original PDF is never modified.
    """
    reset_questions()
    print_heading("\nRemove image watermark")
    logger.info("Operation started: Remove image watermark.")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    try:
        doc = open_source_pdf(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s': %s", source, exc)
        return

    # Capture the working password so the queued runner reopens silently (A13).
    pw = source_password(doc)
    protection = detect_protection(doc)
    total_pages = doc.page_count
    print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
    print_info("Scanning for repeated images (watermark candidates)...")
    candidates, total, skipped = scan_watermark_candidates(doc, with_skipped=True)

    if skipped:
        # Told, not silently dropped: these repeat like a watermark but live in
        # the content stream, so no image object exists to replace (C-14).
        print_warning(
            f"{skipped} repeated inline image(s) were skipped: an inline image "
            "is part of the page content stream, not a removable image object, "
            "so this tool cannot remove it."
        )
        logger.info("Watermark scan skipped %d inline group(s) in '%s'.",
                    skipped, source)

    if not candidates:
        print_warning(
            "No removable repeated images were found. This tool only removes "
            "image-based watermarks that repeat across pages (not text, inline "
            "images, or flattened scans)."
        )
        logger.info("Watermark scan found no removable repeated images in '%s'.",
                    source)
        doc.close()
        return

    # Export previews to the project-local temp folder (always in a known place).
    # Fall back to the system temp folder if that location is not writable.
    preview_dir = unique_dir_path(_temp_dir() / f"{source.stem}_wm_preview")
    try:
        preview_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        preview_dir = Path(tempfile.mkdtemp(prefix="pdfforge_wm_preview_"))
    logger.info("Watermark previews at: %s", preview_dir)
    print_heading("\nWatermark candidates")
    for idx, cand in enumerate(candidates, start=1):
        coverage = len(cand.pages)
        percent = int(coverage * 100 / total) if total else 0
        preview_path = preview_dir / f"candidate_{idx}.png"
        ok = export_watermark_preview(doc, cand, preview_path)
        detail = f"on {coverage}/{total} pages ({percent}%)"
        detail += f" - preview: {preview_path.name}" if ok else " - preview unavailable"
        print_kv(f"[{idx}] {cand.width}x{cand.height}px", detail, Color.LIME)

    print_note(
        "Preview images were created in the temp folder. Open them to check "
        f"each candidate:\n  {preview_dir}\n"
        "(this folder is removed automatically when the operation finishes)"
    )

    try:
        # Let the user pick which candidate(s) to remove.
        sel_prompt = question_prompt(
            "Watermark(s) to remove",
            details="e.g. 1 or 1,3",
            default="1",
        )
        indices: List[int] = []
        while True:
            raw = _input(sel_prompt).strip()
            if raw == "":
                raw = "1"  # Enter selects candidate 1 (the top match).
            if raw == "0":
                print_warning("Returning to menu.")
                return
            if raw.lower() in ("exit", "quit"):
                raise _ExitRequested()
            try:
                indices = parse_index_list(raw, len(candidates))
                break
            except ValueError as exc:
                print_error(str(exc))

        chosen = [candidates[i - 1] for i in indices]
        chosen_sigs = [c.signature for c in chosen]
        affected_pages = set()
        for c in chosen:
            affected_pages |= c.pages

        # Consent BEFORE any output is configured or written (C-13). The
        # resolved policy is captured in the queued task and handed to the
        # writer, so a run-time re-detection never decides it.
        protection = resolve_protection(protection, context="watermark-free PDF")
        if protection is None:
            print_warning("Cancelled. Returning to menu.")
            return

        default_path = unique_file_path(source.parent / f"{source.stem}_no_watermark.pdf")
        out_path = _choose_output_file(default_path, source)
        if out_path is None:
            print_warning("Returning to menu.")
            return

        print_heading("\nSummary")
        print_kv("Source file", source.name, Color.CYAN)
        print_kv("Watermarks to remove", len(chosen), Color.MAGENTA)
        for i, c in zip(indices, chosen):
            print(
                colorize(f"    [{i}] ", Color.GREEN + Color.BOLD)
                + colorize(f"{c.width}x{c.height}px on {len(c.pages)} page(s)", Color.LIME)
            )
        print_kv("Pages affected", len(affected_pages), Color.GOLD)
        print_kv("Output Path", out_path, Color.AQUA)
        logger.info(
            "Watermark removal chosen: candidates=%s pages=%d output='%s'",
            indices, len(affected_pages), out_path,
        )

        def _run():
            # Reopen the source fresh (the configure-time doc is closed after
            # previews, and removal mutates the document in place). The captured
            # password makes this silent - no prompt during queue execution.
            try:
                rdoc = open_source_pdf(source, password=pw)
            except (PdfOpenError, RuntimeError) as exc:
                print_error(str(exc))
                logger.error("Failed to reopen '%s': %s", source, exc)
                return
            try:
                result = remove_watermark_images(
                    rdoc,
                    chosen_sigs,
                    out_path,
                    progress=lambda c, t: _print_progress("Cleaning pages", c, t),
                    protection=protection,
                )
            except Exception as exc:  # noqa: BLE001 - clean message, log details
                print_error(f"Failed to remove the watermark: {exc}")
                logger.exception("Watermark removal failed for output '%s'", out_path)
                return
            finally:
                rdoc.close()
            # Report the path that was actually written: promotion may have
            # allocated a sibling name if the requested one appeared meanwhile.
            print_success(
                f"Done. Removed watermark from {result.count} page(s):"
                f"\n  {result.path}"
            )
            logger.info(
                "Watermark removal complete: pages=%d output='%s'",
                result.count, result.path,
            )

        queue_task(
            f"Remove {len(chosen)} watermark(s) from {source.name} "
            f"-> {out_path.name}",
            _run,
        )
    finally:
        # Close the configure-time doc, remove the preview folder, and drop the
        # temp parent too if it is now empty.
        doc.close()
        shutil.rmtree(preview_dir, ignore_errors=True)
        try:
            temp = _temp_dir()
            if temp.exists() and not any(temp.iterdir()):
                temp.rmdir()
        except OSError:
            pass
