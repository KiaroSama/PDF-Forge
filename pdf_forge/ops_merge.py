from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403

__all__ = ['_show_merge_source_menu', 'prompt_merge_source_files', 'prompt_merge_source_folder', '_choose_output_file_for_merge', '_describe_merge_sort_mode', '_print_merge_summary', '_default_merge_output', 'operation_merge_pdfs', '_run_merge_with_sources']

def _show_merge_source_menu() -> None:
    """Render the merge submenu in the same style as the Page tools submenu."""
    print()
    print(colorize(f"{APP_NAME} Merge:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} Add PDF files one by one "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Use all PDFs from a folder")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Back")
    print()


def prompt_merge_source_files() -> Optional[List[Path]]:
    """Collect PDF paths one at a time for a merge. Returns None to go back.

    Requires at least 2 distinct PDF files. Type ``done`` (or press Enter) to
    finish once enough files are gathered, ``b`` to drop the file added last and
    enter it again, ``0`` to cancel (Back). The merge order matches the order
    entered. Duplicate files are rejected.
    """
    print_note(
        "Enter PDF paths one at a time. Add at least 2 files, then type 'done' "
        "to finish. Type 'b' to re-enter the previous file."
    )
    selected: List[Path] = []
    while True:
        prompt = question_prompt(
            f"PDF file #{len(selected) + 1}",
            details=guidance_text(
                drag_drop_guidance(repeated=True), GUIDANCE_KEYWORDS
            ),
        )
        raw = _input(prompt)
        cleaned = strip_surrounding_quotes(raw)

        # 'done' (or a blank Enter) finishes once enough files exist.
        if cleaned == "" or cleaned.lower() == "done":
            if len(selected) >= 2:
                return selected
            print_error("Add at least 2 PDF files before finishing.")
            continue
        if cleaned.lower() == "b":
            if not selected:
                print_error("There is no previous file to re-enter yet.")
                continue
            removed = selected.pop()
            print_warning(f"Removed: {removed.name}. Enter file #{len(selected) + 1} again.")
            continue
        if cleaned == "0":
            return None
        if cleaned.lower() in ("exit", "quit"):
            raise _ExitRequested()

        path = Path(cleaned)
        if not path.exists():
            print_error(f"Path does not exist: {cleaned}")
            continue
        if not path.is_file():
            print_error("The path is not a file.")
            continue
        if path.suffix.lower() != ".pdf":
            print_error("The file is not a .pdf file.")
            continue

        # Reject duplicates so the same PDF is never merged twice by accident.
        if any(resolves_to_same_file(path, existing) for existing in selected):
            print_warning("That PDF is already in the list; duplicates are not allowed.")
            continue

        selected.append(path)
        print_success(f"Added: {path.name}  (total: {len(selected)})")


def prompt_merge_source_folder() -> Optional[List[Path]]:
    """Collect all PDFs directly inside a folder (non-recursive).

    Returns the discovered, A-Z sorted list, or None to go back. When fewer
    than 2 PDFs are found, a clear error is shown and None is returned.
    """
    prompt = question_prompt(
        "Folder containing PDFs",
        details=guidance_text(drag_drop_guidance(kind="folder"), GUIDANCE_KEYWORDS),
    )
    while True:
        raw = _input(prompt)
        cleaned = strip_surrounding_quotes(raw)
        if cleaned == "0":
            return None
        if cleaned == "":
            print_error("No folder entered. Please try again.")
            continue
        if cleaned.lower() in ("exit", "quit"):
            raise _ExitRequested()

        folder = Path(cleaned)
        if not folder.exists():
            print_error(f"Path does not exist: {cleaned}")
            continue
        if not folder.is_dir():
            print_error("The path is not a folder.")
            continue

        pdfs = discover_pdfs_in_folder(folder)
        if len(pdfs) < 2:
            print_error(
                f"Found {len(pdfs)} PDF file(s) in that folder; at least 2 are "
                "required to merge."
            )
            return None
        return pdfs


def _choose_output_file_for_merge(default_path: Path,
                                  sources: Sequence[Path]) -> Optional[Path]:
    """Choose the merged output path (Enter = default beside the source).

    Guarantees the result never resolves to any source PDF and never overwrites
    an existing file.
    """
    prompt = question_prompt("Output Path", default=f"{default_path.name}")
    while True:
        raw = _input(prompt)
        cleaned = strip_surrounding_quotes(raw)
        if cleaned == "":
            chosen = default_path
        elif cleaned == "0":
            return None
        elif cleaned.lower() in ("exit", "quit"):
            raise _ExitRequested()
        else:
            candidate = Path(cleaned)
            if candidate.suffix.lower() == ".pdf":
                chosen = candidate
            else:
                # Treat as a directory; keep the default filename.
                chosen = candidate / default_path.name

        # Reject any path that resolves to one of the source PDFs.
        if any(resolves_to_same_file(chosen, src) for src in sources):
            print_error("The output cannot be the same file as any source PDF.")
            continue

        # Confirm intent for a not-yet-existing directory, but defer creation to
        # the task runner (no empty folder left if the task is discarded).
        if not chosen.parent.exists():
            if not ask_yes_no(
                f"Directory does not exist (it will be created when the task "
                f"runs):\n  {chosen.parent}\nUse it?",
                default_yes=True,
            ):
                continue

        # Never overwrite / collide with another queued output: reserve a name.
        final = reserve_unique_file(chosen)
        if final != chosen:
            print_warning(
                f"Output exists or is already queued; using a unique name: "
                f"{final.name}"
            )
        return final


def _describe_merge_sort_mode(mode: str) -> str:
    """Return a human-readable description of the merge ordering for ``mode``."""
    if mode == "folder":
        return "natural, case-insensitive, stable (1, 2, 10)"
    return "manual (exact order entered)"


def _print_merge_summary(
    mode: str,
    sources: Sequence[Path],
    total_pages: int,
    out_path: Path,
) -> None:
    """Print the final merge summary shown right before confirmation.

    Includes the total PDF count, total page count, the resolved output path,
    the sorting mode, and the final merge order. The full order is shown for
    small lists; long lists show the first items and the last few with a gap
    indicator (see :func:`_print_merge_order`).
    """
    print_heading("\nMerge summary:")
    print_kv("Total PDFs", len(sources), Color.MAGENTA)
    print_kv("Total pages", total_pages, Color.GOLD)
    print_kv("Sorting mode", _describe_merge_sort_mode(mode), Color.LIME)
    print_kv("Output Path", out_path, Color.AQUA)
    print(colorize("\n  Final merge order:", Color.GRAY))
    _print_merge_order(sources)


def _default_merge_output(mode: str, sources: Sequence[Path]) -> Path:
    """Compute the default (pre-uniqueness) output path for a merge."""
    if mode == "folder":
        folder = sources[0].parent
        return folder / f"{folder.name}_merged.pdf"
    # File-by-file mode: place beside the first source.
    first = sources[0]
    name = f"{first.stem}_merged.pdf" if first.stem else "PDF_Forge_merged.pdf"
    return first.parent / name


def operation_merge_pdfs() -> None:
    """Interactive flow for merging multiple PDFs into a single new PDF."""
    reset_questions()
    logger.info("Operation started: Merge multiple PDFs.")

    # The merge source menu is the hub for this operation. Every step below it
    # (source picker, output, confirmation, and completion) returns here, so
    # pressing 0 always goes back exactly one level. Only 0 at this menu returns
    # to the main menu.
    while True:
        _show_merge_source_menu()
        choice = _input(
            colorize("Select an option ", Color.BOLD)
            + colorize("[1]", Color.GREEN)
            + " "
            + back_text("back=0, quit=exit")
            + colorize(": ", Color.WHITE)
        ).strip().lower()
        if choice == "":
            choice = "1"
        if choice == "0":
            return  # Back to the main menu.
        if choice in ("exit", "quit"):
            raise _ExitRequested()
        # The chosen merge submenu item is the hierarchical numbering prefix:
        # "Add files one by one" -> 1-1, 1-2, ...; "folder" -> 2-1, ...
        set_operation_prompt(choice)
        if choice == "1":
            mode = "files"
            sources = prompt_merge_source_files()
        elif choice == "2":
            mode = "folder"
            sources = prompt_merge_source_folder()
        else:
            print_error("Invalid option. Please choose 1, 2, or 0.")
            continue

        if not sources:
            # Back (0) from the source picker: re-show this submenu.
            logger.info("Merge source picker cancelled; re-showing the merge menu.")
            continue

        # Run a single merge. It returns here (to the merge menu) whether it
        # completed, was cancelled with 0, or failed to open a source.
        _run_merge_with_sources(mode, sources)


def _run_merge_with_sources(mode: str, sources: List[Path]) -> None:
    """Open, preview, confirm, and write a single merge for the given sources.

    Any cancellation (0 at output or a 'no' confirmation) or failure returns
    normally, so the caller's merge menu is shown again (one level back).
    """
    logger.info("Merge source selected: mode=%s files=%d", mode, len(sources))

    # Open every source up front to validate it, count its pages, and capture
    # the password that opens it - then close it again. Nothing but immutable
    # paths/passwords is carried into the queue, so a discarded task leaks no
    # file handles (A5) and the runner never re-prompts for a password (A13).
    readers = []
    passwords: List[str] = []
    policies = []
    total_pages = 0
    current = sources[0]
    try:
        for current in sources:
            reader = open_source_pdf(current, password_prompt=prompt_password)
            readers.append(reader)
            passwords.append(source_password(reader))
            policies.append(detect_protection(reader))
            total_pages += reader.page_count
    except (PdfOpenError, RuntimeError) as exc:
        print_error(f"Cannot merge: failed to open '{current.name}': {exc}")
        logger.error("Merge aborted; failed to open '%s': %s", current, exc)
        return
    finally:
        # Close every handle opened above, including on a mid-way failure.
        for reader in readers:
            close_doc(reader)
        readers = []

    logger.info(
        "All %d merge source(s) opened successfully; total pages=%d (sort=%s).",
        len(sources), total_pages, _describe_merge_sort_mode(mode),
    )

    # A merge never invents a protection policy: if any source is protected the
    # user makes an explicit choice (documented default: unprotected output).
    merge_protection = resolve_merge_protection(policies)
    if merge_protection is None:
        print_warning("Cancelled. Returning to the merge menu.")
        return

    # Choose the output path (Enter accepts a safe default beside the source).
    default_path = unique_file_path(_default_merge_output(mode, sources))
    out_path = _choose_output_file_for_merge(default_path, sources)
    if out_path is None:
        print_warning("Returning to the merge menu.")
        logger.info("Merge cancelled at output selection.")
        return

    # Show the full merge summary, then confirm.
    _print_merge_summary(mode, sources, total_pages, out_path)
    logger.info(
        "Merge summary: pdfs=%d pages=%d sort=%s output='%s'",
        len(sources), total_pages, mode, out_path,
    )

    def _run():
        logger.info("Merge start: sources=%d output='%s'", len(sources), out_path)
        docs = []
        try:
            # Reopen each source silently with its captured password.
            for src, src_pw in zip(sources, passwords):
                docs.append(open_source_pdf(src, password=src_pw))
            written = write_merged_pdfs_to_pdf(
                docs,
                out_path,
                progress=lambda c, t: _print_progress("Merging pages", c, t),
                protection=merge_protection,
            )
        except Exception as exc:  # noqa: BLE001 - present a clean message, log details
            print_error(f"Failed to create the merged PDF: {exc}")
            logger.exception("Merge failed for output '%s'", out_path)
            return
        finally:
            for doc in docs:
                close_doc(doc)
        print_success(
            f"Done. Merged {len(sources)} file(s), {written} page(s) into:\n  {out_path}"
        )
        logger.info("Merge complete: output='%s' pages=%d", out_path, written)

    queue_task(
        f"Merge {len(sources)} PDF(s) -> {out_path.name}",
        _run,
    )
