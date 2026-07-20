from __future__ import annotations

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .encrypt import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403

__all__ = ['operation_protect_open_password', 'operation_protect_restrict',
           '_prompt_blocked_actions']


def _open_source_for_protect(source):
    """Open the source (handling its own password) and print a loaded line.

    Returns the open document, or None on failure/cancel.
    """
    try:
        doc = open_source_pdf(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Protect: failed to open '%s': %s", source, exc)
        return None
    print_success(f"Loaded '{source.name}' - {doc.page_count} page(s).")
    return doc


def operation_protect_open_password() -> None:
    """Encrypt a PDF so it needs a password just to open (view)."""
    reset_questions()
    print_heading("\nProtect PDF: password to open")
    logger.info("Operation started: Protect PDF (open password).")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    doc = _open_source_for_protect(source)
    if doc is None:
        return
    # Keep only immutable state in the queue: the source path, its own password
    # (for a silent reopen) and the page count. The handle is closed now, so a
    # discarded task cannot leak it (A5).
    source_pw = source_password(doc)
    page_count = doc.page_count
    close_doc(doc)

    try:
        password = prompt_new_password("to open the file")
    except _ExitRequested:
        raise
    if password is None:
        print_warning("Cancelled. Returning to menu.")
        return

    default_path = unique_file_path(source.parent / f"{source.stem}_protected.pdf")

    print_heading("\nSummary")
    print_kv("Source file", source.name, Color.CYAN)
    print_kv("Total source pages", page_count, Color.GOLD)
    print_kv("Protection", "AES-256; a password is required to open", Color.LIME)
    print_kv("Default Output Path", default_path, Color.AQUA)
    print_note("Keep the password safe - without it the file cannot be opened.")

    out_path = _choose_output_file(default_path, source)
    if out_path is None:
        print_warning("Returning to menu.")
        return

    def _run():
        rdoc = None
        try:
            rdoc = open_source_pdf(source, password=source_pw)
            # The same password opens the file and guards its permissions.
            result = save_encrypted_pdf(
                rdoc, out_path, user_pw=password, owner_pw=password,
                permissions=all_permissions(),
            )
        except Exception as exc:  # noqa: BLE001 - clean message, log details
            print_error(f"Failed to protect the PDF: {exc}")
            logger.exception("Protect (open password) failed for '%s'", out_path)
            return
        finally:
            close_doc(rdoc)
        # The written path, not the configured one: promotion may have had to
        # allocate a suffixed sibling.
        print_success(
            f"Done. Protected {result.count} page(s) with an open password:"
            f"\n  {result.path}"
        )
        logger.info("Protect (open password) complete: output='%s'", result.path)

    queue_task(f"Protect (open password) {source.name} -> {out_path.name}", _run,
               sources=[capture_file_source(source)])


def _prompt_blocked_actions():
    """Ask which actions to block. Returns the allowed-permissions bitmask,
    the list of blocked labels, or None to go back.
    """
    actions = restrictable_actions()
    print_note("Choose which actions to BLOCK in the protected file:")
    for index, (label, _bits) in enumerate(actions, start=1):
        print(f"    {colorize(f'{index}.', Color.LIGHT_BLUE)} {label}")
    prompt = question_prompt(
        "Actions to block",
        details="e.g. 1,3 - or 'all', or Enter for editing+copying",
        default="editing content, copying",
    )
    while True:
        raw = _input(prompt).strip().lower()
        if raw == "0":
            return None
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        if raw == "":
            # Sensible default: block editing and copying, allow the rest.
            chosen = {"editing content", "copying text/images"}
            blocked = [(lbl, bits) for lbl, bits in actions if lbl in chosen]
            break
        if raw == "all":
            blocked = list(actions)
            break
        try:
            picks = parse_index_list(raw, len(actions))
        except ValueError as exc:
            print_error(str(exc))
            continue
        blocked = [actions[i - 1] for i in picks]
        break

    blocked_bits = 0
    for _label, bits in blocked:
        blocked_bits |= bits
    allowed = all_permissions() & ~blocked_bits
    return allowed, [label for label, _bits in blocked]


def operation_protect_restrict() -> None:
    """Restrict editing/printing/etc. behind an owner password (opens freely)."""
    reset_questions()
    print_heading("\nProtect PDF: restrict editing (owner password)")
    logger.info("Operation started: Protect PDF (restrict).")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    doc = _open_source_for_protect(source)
    if doc is None:
        return
    # Only immutable state crosses the queue boundary (A5); see the open-password
    # operation above for the rationale.
    source_pw = source_password(doc)
    page_count = doc.page_count
    close_doc(doc)

    try:
        result = _prompt_blocked_actions()
    except _ExitRequested:
        raise
    if result is None:
        print_warning("Returning to menu.")
        return
    permissions, blocked_labels = result

    try:
        owner_password = prompt_new_password("to change permissions (owner password)")
    except _ExitRequested:
        raise
    if owner_password is None:
        print_warning("Cancelled. Returning to menu.")
        return

    default_path = unique_file_path(source.parent / f"{source.stem}_restricted.pdf")

    print_heading("\nSummary")
    print_kv("Source file", source.name, Color.CYAN)
    print_kv("Total source pages", page_count, Color.GOLD)
    print_kv("Opens without a password", "yes", Color.LIME)
    print_kv("Blocked actions", ", ".join(blocked_labels) or "(none)", Color.RED)
    print_kv("Owner password", "required to change permissions", Color.MAGENTA)
    print_kv("Default Output Path", default_path, Color.AQUA)

    out_path = _choose_output_file(default_path, source)
    if out_path is None:
        print_warning("Returning to menu.")
        return

    def _run():
        rdoc = None
        try:
            rdoc = open_source_pdf(source, password=source_pw)
            result = save_encrypted_pdf(
                rdoc, out_path, user_pw=None, owner_pw=owner_password,
                permissions=permissions,
            )
        except Exception as exc:  # noqa: BLE001 - clean message, log details
            print_error(f"Failed to protect the PDF: {exc}")
            logger.exception("Protect (restrict) failed for '%s'", out_path)
            return
        finally:
            close_doc(rdoc)
        # The written path, not the configured one: promotion may have had to
        # allocate a suffixed sibling.
        print_success(
            f"Done. Restricted {result.count} page(s):\n  {result.path}"
        )
        logger.info("Protect (restrict) complete: output='%s'", result.path)

    queue_task(f"Protect (restrict) {source.name} -> {out_path.name}", _run,
               sources=[capture_file_source(source)])
