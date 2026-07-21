from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403
from .batch_protection import _batch_protection_preflight

__all__ = ['operation_extract_pages', '_extract_single_file', '_extract_multiple_files', 'operation_split_chunks', '_report_created', '_prompt_delete_selection', 'operation_delete_pages_single', 'operation_delete_pages_batch']

def operation_extract_pages() -> None:
    """Interactive flow for extracting pages.

    A plain selection produces one combined PDF. When the expression contains
    '|' separators, each group becomes its own separate output PDF.
    """
    reset_questions()
    print_heading("\nExtract selected pages")
    logger.info("Operation started: Extract selected pages.")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    try:
        reader = open_source_pdf(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open source PDF '%s': %s", source, exc)
        return

    total_pages = reader.page_count
    protection = detect_protection(reader)
    # Carry immutable state into the queue, never the open document (PF-004).
    ref = capture_source(reader, source)
    close_doc(reader)
    print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
    logger.info("Extract: source='%s' pages=%d", source, total_pages)

    selection_prompt = question_prompt(
        "Pages to extract",
        details="',' = one file, '|' = separate files, e.g. 6-37,39-85 or 6-37|39-85",
    )
    while True:
        expression = _input(selection_prompt).strip()
        if expression == "0":
            return
        if expression.lower() in ("exit", "quit"):
            raise _ExitRequested()
        try:
            groups = parse_multi_file_selection(expression, total_pages)
            break
        except PageSelectionError as exc:
            print_error(f"Invalid selection: {exc}")

    logger.info(
        "Extract selection parsed: expression='%s' groups=%d total_selected=%d",
        expression, len(groups), sum(len(g.pages) for g in groups),
    )

    if len(groups) == 1:
        _extract_single_file(ref, source, total_pages, groups[0], protection)
    else:
        _extract_multiple_files(ref, source, total_pages, groups, protection)


def _extract_single_file(ref, source: Path, total_pages: int, group: "PageGroup",
                         protection=None) -> None:
    """Write one combined output PDF from a single page group."""
    if group.duplicates_removed:
        print_warning("Duplicate pages were removed; first occurrence kept.")

    # Default output path lives next to the source PDF.
    default_name = build_extract_output_name(
        source.stem, group.text, len(group.pages)
    )
    default_path = unique_file_path(source.parent / default_name)

    print_heading("\nSummary")
    print_kv("Source file", source.name, Color.CYAN)
    print_kv("Total source pages", total_pages, Color.GOLD)
    print_kv("Selected pages", summarize_ranges(group.pages), Color.LIME)
    print_kv("Pages to extract", len(group.pages), Color.ORANGE)
    print_kv("Default Output Path", default_path, Color.AQUA)

    protection = resolve_protection(protection, context="extracted PDF")
    if protection is None:
        print_warning("Cancelled. Returning to menu.")
        return

    out_path = _choose_output_file(default_path, source)
    if out_path is None:
        print_warning("Returning to menu.")
        return

    pages_zero_based = [p - 1 for p in group.pages]

    def _run():
        doc = None
        try:
            doc = ref.open()
            result = write_pages_to_pdf(
                doc,
                pages_zero_based,
                out_path,
                progress=lambda c, t: _print_progress("Extracting", c, t),
                protection=protection,
            )
            # No-clobber promotion may have chosen a suffixed name; everything
            # downstream must name the file that was actually written (C-01).
            written_path, written = result.path, result.count
        except Exception as exc:  # noqa: BLE001 - present a clean message, log details
            print_error(f"Failed to create the output PDF: {exc}")
            logger.exception("Extraction failed for output '%s'", out_path)
            return
        finally:
            close_doc(doc)
        print_success(f"Done. Wrote {written} page(s) to:\n  {written_path}")
        logger.info("Extract complete: output='%s' pages=%d", written_path, written)

    queue_task(
        f"Extract pages {group.text} from {source.name} -> {out_path.name}",
        _run,
        # Identity of every source this task was configured against;
        # the queue re-verifies it just before running (C-06).
        sources=[ref],
    )


def _extract_multiple_files(ref, source: Path, total_pages: int,
                            groups: "List[PageGroup]", protection=None) -> None:
    """Write one separate output PDF per page group (split by '|')."""
    if any(g.duplicates_removed for g in groups):
        print_warning("Duplicate pages were removed in one or more groups; order kept.")

    print_heading("\nSummary")
    print_kv("Source file", source.name, Color.CYAN)
    print_kv("Total source pages", total_pages, Color.GOLD)
    print_kv("Separate files", len(groups), Color.MAGENTA)
    file_colors = (Color.SKY, Color.VIOLET, Color.TEAL, Color.CORAL, Color.PINK)
    for index, group in enumerate(groups, start=1):
        print(
            colorize(f"    File {index}: ", Color.GREEN + Color.BOLD)
            + colorize(summarize_ranges(group.pages), file_colors[(index - 1) % len(file_colors)])
            + colorize(f"  ({len(group.pages)} page(s))", Color.GRAY)
        )

    protection = resolve_protection(protection, context="extracted PDFs")
    if protection is None:
        print_warning("Cancelled. Returning to menu.")
        return

    out_dir = _choose_output_dir_for_files(source.parent)
    if out_dir is None:
        print_warning("Returning to menu.")
        return

    def _run():
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print_error(f"Could not create output directory: {exc}")
            logger.error("Failed to create output dir '%s': %s", out_dir, exc)
            return

        try:
            doc = ref.open()
        except (PdfOpenError, RuntimeError) as exc:
            print_error(str(exc))
            logger.error("Extract (multi) could not reopen '%s': %s", source, exc)
            return
        try:
            _write_extract_groups(doc, groups, out_dir, source, protection)
        finally:
            close_doc(doc)

    def _write_extract_groups(doc, groups, out_dir, source, protection):
        created_files: List[Path] = []
        total_written = 0
        for index, group in enumerate(groups, start=1):
            name = build_extract_output_name(source.stem, group.text, len(group.pages))
            out_path = unique_file_path(out_dir / name)
            # Never let an output collide with the source PDF.
            if resolves_to_same_file(out_path, source):
                out_path = unique_file_path(out_dir / f"{source.stem}_extract_{index}.pdf")
            pages_zero_based = [p - 1 for p in group.pages]
            _print_progress("Writing files", index, len(groups))
            try:
                result = write_pages_to_pdf(doc, pages_zero_based, out_path,
                                            protection=protection)
                written_path, written = result.path, result.count
            except Exception as exc:  # noqa: BLE001
                sys.stdout.write("\n")
                print_error(f"Failed while writing '{out_path.name}': {exc}")
                logger.exception("Extract (multi) write failed: '%s'", out_path)
                print_warning(
                    f"{len(created_files)} file(s) were completed before the failure."
                )
                _report_created(created_files, total_written, out_dir)
                return
            created_files.append(written_path)
            total_written += written

        print_success(
            f"Done. Created {len(created_files)} file(s), {total_written} page(s) total."
        )
        print_success(f"Output directory:\n  {out_dir}")
        logger.info(
            "Extract (multi) complete: files=%d pages=%d dir='%s'",
            len(created_files), total_written, out_dir,
        )

    queue_task(
        f"Extract {len(groups)} file(s) from {source.name} -> {out_dir.name}",
        _run,
        # Identity of every source this task was configured against;
        # the queue re-verifies it just before running (C-06).
        sources=[ref],
    )


def operation_split_chunks() -> None:
    """Interactive flow for splitting a PDF into fixed-size page chunks."""
    reset_questions()
    print_heading("\nSplit PDF into fixed-size chunks")
    logger.info("Operation started: Split PDF into fixed-size chunks.")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    try:
        reader = open_source_pdf(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open source PDF '%s': %s", source, exc)
        return

    total_pages = reader.page_count
    protection = detect_protection(reader)
    # Immutable state only: the queue must not hold an open document (PF-004).
    ref = capture_source(reader, source)
    close_doc(reader)
    print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
    logger.info("Split: source='%s' pages=%d", source, total_pages)

    chunk_prompt = question_prompt("Pages per file")
    while True:
        raw = _input(chunk_prompt).strip()
        if raw == "0":
            return
        if raw.lower() in ("exit", "quit"):
            raise _ExitRequested()
        try:
            chunk_size = parse_chunk_size(raw)
            break
        except ChunkSizeError as exc:
            print_error(f"Invalid value: {exc}")

    # Optional start/end page range. Empty input keeps the document's natural
    # boundaries (start = 1, end = total_pages).
    start_prompt = question_prompt("Start page", default="1")
    while True:
        raw_start = _input(start_prompt).strip()
        if raw_start == "0":
            return
        if raw_start.lower() in ("exit", "quit"):
            raise _ExitRequested()
        try:
            first_page = parse_page_number(raw_start, 1, total_pages, "start page")
            break
        except ChunkSizeError as exc:
            print_error(f"Invalid value: {exc}")

    end_prompt = question_prompt("End page", default=str(total_pages))
    while True:
        raw_end = _input(end_prompt).strip()
        if raw_end == "0":
            return
        if raw_end.lower() in ("exit", "quit"):
            raise _ExitRequested()
        try:
            last_page = parse_page_number(raw_end, total_pages, total_pages, "end page")
        except ChunkSizeError as exc:
            print_error(f"Invalid value: {exc}")
            continue
        if first_page > last_page:
            print_error(
                f"The start page ({first_page}) must not be greater than the "
                f"end page ({last_page})."
            )
            continue
        break

    covered_pages = last_page - first_page + 1
    using_subrange = (first_page != 1) or (last_page != total_pages)

    if chunk_size >= covered_pages:
        print_warning(
            f"The chunk size ({chunk_size}) is >= the selected span "
            f"({covered_pages} page(s)). Only one output PDF will be created."
        )
        if not ask_yes_no("Continue?", default_yes=True):
            print_warning("Cancelled. Returning to menu.")
            return

    chunks = compute_chunks(total_pages, chunk_size, first_page, last_page)
    pad = pad_width_for(total_pages)
    logger.info(
        "Split parameters: chunk_size=%d range=%d-%d covered=%d chunks=%d subrange=%s",
        chunk_size, first_page, last_page, covered_pages, len(chunks), using_subrange,
    )

    # Default output folder next to the source PDF; prefer a unique folder.
    # Include the page span in the folder name when a sub-range is used.
    if using_subrange:
        folder_name = (
            f"{source.stem}_split_{chunk_size}_pages_{first_page}-{last_page}"
        )
    else:
        folder_name = f"{source.stem}_split_{chunk_size}_pages"
    default_folder = unique_dir_path(source.parent / folder_name)

    print_heading("\nPreview")
    print_kv("Source file", source.name, Color.CYAN)
    print_kv("Total source pages", total_pages, Color.GOLD)
    print_kv(
        "Page range",
        f"{first_page} - {last_page} ({covered_pages} page(s))",
        Color.LIME,
    )
    print_kv("Pages per file", chunk_size, Color.ORANGE)
    print_kv("Output PDFs", len(chunks), Color.MAGENTA)
    preview_count = min(len(chunks), 10)
    # Alternate two accent colors for the range list to add visual variety.
    range_colors = (Color.SKY, Color.VIOLET)
    for idx, (start, end) in enumerate(chunks[:preview_count]):
        print(
            colorize("    - pages ", Color.DIM)
            + colorize(f"{start}-{end}", range_colors[idx % 2])
        )
    if len(chunks) > preview_count:
        print(colorize(f"    ... (+{len(chunks) - preview_count} more)", Color.DIM))
    print_kv("Output directory", default_folder, Color.AQUA)

    protection = resolve_protection(protection, context="split PDFs")
    if protection is None:
        print_warning("Cancelled. Returning to menu.")
        return

    out_dir = _choose_output_dir(default_folder)
    if out_dir is None:
        print_warning("Returning to menu.")
        return

    def _run():
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print_error(f"Could not create output directory: {exc}")
            logger.error("Failed to create output dir '%s': %s", out_dir, exc)
            return

        try:
            doc = ref.open()
        except (PdfOpenError, RuntimeError) as exc:
            print_error(str(exc))
            logger.error("Split could not reopen '%s': %s", source, exc)
            return
        try:
            _write_chunks(doc)
        finally:
            close_doc(doc)

    def _write_chunks(doc):
        created_files: List[Path] = []
        total_written = 0
        for index, (start, end) in enumerate(chunks, start=1):
            name = build_chunk_output_name(source.stem, start, end, pad)
            # Guarantee uniqueness even if a stray file exists in a reused folder.
            out_path = unique_file_path(out_dir / name)
            pages_zero_based = list(range(start - 1, end))
            _print_progress("Writing chunks", index, len(chunks))
            try:
                # Write from the document this runner reopened; the
                # configure-time reader was closed before queueing (PF-004).
                result = write_pages_to_pdf(doc, pages_zero_based, out_path,
                                            protection=protection)
                written_path, written = result.path, result.count
            except Exception as exc:  # noqa: BLE001
                sys.stdout.write("\n")
                print_error(f"Failed while writing '{out_path.name}': {exc}")
                logger.exception("Chunk write failed: '%s'", out_path)
                # Partial failure: keep already completed valid files, stop here.
                print_warning(
                    f"{len(created_files)} file(s) were completed before the failure."
                )
                _report_created(created_files, total_written, out_dir)
                return
            created_files.append(written_path)
            total_written += written

        print_success(
            f"Done. Created {len(created_files)} file(s), {total_written} page(s) total."
        )
        print_success(f"Output directory:\n  {out_dir}")
        logger.info(
            "Split complete: files=%d pages=%d dir='%s'",
            len(created_files), total_written, out_dir,
        )

    queue_task(
        f"Split {source.name} into {len(chunks)} file(s) of {chunk_size} "
        f"-> {out_dir.name}",
        _run,
        # Identity of every source this task was configured against;
        # the queue re-verifies it just before running (C-06).
        sources=[ref],
    )


def _report_created(files: Sequence[Path], pages: int, out_dir: Path) -> None:
    """Report which files were completed after a partial failure."""
    if files:
        print_success(f"Completed {len(files)} file(s), {pages} page(s):")
        for f in files[:10]:
            print(f"    - {f.name}")
        if len(files) > 10:
            print(f"    ... (+{len(files) - 10} more)")
        print_success(f"Output directory:\n  {out_dir}")


def _prompt_delete_selection(max_page: Optional[int] = None) -> Optional[List[int]]:
    """Prompt for the pages to delete. Returns the parsed list or None (Back).

    ``max_page`` bounds accepted page numbers: the document length in single-file
    mode, or ``None`` in batch mode (a hard sanity ceiling then applies so a
    pathological range cannot exhaust memory).
    """
    prompt = question_prompt(
        "Pages to delete",
        details="e.g. 5 or 10-20 or 10-20,25,30-50",
    )
    while True:
        raw = _input(prompt).strip()
        if raw == "0":
            return None
        if raw.lower() in ("exit", "quit"):
            raise _ExitRequested()
        try:
            return parse_delete_pages(raw, max_page=max_page)
        except PageSelectionError as exc:
            print_error(f"Invalid selection: {exc}")


def operation_delete_pages_single() -> None:
    """Delete selected pages from a single PDF into a new file."""
    reset_questions()
    print_heading("\nDelete pages: single file")
    logger.info("Operation started: Delete pages (single file).")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    try:
        reader = open_source_pdf(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s': %s", source, exc)
        return

    total_pages = reader.page_count
    protection = detect_protection(reader)
    ref = capture_source(reader, source)
    close_doc(reader)
    print_success(f"Loaded '{source.name}' - {total_pages} page(s).")

    # Ask for the pages, rejecting any that do not exist in this document.
    while True:
        requested = _prompt_delete_selection(max_page=total_pages)
        if requested is None:
            return
        present, missing, kept = compute_deletion(total_pages, requested)
        if missing:
            print_error(
                f"These pages do not exist (document has {total_pages}): "
                f"{summarize_ranges(missing)}. Please re-enter."
            )
            continue
        if not present:
            print_error("No pages selected.")
            continue
        if not kept:
            print_error(
                "That would delete every page. A PDF must keep at least one page."
            )
            continue
        break

    selection_text = summarize_ranges(present)
    default_name = build_delete_output_name(source.stem, selection_text)
    default_path = unique_file_path(source.parent / default_name)

    print_heading("\nSummary")
    print_kv("Source file", source.name, Color.CYAN)
    print_kv("Total source pages", total_pages, Color.GOLD)
    print_kv("Pages to delete", f"{selection_text}  ({len(present)} page(s))", Color.RED)
    print_kv("Pages remaining", len(kept), Color.LIME)
    print_kv("Default Output Path", default_path, Color.AQUA)

    protection = resolve_protection(protection, context="output PDF")
    if protection is None:
        print_warning("Cancelled. Returning to menu.")
        return

    out_path = _choose_output_file(default_path, source)
    if out_path is None:
        print_warning("Returning to menu.")
        return

    def _run():
        logger.info(
            "Delete-pages start: source='%s' delete=%s keep=%d output='%s'",
            source, selection_text, len(kept), out_path,
        )
        doc = None
        try:
            doc = ref.open()
            result = write_pages_to_pdf(
                doc, kept, out_path,
                progress=lambda c, t: _print_progress("Writing pages", c, t),
                protection=protection,
            )
            written_path, written = result.path, result.count
        except Exception as exc:  # noqa: BLE001 - clean message, log details
            print_error(f"Failed to create the output PDF: {exc}")
            logger.exception("Delete-pages failed for output '%s'", out_path)
            return
        finally:
            close_doc(doc)
        print_success(
            f"Done. Deleted {len(present)} page(s); kept {written}:\n  {written_path}"
        )
        logger.info("Delete-pages complete: output='%s' kept=%d", written_path, written)

    queue_task(
        f"Delete pages {selection_text} from {source.name} -> {out_path.name}",
        _run,
        # Identity of every source this task was configured against;
        # the queue re-verifies it just before running (C-06).
        sources=[ref],
    )


def operation_delete_pages_batch() -> None:
    """Delete selected pages from every PDF in a folder (one new file per PDF).

    Pages are matched per file: a file is only touched for the pages it actually
    has. Pages that do not exist in a given file are skipped for that file and
    reported in a note. Files with none of the requested pages, or where the
    request would remove every page, are skipped with a note.
    """
    reset_questions()
    print_heading("\nDelete pages: batch folder")
    logger.info("Operation started: Delete pages (batch folder).")

    pdfs = prompt_source_folder_pdfs()
    if pdfs is None:
        return

    requested = _prompt_delete_selection()
    if requested is None:
        return

    folder = pdfs[0].parent
    selection_text = summarize_ranges(requested)

    # Decide protection ONCE, before anything is queued or written (PF-008).
    decisions, cancelled = _batch_protection_preflight(pdfs)
    if cancelled:
        print_warning("Cancelled. Returning to menu.")
        return
    pdfs = [p for p in pdfs if decisions.get(normalized_path_key(p)) is not None]
    if not pdfs:
        print_warning("No files remain after the protection decision.")
        return

    print_heading("\nSummary")
    print_kv("Folder", folder, Color.CYAN)
    print_kv("PDF files", len(pdfs), Color.MAGENTA)
    print_kv("Pages to delete", selection_text, Color.RED)
    print_note(
        "For each file, only the requested pages that exist are deleted. Pages "
        "beyond a file's length are skipped for that file (a note is shown)."
    )
    print_kv("Per-file output", "<name>_deleted_... .pdf beside each PDF", Color.AQUA)
    print_note(BATCH_PASSWORD_NOTICE)
    print(colorize("\n  Files:", Color.GRAY))
    _print_merge_order(pdfs)

    def _run():
        logger.info(
            "Delete-pages batch start: folder='%s' files=%d delete=%s",
            folder, len(pdfs), selection_text,
        )
        print()
        processed = skipped = failed = total_deleted = 0
        unprotected_notes: List[str] = []
        for index, src in enumerate(pdfs, start=1):
            print_info(f"[{index}/{len(pdfs)}] {src.name}")
            try:
                reader = open_source_pdf(src, password_prompt=prompt_password)
            except (PdfOpenError, RuntimeError) as exc:
                print_error(f"  Skipped (could not open): {exc}")
                logger.error("Delete-pages batch: failed to open '%s': %s", src, exc)
                failed += 1
                continue

            # Every path below - skip, error, write failure, success, or a
            # failure while reporting - must release this handle exactly once,
            # so the source stays renameable/deletable straight afterwards.
            try:
                outcome = _delete_one(src, reader)
            finally:
                close_doc(reader)
            processed += outcome["processed"]
            skipped += outcome["skipped"]
            failed += outcome["failed"]
            total_deleted += outcome["deleted"]
            if outcome["unprotected"]:
                unprotected_notes.append(src.name)
            continue

        _report_batch(processed, skipped, failed, total_deleted, unprotected_notes)

    def _delete_one(src, reader) -> dict:
        """Handle one already-opened batch file. Never closes the document."""
        result = {"processed": 0, "skipped": 0, "failed": 0, "deleted": 0,
                  "unprotected": False}
        total = reader.page_count
        present, missing, kept = compute_deletion(total, requested)

        if not present:
            print_note(
                f"  Note: none of the requested pages exist here "
                f"(has {total} page(s)); skipped."
            )
            logger.info(
                "Delete-pages batch: '%s' has no requested pages; skipped.", src
            )
            result["skipped"] = 1
            return result
        if not kept:
            print_note(
                f"  Note: the request covers all {total} page(s); skipped to keep "
                "a valid PDF."
            )
            logger.info("Delete-pages batch: '%s' would be emptied; skipped.", src)
            result["skipped"] = 1
            return result

        out_name = build_delete_output_name(src.stem, summarize_ranges(present))
        out_path = unique_file_path(src.parent / out_name)
        if resolves_to_same_file(out_path, src):
            out_path = unique_file_path(src.parent / f"{src.stem}_pages_deleted.pdf")
        # Per-file policy decided by the batch preflight, never prompted here.
        # A file the preflight could not read (needs an open password) has no
        # stored policy; the runner authenticated it just now, so preserve its
        # own policy faithfully instead of dropping it.
        decided = decisions.get(normalized_path_key(src))
        detected = detect_protection(reader)
        file_policy = decided if isinstance(decided, ProtectionPolicy) else detected
        was_restricted = detected.kind == "restricted"
        try:
            written_result = write_pages_to_pdf(reader, kept, out_path,
                                                protection=file_policy)
            written_path, written = written_result.path, written_result.count
        except Exception as exc:  # noqa: BLE001 - keep the batch going
            print_error(f"  Failed: {exc}")
            logger.exception("Delete-pages batch write failed for '%s'", src)
            result["failed"] = 1
            return result

        result["deleted"] = len(present)
        result["processed"] = 1
        # Only a source that WAS restricted and whose output was actually
        # written (the consented "write unprotected" choice) is reported as
        # unprotected - after the write, so a failed write is never named (PF-031).
        if was_restricted:
            result["unprotected"] = True
        print_success(
            f"  -> deleted {len(present)} page(s) [{summarize_ranges(present)}]; "
            f"kept {written} -> {written_path.name}"
        )
        if missing:
            print_note(
                f"  Note: pages not in this file were skipped: "
                f"{summarize_ranges(missing)} (has {total} page(s))."
            )
        return result

    def _report_batch(processed, skipped, failed, total_deleted, unprotected_notes):
        print_success(
            f"Done. Processed {processed} file(s), skipped {skipped}, failed {failed}; "
            f"{total_deleted} page(s) deleted in total."
        )
        if unprotected_notes:
            print_warning(
                f"{len(unprotected_notes)} file(s) had owner restrictions that "
                "cannot be reproduced (the owner password is not recoverable); "
                "their outputs are unprotected: "
                + ", ".join(unprotected_notes[:5])
                + (" ..." if len(unprotected_notes) > 5 else "")
            )
        logger.info(
            "Delete-pages batch complete: processed=%d skipped=%d failed=%d deleted=%d",
            processed, skipped, failed, total_deleted,
        )

    queue_task(
        f"Delete pages {selection_text} in {len(pdfs)} file(s) of {folder.name}",
        _run,
        # Identity of every source this task was configured against;
        # the queue re-verifies it just before running (C-06).
        sources=[capture_file_source(p) for p in pdfs],
    )
