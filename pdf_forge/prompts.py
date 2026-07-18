from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['_input', 'ask_yes_no', 'prompt_password', 'prompt_new_password',
           'resolve_protection', 'resolve_merge_protection', 'prompt_source_pdf', '_ExitRequested', '_choose_output_dir_for_files', '_choose_output_file', '_choose_output_dir', '_print_merge_order', 'prompt_image_quality', '_prompt_custom_dpi', 'prompt_source_folder_pdfs']

def _input(prompt: str) -> str:
    """Read a line of input, treating EOF as a request to exit."""
    try:
        return input(prompt)
    except EOFError:
        # No interactive input available; behave like the exit command.
        return "exit"


def ask_yes_no(question: str, default_yes: bool = True) -> bool:
    """Ask a yes/no question. Empty input selects the default (Yes by default).

    Typing 'exit' or 'quit' raises _ExitRequested to close the application.
    """
    default_char = "y" if default_yes else "n"
    prompt = question_prompt(
        question, details="y/n", default=default_char, back="quit=exit"
    )
    while True:
        answer = _input(prompt).strip().lower()
        if answer == "":
            return default_yes
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if answer in ("exit", "quit"):
            raise _ExitRequested()
        print_error("Please answer with 'y', 'n', or type 'exit' to quit.")


def prompt_password(previous_failed: bool = False) -> Optional[str]:
    """Prompt for a PDF password without echoing it when possible.

    Returns the entered password, or ``None`` when the user navigates away by
    typing ``0``, ``back``, or ``skip`` (case-insensitive). ``exit``/``quit``
    raise :class:`_ExitRequested`. There is no attempt limit: the caller
    (:func:`open_source_pdf`) re-invokes this until a correct password is
    entered or the user cancels. Because input is hidden, the navigation words
    cannot double as a literal password (a documented, intentional limitation).
    """
    import getpass

    if previous_failed:
        print_error(
            "Incorrect password. Try again, or type 0/back to cancel "
            "(exit/quit to close)."
        )
    else:
        print_warning("This PDF is encrypted.")
    try:
        entry = getpass.getpass(
            colorize(
                "Enter PDF password (hidden; 0/back to cancel): ", Color.CYAN
            )
        )
    except (EOFError, KeyboardInterrupt):
        return None
    nav = entry.strip().lower()
    if nav in ("0", "back", "skip"):
        return None
    if nav in ("exit", "quit"):
        raise _ExitRequested()
    return entry


def prompt_new_password(purpose: str) -> Optional[str]:
    """Ask for a new password (hidden), entered twice to confirm.

    ``purpose`` is shown in the prompt (e.g. "to open the file"). Returns the
    password, or ``None`` if the user cancels with an empty entry. Raises
    ``_ExitRequested`` on 'exit'/'quit'.
    """
    import getpass

    print_note(f"Set a password {purpose}. Leave empty to cancel.")
    while True:
        try:
            first = getpass.getpass(
                colorize("  Enter password (hidden): ", Color.CYAN)
            )
        except (EOFError, KeyboardInterrupt):
            return None
        if first == "":
            return None
        if first.lower() in ("exit", "quit"):
            raise _ExitRequested()
        try:
            second = getpass.getpass(
                colorize("  Confirm password (hidden): ", Color.CYAN)
            )
        except (EOFError, KeyboardInterrupt):
            return None
        if first != second:
            print_error("The passwords do not match. Please try again.")
            continue
        return first


def resolve_protection(policy, context: str = "output") -> Optional[object]:
    """Apply PDF Forge's protection policy, asking the user when it cannot be kept.

    Policy (documented in the README):
      * unprotected source  -> unprotected output, no question asked;
      * open-password source -> the output is re-encrypted AES-256 with the same
        password and permission bits (technically safe: the password is known).
        The user is told, not asked;
      * owner-restricted source -> the owner password cannot be recovered, so the
        policy cannot be reproduced. Warn and require an intentional choice
        instead of silently dropping the restrictions.

    Returns the policy to apply when writing (possibly downgraded to "none"), or
    ``None`` when the user cancels.
    """
    if policy is None or not policy.is_protected:
        return policy

    if policy.can_preserve:
        print_note(
            f"The source needs a password to open. The {context} will be "
            "re-protected with the same password and permissions."
        )
        return policy

    # Owner-restricted: cannot reproduce faithfully.
    print_warning(
        "This PDF opens without a password but restricts: "
        + ", ".join(policy.denied)
        + ".\nThose restrictions are enforced by an owner password that cannot "
        f"be recovered, so the {context} cannot reproduce them."
    )
    if ask_yes_no(
        "Create an UNPROTECTED output instead? "
        "(No = cancel; use 'Protect PDF' afterwards to set your own policy)",
        default_yes=True,
    ):
        logger.info("Protection policy: restricted source -> unprotected output.")
        return ProtectionPolicy(kind="none")
    logger.info("Protection policy: cancelled by user (restricted source).")
    return None


def resolve_merge_protection(policies) -> Optional[object]:
    """Decide the protection policy for a merge of several sources.

    A merge has no single correct answer when sources carry different passwords
    or permissions, so PDF Forge never invents one: if any source is protected,
    it warns and requires an intentional choice. The documented default is an
    unprotected merged output (the user has already proven access to every
    source and can apply 'Protect PDF' afterwards).
    """
    protected = [p for p in policies if p is not None and p.is_protected]
    if not protected:
        return ProtectionPolicy(kind="none")
    print_warning(
        f"{len(protected)} of {len(policies)} source(s) are protected, and a "
        "merge cannot carry several different passwords or permission sets."
    )
    if ask_yes_no(
        "Create an UNPROTECTED merged PDF? "
        "(No = cancel; use 'Protect PDF' afterwards to set one policy)",
        default_yes=True,
    ):
        logger.info("Merge protection policy: unprotected output (%d protected sources).",
                    len(protected))
        return ProtectionPolicy(kind="none")
    logger.info("Merge cancelled at the protection policy question.")
    return None


def prompt_source_pdf() -> Optional[Path]:
    """Prompt for and validate a source PDF path. Returns None to go back."""
    prompt = question_prompt(
        "Source PDF path",
        details=guidance_text(drag_drop_guidance(), GUIDANCE_KEYWORDS),
    )
    while True:
        raw = _input(prompt)
        cleaned = strip_surrounding_quotes(raw)
        if cleaned in ("0", ""):
            if cleaned == "0":
                return None
            print_error("No path entered. Please try again.")
            continue
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
        return path


class _ExitRequested(Exception):
    """Internal signal that the user asked to exit the whole application."""


def _choose_output_dir_for_files(default_dir: Path) -> Optional[Path]:
    """Choose an output directory for multi-file extraction (Enter = source folder)."""
    prompt = question_prompt("Output folder", default="beside source PDF")
    while True:
        raw = _input(prompt)
        cleaned = strip_surrounding_quotes(raw)
        if cleaned == "":
            return default_dir
        if cleaned == "0":
            return None
        if cleaned.lower() in ("exit", "quit"):
            raise _ExitRequested()
        return Path(cleaned)


def _choose_output_file(default_path: Path, source: Path) -> Optional[Path]:
    """Let the user accept the default output or provide a custom directory/file.

    Guarantees the returned path never resolves to the source PDF and never
    overwrites an existing file.
    """
    prompt = question_prompt("Output Path", default=f"{default_path.name} beside source")
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

        # Reject any path that resolves to the source PDF.
        if resolves_to_same_file(chosen, source):
            print_error("The output cannot be the same file as the source PDF.")
            continue

        # Confirm intent to use a not-yet-existing directory, but do NOT create
        # it here: the directory is created inside the task runner so a queued
        # task that is later discarded leaves no empty folder behind (A18).
        if not chosen.parent.exists():
            if not ask_yes_no(
                f"Directory does not exist (it will be created when the task "
                f"runs):\n  {chosen.parent}\nUse it?",
                default_yes=True,
            ):
                continue

        # Never overwrite an existing file or collide with another queued task's
        # reserved output: pick and reserve a unique name.
        final = reserve_unique_file(chosen)
        if final != chosen:
            print_warning(
                f"Output exists or is already queued; using a unique name: "
                f"{final.name}"
            )
        return final


def _choose_output_dir(default_folder: Path) -> Optional[Path]:
    """Let the user accept the default output folder or provide another one.

    Pressing Enter uses the default folder (beside the source PDF).
    """
    prompt = question_prompt("Output folder", default=f"{default_folder.name} beside source")
    while True:
        raw = _input(prompt)
        cleaned = strip_surrounding_quotes(raw)
        if cleaned == "":
            # Reserve even the default so a second queued task gets a fresh name.
            return reserve_unique_dir(default_folder)
        if cleaned == "0":
            return None
        if cleaned.lower() in ("exit", "quit"):
            raise _ExitRequested()
        # Reserve a unique folder so two queued tasks never target the same one.
        chosen = reserve_unique_dir(Path(cleaned))
        if chosen.name != Path(cleaned).name:
            print_warning(f"Folder exists or is already queued; using: {chosen.name}")
        return chosen


def _print_merge_order(sources: Sequence[Path], limit: int = 20) -> None:
    """Print the ordered source list for a merge preview.

    For long lists, show the first items and the last 5 with a gap indicator.
    """
    name_colors = (Color.SKY, Color.VIOLET, Color.TEAL, Color.CORAL, Color.PINK)
    total = len(sources)

    def _line(i: int) -> None:
        print(
            colorize(f"  {i + 1}. ", Color.GREEN + Color.BOLD)
            + colorize(sources[i].name, name_colors[i % len(name_colors)])
        )

    if total <= limit:
        for i in range(total):
            _line(i)
        return
    head = limit - 5
    for i in range(head):
        _line(i)
    print(colorize(f"    ... (+{total - limit} more) ...", Color.DIM))
    for i in range(total - 5, total):
        _line(i)


def prompt_image_quality() -> Optional[int]:
    """Ask for the output image quality; return the render DPI or None (Back).

    Seven levels: six named DPI presets plus Custom (a free DPI value).
    Presented as an inline numbered question (same style as other operation
    prompts). Medium is the default: pressing Enter selects it.
    """
    prompt = question_prompt(
        "Output image quality",
        details=(
            f"1=Very low ({IMAGE_QUALITY_DPI['very low']}), "
            f"2=Low ({IMAGE_QUALITY_DPI['low']}), "
            f"3=Medium ({IMAGE_QUALITY_DPI['medium']}), "
            f"4=High ({IMAGE_QUALITY_DPI['high']}), "
            f"5=Very high ({IMAGE_QUALITY_DPI['very high']}), "
            f"6=Ultra ({IMAGE_QUALITY_DPI['ultra']} DPI), "
            "7=Custom"
        ),
        default="3",
    )
    choices = {
        "1": "very low", "2": "low", "3": "medium",
        "4": "high", "5": "very high", "6": "ultra",
    }
    while True:
        raw = _input(prompt).strip().lower()
        if raw == "":
            raw = "3"  # Enter selects Medium.
        if raw == "0":
            return None
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        if raw in choices:
            quality = choices[raw]
            dpi = IMAGE_QUALITY_DPI[quality]
            logger.info("Image quality selected: %s (%d DPI).", quality, dpi)
            return dpi
        if raw in ("7", "custom"):
            dpi = _prompt_custom_dpi()
            if dpi is None:
                continue  # 0 = back to the quality selection.
            logger.info("Image quality selected: custom (%d DPI).", dpi)
            return dpi
        print_error("Invalid quality. Please choose 1-7.")


def _prompt_custom_dpi() -> Optional[int]:
    """Ask for a custom render DPI (30-1200). Returns None to go back."""
    prompt = question_prompt("Custom DPI", details="30-1200", default="150")
    while True:
        raw = _input(prompt).strip().lower()
        if raw == "":
            raw = "150"
        if raw == "0":
            return None
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        try:
            dpi = int(raw)
        except ValueError:
            print_error("Please enter a whole number between 30 and 1200.")
            continue
        if not 30 <= dpi <= 1200:
            print_error("DPI must be between 30 and 1200.")
            continue
        if dpi > 600:
            print_warning(
                "DPI above 600 produces very large images and can be slow."
            )
        return dpi


def prompt_source_folder_pdfs() -> Optional[List[Path]]:
    """Prompt for a folder and return its PDFs in natural order, or None (Back).

    Used by the batch image tools. Requires at least one PDF directly inside the
    folder (non-recursive). Entering ``0`` goes back one step.
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
        if not pdfs:
            print_error("No PDF files were found in that folder.")
            continue
        return pdfs
