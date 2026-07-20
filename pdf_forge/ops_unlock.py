from __future__ import annotations

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .unlock import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403

__all__ = ['operation_unlock_pdf']


def operation_unlock_pdf() -> None:
    """Remove a PDF's open password and permission restrictions (unlock).

    Requires legitimate access: if the file needs a password to open, you must
    provide it. Owner-only restrictions (the file opens freely but forbids
    printing/copying/editing) are removed without any password. The original
    PDF is never modified.
    """
    reset_questions()
    print_heading("\nUnlock PDF (remove password & restrictions)")
    logger.info("Operation started: Unlock PDF.")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    pymupdf = _import_pymupdf()
    try:
        doc = pymupdf.open(str(source))
    except Exception as exc:  # noqa: BLE001 - any open failure is a clean message
        print_error(f"Could not open the PDF: {exc}")
        logger.error("Unlock: failed to open '%s': %s", source, exc)
        return

    # If an open password is required, the user must supply it. Authentication
    # has no attempt limit: _authenticate_doc re-prompts until the password is
    # accepted or the user types 0/back/skip (exit/quit propagates).
    had_open_password = bool(doc.needs_pass)
    source_pw = ""
    if had_open_password:
        try:
            source_pw = _authenticate_doc(doc, prompt_password, None)
        except BaseException:
            close_doc(doc)
            raise
        if source_pw is None:
            print_error(
                "The PDF is password-protected and was not unlocked (cancelled)."
            )
            logger.info("Unlock cancelled: no valid password for '%s'.", source)
            close_doc(doc)
            return

    restricted = denied_permissions(doc)
    total_pages = doc.page_count
    print_success(f"Loaded '{source.name}' - {total_pages} page(s).")

    if not had_open_password and not restricted:
        print_warning(
            "This PDF is not locked: no open password and no permission "
            "restrictions. Nothing to unlock."
        )
        logger.info("Unlock: '%s' is already unlocked.", source)
        close_doc(doc)
        return

    # Nothing but the path and its password crosses the queue boundary (A5).
    close_doc(doc)

    default_path = unique_file_path(source.parent / f"{source.stem}_unlocked.pdf")

    print_heading("\nSummary")
    print_kv("Source file", source.name, Color.CYAN)
    print_kv("Total source pages", total_pages, Color.GOLD)
    if had_open_password:
        print_kv("Open password", "required (entered) - will be removed", Color.RED)
    if restricted:
        print_kv("Restricted actions", ", ".join(restricted), Color.RED)
        print_note("These restrictions will be lifted in the unlocked copy.")
    print_kv("Default Output Path", default_path, Color.AQUA)

    out_path = _choose_output_file(default_path, source)
    if out_path is None:
        print_warning("Returning to menu.")
        return

    def _run():
        rdoc = None
        try:
            # Reopen silently with the captured password (no prompt mid-run).
            rdoc = open_source_pdf(source, password=source_pw)
            written = unlock_pdf_doc(rdoc, out_path)
        except Exception as exc:  # noqa: BLE001 - clean message, log details
            print_error(f"Failed to unlock the PDF: {exc}")
            logger.exception("Unlock failed for output '%s'", out_path)
            return
        finally:
            close_doc(rdoc)
        print_success(f"Done. Unlocked {written} page(s):\n  {out_path}")
        logger.info("Unlock complete: output='%s' pages=%d", out_path, written)

    queue_task(f"Unlock {source.name} -> {out_path.name}", _run,
               sources=[capture_file_source(source)])
