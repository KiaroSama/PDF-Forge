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

def _show_pdf_to_images_menu() -> None:
    """Render the PDF-to-images submenu in the Page tools submenu style."""
    print()
    print(colorize(f"{APP_NAME} PDF to images:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} All pages to PNG "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Selected pages to PNG")
    print(f"  {colorize('3.', Color.LIGHT_BLUE)} Batch: all PDFs in a folder to PNG")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Back")
    print()


def pdf_to_images_menu() -> None:
    """Run the PDF-to-images submenu loop (mirrors the Page tools submenu)."""
    while True:
        _show_pdf_to_images_menu()
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

        logger.debug("PDF-to-images menu selection: '%s'", choice)
        set_operation_prompt(choice)  # numbering prefix = selected submenu item.
        try:
            if choice == "1":
                operation_images_all_pages()
            elif choice == "2":
                operation_images_selected_pages()
            elif choice == "3":
                operation_images_batch_folder()
            else:
                print_error("Invalid option. Please choose 1, 2, 3, or 0.")
                continue
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Operation interrupted by user (KeyboardInterrupt).")


def _show_image_pdf_menu() -> None:
    """Render the image-only-PDF submenu in the Page tools submenu style."""
    print()
    print(colorize(f"{APP_NAME} PDF to image-only PDF:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} Single PDF "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Batch: all PDFs in a folder")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Back")
    print()


def pdf_to_image_pdf_menu() -> None:
    """Run the image-only-PDF submenu loop (mirrors the Page tools submenu)."""
    while True:
        _show_image_pdf_menu()
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

        logger.debug("Image-only-PDF menu selection: '%s'", choice)
        set_operation_prompt(choice)  # numbering prefix = selected submenu item.
        try:
            if choice == "1":
                operation_pdf_to_image_pdf()
            elif choice == "2":
                operation_image_pdf_batch_folder()
            else:
                print_error("Invalid option. Please choose 1, 2, or 0.")
                continue
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Operation interrupted by user (KeyboardInterrupt).")


def _show_delete_pages_menu() -> None:
    """Render the delete-pages submenu in the Page tools submenu style."""
    print()
    print(colorize(f"{APP_NAME} Delete pages:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} Single PDF "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Batch: all PDFs in a folder")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Back")
    print()


def delete_pages_menu() -> None:
    """Run the delete-pages submenu loop (mirrors the Page tools submenu)."""
    while True:
        _show_delete_pages_menu()
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

        logger.debug("Delete-pages menu selection: '%s'", choice)
        set_operation_prompt(choice)  # numbering prefix = selected submenu item.
        try:
            if choice == "1":
                operation_delete_pages_single()
            elif choice == "2":
                operation_delete_pages_batch()
            else:
                print_error("Invalid option. Please choose 1, 2, or 0.")
                continue
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Operation interrupted by user (KeyboardInterrupt).")


def _show_compress_menu() -> None:
    """Render the compress submenu in the Page tools submenu style."""
    print()
    print(colorize(f"{APP_NAME} Compress PDF:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} Single PDF "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Batch: all PDFs in a folder")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Back")
    print()


def compress_menu() -> None:
    """Run the compress submenu loop (mirrors the Page tools submenu)."""
    while True:
        _show_compress_menu()
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

        logger.debug("Compress menu selection: '%s'", choice)
        set_operation_prompt(choice)  # numbering prefix = selected submenu item.
        try:
            if choice == "1":
                operation_compress_pdf()
            elif choice == "2":
                operation_compress_pdf_batch()
            else:
                print_error("Invalid option. Please choose 1, 2, or 0.")
                continue
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Operation interrupted by user (KeyboardInterrupt).")


def _show_protect_menu() -> None:
    """Render the protect submenu in the Page tools submenu style."""
    print()
    print(colorize(f"{APP_NAME} Protect PDF:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} Password to open (view) "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Restrict editing (owner password + permissions)")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Back")
    print()


def protect_menu() -> None:
    """Run the protect submenu loop (mirrors the Page tools submenu)."""
    while True:
        _show_protect_menu()
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

        logger.debug("Protect menu selection: '%s'", choice)
        set_operation_prompt(choice)  # numbering prefix = selected submenu item.
        try:
            if choice == "1":
                operation_protect_open_password()
            elif choice == "2":
                operation_protect_restrict()
            else:
                print_error("Invalid option. Please choose 1, 2, or 0.")
                continue
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Operation interrupted by user (KeyboardInterrupt).")


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
    """Render the Page tools submenu: light-blue header and numbered options."""
    print()
    print(colorize(f"{APP_NAME} Page tools:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} Extract selected pages "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Split PDF into fixed-size chunks")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Back")
    print()


def page_tools_menu() -> None:
    """Run the Page tools submenu loop.

    Returns when the user goes Back (option 0). Raises ``_ExitRequested`` when
    the user types 'exit'/'quit' to close the whole application.
    """
    while True:
        show_page_tools_menu()
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
            return  # Back to the main menu.
        if choice in ("exit", "quit"):
            raise _ExitRequested()

        logger.debug("Page tools menu selection: '%s'", choice)
        set_operation_prompt(choice)  # numbering prefix = selected submenu item.
        try:
            if choice == "1":
                operation_extract_pages()
            elif choice == "2":
                operation_split_chunks()
            else:
                print_error("Invalid option. Please choose 1, 2, or 0.")
                continue
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Operation interrupted by user (KeyboardInterrupt).")


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
