from __future__ import annotations

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403
from .ops_pages import *  # noqa: F401,F403
from .ops_merge import *  # noqa: F401,F403
from .ops_convert import *  # noqa: F401,F403
from .ops_watermark import *  # noqa: F401,F403
from .ops_compress import *  # noqa: F401,F403
from .ops_unlock import *  # noqa: F401,F403
from .ops_encrypt import *  # noqa: F401,F403
from .ops_office import *  # noqa: F401,F403

__all__ = ['_show_pdf_to_images_menu', 'pdf_to_images_menu', '_show_image_pdf_menu', 'pdf_to_image_pdf_menu', '_show_delete_pages_menu', 'delete_pages_menu', '_show_compress_menu', 'compress_menu', '_show_protect_menu', 'protect_menu', 'show_menu', 'show_page_tools_menu', 'page_tools_menu', 'main_menu']

# Six submenus share one loop. Each config is (title, debug-log tag, options),
# where options is an ordered tuple of (label, operation-function NAME). Names,
# not function objects, so dispatch resolves through the module namespace at
# call time - the same late binding the original had when each loop called its
# operations by name, and what lets a test monkeypatch an operation.
_SUBMENUS = {
    "page_tools": ("Page tools", "Page tools menu selection", (
        ("Extract selected pages", "operation_extract_pages"),
        ("Split PDF into fixed-size chunks", "operation_split_chunks"),
    )),
    "pdf_to_images": ("PDF to images", "PDF-to-images menu selection", (
        ("All pages to PNG", "operation_images_all_pages"),
        ("Selected pages to PNG", "operation_images_selected_pages"),
        ("Batch: all PDFs in a folder to PNG", "operation_images_batch_folder"),
    )),
    "image_pdf": ("PDF to image-only PDF", "Image-only-PDF menu selection", (
        ("Single PDF", "operation_pdf_to_image_pdf"),
        ("Batch: all PDFs in a folder", "operation_image_pdf_batch_folder"),
    )),
    "delete_pages": ("Delete pages", "Delete-pages menu selection", (
        ("Single PDF", "operation_delete_pages_single"),
        ("Batch: all PDFs in a folder", "operation_delete_pages_batch"),
    )),
    "compress": ("Compress PDF", "Compress menu selection", (
        ("Single PDF", "operation_compress_pdf"),
        ("Batch: all PDFs in a folder", "operation_compress_pdf_batch"),
    )),
    "protect": ("Protect PDF", "Protect menu selection", (
        ("Password to open (view)", "operation_protect_open_password"),
        ("Restrict editing (owner password + permissions)",
         "operation_protect_restrict"),
    )),
}


def _render_submenu(key: str) -> None:
    """Print one submenu in the shared Page-tools style."""
    title, _log, options = _SUBMENUS[key]
    print()
    print(colorize(f"{APP_NAME} {title}:", Color.BOLD + Color.LIGHT_BLUE))
    for index, (label, _op) in enumerate(options, start=1):
        marker = f" {colorize('[1]', Color.GREEN)}" if index == 1 else ""
        print(f"  {colorize(f'{index}.', Color.LIGHT_BLUE)} {label}{marker}")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Back")
    print()


def _run_submenu(key: str) -> None:
    """Run one submenu loop. Returns on 0=Back; raises on exit/quit.

    Dispatch is an exact-string match (like the original per-option ``if``
    chain), so "01" or "1.0" are invalid, not option 1.
    """
    _title, log_tag, options = _SUBMENUS[key]
    dispatch = {str(i): op for i, (_label, op) in enumerate(options, start=1)}
    valid = ", ".join(str(i) for i in range(1, len(options) + 1))
    while True:
        _render_submenu(key)
        choice = _input(
            colorize("Select an option ", Color.BOLD)
            + colorize("[1]", Color.GREEN)
            + " "
            + back_text("back=0, quit=exit")
            + colorize(": ", Color.WHITE)
        ).strip().lower()

        if choice == "":
            choice = "1"  # Enter selects option 1.

        if choice == "0":
            return
        if choice in ("exit", "quit"):
            raise _ExitRequested()

        logger.debug("%s: '%s'", log_tag, choice)
        set_operation_prompt(choice)  # numbering prefix = selected submenu item.
        try:
            if choice in dispatch:
                # Resolved now, not at table-build time, so a monkeypatched
                # operation is honoured and the binding matches the original.
                globals()[dispatch[choice]]()
            else:
                print_error(f"Invalid option. Please choose {valid}, or 0.")
                continue
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Operation interrupted by user (KeyboardInterrupt).")


def _show_pdf_to_images_menu() -> None:
    """Render the PDF-to-images submenu."""
    _render_submenu("pdf_to_images")


def pdf_to_images_menu() -> None:
    """Run the PDF-to-images submenu loop."""
    _run_submenu("pdf_to_images")


def _show_image_pdf_menu() -> None:
    """Render the image-only-PDF submenu."""
    _render_submenu("image_pdf")


def pdf_to_image_pdf_menu() -> None:
    """Run the image-only-PDF submenu loop."""
    _run_submenu("image_pdf")


def _show_delete_pages_menu() -> None:
    """Render the delete-pages submenu."""
    _render_submenu("delete_pages")


def delete_pages_menu() -> None:
    """Run the delete-pages submenu loop."""
    _run_submenu("delete_pages")


def _show_compress_menu() -> None:
    """Render the compress submenu."""
    _render_submenu("compress")


def compress_menu() -> None:
    """Run the compress submenu loop."""
    _run_submenu("compress")


def _show_protect_menu() -> None:
    """Render the protect submenu."""
    _render_submenu("protect")


def protect_menu() -> None:
    """Run the protect submenu loop."""
    _run_submenu("protect")


def show_menu() -> None:
    """Render the main menu: light-blue header and numbered options."""
    print()
    print(colorize(f"{APP_NAME} Main menu:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} Page tools "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Merge multiple PDFs")
    print(f"  {colorize('3.', Color.LIGHT_BLUE)} Delete pages")
    print(f"  {colorize('4.', Color.LIGHT_BLUE)} PDF to images (PNG)")
    print(f"  {colorize('5.', Color.LIGHT_BLUE)} PDF to image-only PDF")
    print(f"  {colorize('6.', Color.LIGHT_BLUE)} Remove image watermark")
    print(f"  {colorize('7.', Color.LIGHT_BLUE)} Extract images from PDF")
    print(f"  {colorize('8.', Color.LIGHT_BLUE)} Compress PDF (reduce file size)")
    print(f"  {colorize('9.', Color.LIGHT_BLUE)} Protect PDF (set password / restrictions)")
    print(f"  {colorize('10.', Color.LIGHT_BLUE)} Unlock PDF (remove password & restrictions)")
    print(f"  {colorize('11.', Color.LIGHT_BLUE)} Convert documents/spreadsheets/presentations to PDF")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Exit")
    print()


def show_page_tools_menu() -> None:
    """Render the Page tools submenu."""
    _render_submenu("page_tools")


def page_tools_menu() -> None:
    """Run the Page tools submenu loop.

    Returns when the user goes Back (option 0). Raises ``_ExitRequested`` when
    the user types 'exit'/'quit' to close the whole application.
    """
    _run_submenu("page_tools")


def main_menu() -> int:
    """Run the interactive main menu loop. Returns a process exit code.

    Operations do not run immediately: each configured operation is added to a
    batch queue. After a task is queued the user is asked whether to queue
    another (default no); answering no shows the full summary and a single
    "Start now?" confirmation before the whole queue runs together.
    """
    def _goodbye() -> int:
        print_success("Goodbye.")
        logger.info("Application exit.")
        return 0

    # Direct main-menu operations use their main-menu number as the numbering
    # prefix; the submenu launchers set their own (submenu item) prefix.
    direct_ops = {
        "6": operation_remove_watermark,
        "7": operation_extract_images,
        "10": operation_unlock_pdf,
    }
    submenu_launchers = {
        "1": page_tools_menu,
        "3": delete_pages_menu,
        "4": pdf_to_images_menu,
        "5": pdf_to_image_pdf_menu,
        "8": compress_menu,
        "9": protect_menu,
        "11": convert_menu,
    }

    while True:
        set_operation_prompt(None)  # menu-level prompts use plain numbering.
        show_menu()
        choice = _input(
            colorize("Select an option ", Color.BOLD)
            + colorize("[1]", Color.GREEN)
            + " "
            + back_text("quit=exit")
            + colorize(": ", Color.WHITE)
        ).strip().lower()

        if choice == "":
            choice = "1"  # Enter opens Page tools.

        if choice in ("0", "exit", "quit"):
            # Finish any pending queue before leaving.
            finalize_queue()
            return _goodbye()

        logger.debug("Main menu selection: '%s'", choice)
        try:
            if choice == "2":
                operation_merge_pdfs()  # sets its own prefix from the merge submenu.
            elif choice in direct_ops:
                set_operation_prompt(choice)
                direct_ops[choice]()
            elif choice in submenu_launchers:
                submenu_launchers[choice]()  # submenu sets the prefix per item.
            else:
                print_error("Invalid option. Please choose 1-11 or 0.")
                continue
        except _ExitRequested:
            finalize_queue()
            return _goodbye()
        except _TaskQueued:
            # A task was configured and added to the queue. Ask whether to add
            # another (default no = Enter), then finalize when the user is done.
            set_operation_prompt(None)  # control prompts use plain numbering.
            try:
                add_more = ask_yes_no(
                    "\nDo you want to queue another task?", default_yes=False
                )
            except _ExitRequested:
                finalize_queue()
                return _goodbye()
            if not add_more:
                if finalize_queue():  # True => exit/quit typed at "Start now?".
                    return _goodbye()
            # Either way, loop back to a fresh main menu (the queue is now empty
            # unless the user chose to keep adding tasks).
            continue
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Operation interrupted by user (KeyboardInterrupt).")
