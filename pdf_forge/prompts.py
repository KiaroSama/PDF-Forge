from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['_input', 'ask_yes_no', 'prompt_password', 'prompt_source_pdf', '_ExitRequested', '_choose_output_dir_for_files', '_choose_output_file', '_choose_output_dir', '_print_merge_order', 'prompt_image_quality', 'prompt_source_folder_pdfs']

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


def prompt_password() -> Optional[str]:
    """Prompt for a PDF password without echoing it when possible."""
    import getpass

    print_warning("This PDF is encrypted.")
    try:
        return getpass.getpass(colorize("Enter PDF password (input hidden): ", Color.CYAN))
    except (EOFError, KeyboardInterrupt):
        return None


def prompt_source_pdf() -> Optional[Path]:
    """Prompt for and validate a source PDF path. Returns None to go back."""
    prompt = question_prompt("Source PDF path")
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

        # Create destination directory only after explicit confirmation.
        if not chosen.parent.exists():
            if not ask_yes_no(
                f"Directory does not exist:\n  {chosen.parent}\nCreate it?",
                default_yes=True,
            ):
                continue
            try:
                chosen.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print_error(f"Could not create directory: {exc}")
                continue

        # Never overwrite: generate a unique name when needed.
        final = unique_file_path(chosen)
        if final != chosen:
            print_warning(f"Output exists; using a unique name: {final.name}")
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
            return default_folder
        if cleaned == "0":
            return None
        if cleaned.lower() in ("exit", "quit"):
            raise _ExitRequested()
        # Prefer the safer unique-folder approach to avoid filename conflicts.
        return unique_dir_path(Path(cleaned))


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

    Presented as an inline numbered question (same style as other operation
    prompts). Medium is the default: pressing Enter selects it.
    """
    prompt = question_prompt(
        "Output image quality",
        details=(
            f"1=Low ({IMAGE_QUALITY_DPI['low']} DPI), "
            f"2=Medium ({IMAGE_QUALITY_DPI['medium']} DPI), "
            f"3=High ({IMAGE_QUALITY_DPI['high']} DPI)"
        ),
        default="2",
    )
    choices = {"1": "low", "2": "medium", "3": "high"}
    while True:
        raw = _input(prompt).strip().lower()
        if raw == "":
            raw = "2"  # Enter selects Medium.
        if raw == "0":
            return None
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        if raw in choices:
            quality = choices[raw]
            dpi = IMAGE_QUALITY_DPI[quality]
            logger.info("Image quality selected: %s (%d DPI).", quality, dpi)
            return dpi
        print_error("Invalid quality. Please choose 1, 2, or 3.")


def prompt_source_folder_pdfs() -> Optional[List[Path]]:
    """Prompt for a folder and return its PDFs in natural order, or None (Back).

    Used by the batch image tools. Requires at least one PDF directly inside the
    folder (non-recursive). Entering ``0`` goes back one step.
    """
    prompt = question_prompt("Folder containing PDFs")
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
