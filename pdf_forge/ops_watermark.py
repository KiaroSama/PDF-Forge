from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

# The project's process-identity primitive: a PID alone is not an owner, so the
# start time is paired with it (see safeio). Imported rather than reimplemented.
from .safeio import _ALIVE_UNKNOWN, _process_start
from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .watermark import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403

__all__ = ['_temp_dir', '_run_dir', 'cleanup_temp_dir',
           'operation_remove_watermark']

def _temp_dir() -> Path:
    """The project-local scratch folder (``PDF Forge/temp``).

    Used for transient files such as watermark preview images. It lives at the
    project root (the parent of the pdf_forge/ package) so it is always in a
    known, writable location regardless of where the source PDF is.
    """
    return Path(__file__).resolve().parent.parent / "temp"


def _run_dir() -> Path:
    """This run's own subfolder of ``temp``, stamped with its owner's identity.

    The temp folder is shared by every instance, but previews are not: a second
    instance starting up must not delete the previews the first instance is
    asking the user to go and look at (C-04). The name carries PID *and* process
    start time, because a recycled PID would otherwise make a dead run look
    alive - the same identity rule ``safeio`` applies to lock owners.
    """
    start = _process_start(os.getpid()) or _ALIVE_UNKNOWN
    # Keep the name filename-safe: _ALIVE_UNKNOWN is '?', illegal on Windows.
    stamp = start if start.isdigit() else "unknown"
    return _temp_dir() / f"run-{os.getpid()}-{stamp}"


def _owner_is_gone(name: str) -> bool:
    """Whether the run directory ``name`` belongs to a process provably gone.

    Fails safe in both directions: an entry that is not one of our run folders
    is treated as stale leftover (that is what startup cleanup has always been
    for), while a live or merely unreadable owner is never reported as gone.
    """
    parts = name.split("-")
    if len(parts) != 3 or parts[0] != "run" or not parts[1].isdigit():
        return True  # legacy or foreign leftover, no owner to respect
    current = _process_start(int(parts[1]))
    if current is None:
        return True  # no such process: provably gone
    if current == _ALIVE_UNKNOWN:
        return False  # alive but opaque - never mistake that for dead
    return current != parts[2]  # different start time: the PID was recycled


def cleanup_temp_dir() -> None:
    """Clear preview folders left by runs that have ended, at startup.

    Only folders whose owning process is provably gone are removed, so a second
    instance launched while the first is sitting at the "open these previews and
    choose" prompt no longer wipes them out from under it (C-04). The temp
    parent itself is dropped once nothing is left in it.
    """
    temp = _temp_dir()
    if not temp.exists():
        return
    for entry in temp.iterdir():
        if not _owner_is_gone(entry.name):
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    try:
        if not any(temp.iterdir()):
            temp.rmdir()
            logger.info("Cleared temp folder at startup: %s", temp)
    except OSError:
        pass


def _prompt_watermark_choice(candidate_count: int) -> Optional[List[int]]:
    """Ask which candidate(s) to remove. ``None`` = skip this file."""
    sel_prompt = question_prompt(
        "Watermark(s) to remove",
        details="e.g. 1 or 1,3 (0 skips this file)",
        default="1",
    )
    while True:
        raw = _input(sel_prompt).strip()
        if raw == "":
            raw = "1"  # Enter selects candidate 1 (the top match).
        if raw == "0":
            return None
        if raw.lower() in ("exit", "quit"):
            raise _ExitRequested()
        try:
            return parse_index_list(raw, candidate_count)
        except ValueError as exc:
            print_error(str(exc))


def _configure_watermark_removal(source: Path, preview_dirs: list) -> Optional[dict]:
    """Scan one PDF and ask which watermark(s) to remove, and where to write.

    Returns the configured job, or ``None`` when this file is skipped: it could
    not be opened, nothing removable was found, the user typed 0 at the
    selection, or the protection question was declined. Skipping one file never
    abandons the others - that is the whole point of accepting a list.

    Any preview folder it creates is appended to ``preview_dirs`` so the caller
    can clear them all once the batch is configured.
    """
    try:
        doc = open_source_pdf(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s': %s", source, exc)
        return None

    try:
        # Immutable identity of the source, captured while the configuration doc is
        # still open. The queue verifies it before the runner starts (C-06) and the
        # runner reopens through it, silently, with the captured password (A13).
        ref = capture_source(doc, source)
        detected_protection = detect_protection(doc)
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
            return None  # nothing to do for this file

        # Export previews to the project-local temp folder (always in a known place).
        # Fall back to the system temp folder if that location is not writable.
        preview_dir = unique_dir_path(_run_dir() / f"{source.stem}_wm_preview")
        try:
            preview_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            preview_dir = Path(tempfile.mkdtemp(prefix="pdfforge_wm_preview_"))
        preview_dirs.append(preview_dir)
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

        while True:
            indices = _prompt_watermark_choice(len(candidates))
            if indices is None:
                print_warning(f"Skipped {source.name}.")
                return None
            chosen = [candidates[i - 1] for i in indices]
            affected_pages = set()
            for c in chosen:
                affected_pages |= c.pages

            # Consent BEFORE any output is configured or written (C-13). The
            # resolved policy is captured in the queued task and handed to the
            # writer, so a run-time re-detection never decides it. Resolved from
            # the ORIGINAL detected policy each time round, so stepping back and
            # forward re-asks cleanly rather than compounding a prior answer.
            protection = resolve_protection(detected_protection,
                                            context="watermark-free PDF")
            if protection is None:
                print_warning(f"Cancelled; skipped {source.name}.")
                return None

            default_path = unique_file_path(
                source.parent / f"{source.stem}_no_watermark.pdf")
            out_path = _choose_output_file(default_path, source)
            if out_path is not None:
                break
            # 0 at the output prompt steps back one prompt, to the selection.

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
            "Watermark removal chosen: source='%s' candidates=%s pages=%d output='%s'",
            source, indices, len(affected_pages), out_path,
        )
        return {
            "source": source,
            "ref": ref,
            "chosen": chosen,
            "signatures": [c.signature for c in chosen],
            "protection": protection,
            "out_path": out_path,
        }
    finally:
        # The configuration document never crosses the queue boundary; the
        # runner reopens each source through its reference (PF-004).
        close_doc(doc)


def _run_watermark_removal(job: dict) -> bool:
    """Execute one configured removal. ``False`` when it failed."""
    # Reopen the source fresh (the configure-time doc is closed after previews,
    # and removal mutates the document in place). Going through the reference
    # re-proves identity and authenticates silently - no prompt during queue
    # execution.
    try:
        rdoc = job["ref"].open()
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to reopen '%s': %s", job["source"], exc)
        return False
    try:
        result = remove_watermark_images(
            rdoc,
            job["signatures"],
            job["out_path"],
            progress=lambda c, t: _print_progress("Cleaning pages", c, t),
            protection=job["protection"],
        )
    except Exception as exc:  # noqa: BLE001 - clean message, log details
        print_error(f"Failed to remove the watermark: {exc}")
        logger.exception("Watermark removal failed for output '%s'", job["out_path"])
        return False
    finally:
        close_doc(rdoc)
    # Report the path that was actually written: promotion may have allocated a
    # sibling name if the requested one appeared meanwhile.
    print_success(
        f"Done. Removed watermark from {result.count} page(s):\n  {result.path}"
    )
    logger.info(
        "Watermark removal complete: pages=%d output='%s'",
        result.count, result.path,
    )
    return True


def operation_remove_watermark() -> None:
    """Detect repeated image watermarks, preview them, and remove the chosen ones.

    Accepts one or more PDFs in a single pass: paths are entered one at a time
    until ``done``, then each file is scanned separately and asked about on its
    own (0 at that point skips just that file). Everything configured is queued
    as one task.

    Only image-based watermarks that repeat across pages can be removed. The
    text layer and all other content are preserved. Preview images are written
    to the project-local ``temp`` folder (``PDF Forge/temp``) so you can confirm
    which image to remove before any change is made; that folder is removed
    automatically when the operation finishes, and any leftovers are cleared on
    the next launch. The original PDFs are never modified.
    """
    reset_questions()
    print_heading("\nRemove image watermark")
    logger.info("Operation started: Remove image watermark.")

    sources = prompt_pdf_paths(
        minimum=1,
        label="Source PDF",
        note=("Add one or more PDFs, one path at a time, then type 'done' (or "
              "press Enter) to finish. Each file is scanned on its own and you "
              "choose its watermark(s) next - 0 there skips just that file."),
    )
    if sources is None:
        return

    preview_dirs = []
    try:
        jobs = []
        for position, source in enumerate(sources, start=1):
            if len(sources) > 1:
                print_heading(f"\nFile {position}/{len(sources)}: {source.name}")
            job = _configure_watermark_removal(source, preview_dirs)
            if job is not None:
                jobs.append(job)

        if not jobs:
            print_warning("No file was configured for removal. Returning to menu.")
            logger.info("Watermark removal: nothing configured from %d file(s).",
                        len(sources))
            return

        def _run():
            failed = 0
            for index, job in enumerate(jobs, start=1):
                if len(jobs) > 1:
                    print_info(f"[{index}/{len(jobs)}] {job['source'].name}")
                if not _run_watermark_removal(job):
                    failed += 1
            if len(jobs) > 1:
                print_success(
                    f"\nDone. Cleaned {len(jobs) - failed} file(s), {failed} failed."
                )
                logger.info("Watermark batch complete: ok=%d failed=%d",
                            len(jobs) - failed, failed)

        summary = (
            f"Remove {len(jobs[0]['chosen'])} watermark(s) from "
            f"{jobs[0]['source'].name} -> {jobs[0]['out_path'].name}"
            if len(jobs) == 1 else
            f"Remove watermarks from {len(jobs)} file(s)"
        )
        queue_task(
            summary,
            _run,
            # Identity of every source this task was configured against;
            # the queue re-verifies them just before running (C-06).
            sources=[job["ref"] for job in jobs],
        )
    finally:
        # Remove every preview folder, then this run's own folder and the temp
        # parent, each only while empty - another instance may be using them.
        for directory in preview_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        for directory in (_run_dir(), _temp_dir()):
            try:
                if directory.exists() and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError:
                pass
