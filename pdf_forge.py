#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF Forge - interactive PDF page tools and merge utility.

This module provides these operations:
  1. Extract a custom selection of pages into a new PDF.
  2. Split a PDF into fixed-size page-range chunks.
  3. Merge multiple PDF files into a single new PDF.

The original PDF is never modified, overwritten, or deleted.

Architecture note:
  The file is organized into clearly separated sections so the core logic
  (page parsing, chunk computation, filename generation) stays independent
  from I/O (pypdf) and from the interactive terminal interface. The pure-core
  functions are import-safe and unit tested in ``tests/``.
"""

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
from typing import List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

APP_NAME = "PDF Forge"            # User-facing application name (never change spelling).
LOG_PREFIX = "PDF Forge"          # Log filename prefix.
APP_VERSION = "1.3.0"

# Image-conversion quality presets mapped to a render resolution in DPI.
# Rendering scale is DPI / 72 (PDF user-space is 72 units per inch).
IMAGE_QUALITY_DPI = {"low": 96, "medium": 150, "high": 300}
DEFAULT_IMAGE_QUALITY = "medium"

# Module level logger; configured by setup_logging().
logger = logging.getLogger("pdf_forge")


# --------------------------------------------------------------------------- #
# Terminal colors
# --------------------------------------------------------------------------- #

class Color:
    """ANSI color codes used for readable terminal output.

    Only bright (high-intensity) foreground colors are used for content so the
    text stays readable on dark terminal themes. Dark/standard-intensity colors
    (30-37) are intentionally avoided because they render poorly on some
    consoles.
    """

    RESET = "\033[0m"
    BOLD = "\033[1m"

    # Curated palette of bright, readable colors for a consistent look.
    RED = "\033[91m"                  # errors
    GREEN = "\033[92m"                # success / default-option marker
    YELLOW = "\033[93m"               # warnings
    BLUE = "\033[38;5;117m"           # info / progress (light sky blue)
    MAGENTA = "\033[38;5;219m"        # accents (light pink-magenta)
    CYAN = "\033[38;5;123m"           # prompts (light cyan)
    WHITE = "\033[97m"                # high-contrast detail text
    GRAY = "\033[38;5;252m"           # field labels
    DIM = "\033[38;5;250m"            # subtle separators
    ORANGE = "\033[38;5;222m"         # accents
    PINK = "\033[38;5;218m"           # accents
    LIME = "\033[38;5;118m"           # accents
    LIGHT_BLUE = "\033[38;5;117m"     # menu headings / option numbers
    NOTE_YELLOW = "\033[38;5;227m"    # informational notes (e.g. "Logging to")

    # Title banner color (truecolor hot pink).
    WIZARD_TITLE = "\033[38;2;255;50;115m"

    # Back/quit prompt accents used by the {back=0, quit=exit} hint.
    BACK_PROMPT = "\033[38;5;166m"    # orange for back=0
    EXIT_PROMPT = "\033[38;5;32m"     # blue for quit=exit

    # Extra accent colors to give the UI a varied ~20-color palette.
    AQUA = "\033[38;5;159m"           # pale aqua
    VIOLET = "\033[38;5;141m"         # soft violet
    TEAL = "\033[38;5;37m"            # teal
    CORAL = "\033[38;5;209m"          # coral
    GOLD = "\033[38;5;220m"           # gold
    SKY = "\033[38;5;75m"             # sky blue
    DEFAULT_NOTE = "\033[38;5;180m"   # muted tan for (Enter=...) notes
    HINT_YELLOW = "\033[38;5;221m"    # yellow used for (y/n) hints


_COLOR_ENABLED = False


def enable_ansi_colors() -> None:
    """Enable ANSI escape sequence processing on the current terminal.

    On Windows 10+ the virtual terminal mode must be enabled explicitly for
    legacy consoles. On other platforms ANSI is assumed available when the
    stream is a TTY. Failures are non-fatal; colors are simply disabled.
    """
    global _COLOR_ENABLED

    if not sys.stdout.isatty():
        _COLOR_ENABLED = False
        return

    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
                _COLOR_ENABLED = True
            else:
                _COLOR_ENABLED = False
        except Exception:  # pragma: no cover - depends on host console
            _COLOR_ENABLED = False
    else:
        _COLOR_ENABLED = True


def colorize(text: str, color: str) -> str:
    """Wrap text in a color code when colors are enabled."""
    if _COLOR_ENABLED:
        return f"{color}{text}{Color.RESET}"
    return text


def print_success(message: str) -> None:
    print(colorize(message, Color.GREEN))


def print_warning(message: str) -> None:
    print(colorize(message, Color.YELLOW))


def print_error(message: str) -> None:
    print(colorize(message, Color.RED))


def print_heading(message: str) -> None:
    print(colorize(message, Color.BOLD + Color.LIGHT_BLUE))


def print_prompt_line(message: str) -> None:
    print(colorize(message, Color.CYAN))


def print_info(message: str) -> None:
    print(colorize(message, Color.BLUE))


def print_note(message: str) -> None:
    """Informational note printed in note-yellow."""
    print(colorize(message, Color.NOTE_YELLOW))


def print_accent(message: str) -> None:
    print(colorize(message, Color.MAGENTA))


def print_kv(label: str, value: str, value_color: str = None) -> None:
    """Print a 'label: value' line: gray label, colored value."""
    if value_color is None:
        value_color = Color.WHITE
    print(
        "  "
        + colorize(f"{label + ':':<19}", Color.GRAY)
        + colorize(str(value), value_color)
    )


def back_text(text: str = "back=0, quit=exit") -> str:
    """Return a colored '{back=0, quit=exit}' control hint.

    'back' parts are orange, 'exit' parts are blue, braces/commas white.
    No trailing colon (question_prompt appends it).
    """
    parts = []
    for part in text.split(", "):
        lowered = part.lower()
        if "back" in lowered:
            parts.append(colorize(part, Color.BACK_PROMPT))
        elif "exit" in lowered:
            parts.append(colorize(part, Color.EXIT_PROMPT))
        else:
            parts.append(colorize(part, Color.WHITE))
    joined = colorize(", ", Color.WHITE).join(parts)
    return colorize("{", Color.WHITE) + joined + colorize("}", Color.WHITE)


# Running question counter, reset at the start of each operation so prompts are
# numbered "1.", "2.", ... per operation.
_question_no = 0


def reset_questions() -> None:
    """Reset the per-operation question counter."""
    global _question_no
    _question_no = 0


def question_prompt(
    title: str,
    details: Optional[str] = None,
    default: Optional[str] = None,
    back: str = "back=0, quit=exit",
) -> str:
    """Build a numbered prompt string ending with ': '.

    Format: '\\nN. {title} ({details}) [{default}] {back}: '
        * title   -> bold (white)
        * details -> hint-yellow inside white parentheses
        * default -> green [default] marker (the Enter value)
        * back    -> colored {back=0, quit=exit} control hint
    """
    global _question_no
    _question_no += 1
    text = "\n" + colorize(f"{_question_no}. {title}", Color.BOLD)
    if details:
        text += (
            " "
            + colorize("(", Color.WHITE)
            + colorize(details, Color.HINT_YELLOW)
            + colorize(")", Color.WHITE)
        )
    if default is not None:
        text += " " + colorize(f"[{default}]", Color.GREEN)
    if back:
        text += " " + back_text(back)
    return text + colorize(": ", Color.WHITE)


def print_banner(text: str) -> None:
    """Print a centered hot-pink title with a single full-width '=' rule.

    One title line + one rule, printed once at startup.
    """
    try:
        width = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        width = 80
    padding = max(0, (width - len(text)) // 2)
    print(" " * padding + colorize(text, Color.BOLD + Color.WIZARD_TITLE))
    print(colorize("=" * width, Color.WIZARD_TITLE))


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def _utc_now() -> datetime.datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.datetime.now(datetime.timezone.utc)


class _UtcFormatter(logging.Formatter):
    """Formatter that renders timestamps as UTC, to the second, no milliseconds."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 (logging API name)
        dt = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _sanitize_for_filename(name: str) -> str:
    """Remove characters that are unsafe in a filename."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()


def setup_logging(script_dir: Path) -> Optional[Path]:
    """Configure file + console logging.

    Creates a uniquely named UTC log file for every execution under ``logs/``.
    Returns the log file path, or ``None`` when persistent logging could not be
    initialized (in which case a console fallback is used).
    """
    log_dir = script_dir / "logs"
    safe_prefix = _sanitize_for_filename(LOG_PREFIX)
    timestamp = _utc_now().strftime("%Y-%m-%d_%H-%M-%S_UTC")

    logger.setLevel(logging.DEBUG)
    # Avoid duplicate handlers if setup runs more than once.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    log_path: Optional[Path] = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        base_name = f"{safe_prefix}_{timestamp}.log"
        log_path = log_dir / base_name
        # Collision-resistant suffix without altering the UTC timestamp format.
        counter = 2
        while log_path.exists():
            log_path = log_dir / f"{safe_prefix}_{timestamp}_{counter}.log"
            counter += 1

        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            _UtcFormatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        )
        logger.addHandler(file_handler)
    except OSError as exc:
        # Console fallback; do not falsely claim a log file was created.
        print_error(f"Persistent logging unavailable: {exc}")
        log_path = None

    # Console handler kept quiet (warnings and above) to keep UX clean.
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.CRITICAL + 1)  # effectively silent
    logger.addHandler(console_handler)

    return log_path


# --------------------------------------------------------------------------- #
# Core domain errors
# --------------------------------------------------------------------------- #

class PageSelectionError(ValueError):
    """Raised when a page-selection expression is invalid."""


class ChunkSizeError(ValueError):
    """Raised when a chunk size value is invalid."""


# --------------------------------------------------------------------------- #
# Core pure functions (no I/O, fully unit-testable)
# --------------------------------------------------------------------------- #

@dataclass
class PageSelectionResult:
    """Result of parsing a page-selection expression."""

    pages: List[int]            # 1-based page numbers, ordered, de-duplicated.
    duplicates_removed: bool    # True when duplicate pages were dropped.


def parse_page_selection(expression: str, total_pages: int) -> PageSelectionResult:
    """Parse a flexible 1-based page-selection expression.

    Supported forms (optional surrounding spaces allowed):
        "5"              -> [5]
        "1,2"            -> [1, 2]
        "10-20"          -> [10, 11, ..., 20]   (inclusive)
        "10-20,25,30-50" -> combined, original order preserved

    Rules:
        * Page numbers are 1-based and must be within 1..total_pages.
        * Ranges are inclusive and must not be reversed (e.g. "20-10" is invalid).
        * Duplicate pages are removed, keeping the first occurrence and order.

    Raises:
        PageSelectionError: with a clear, user-facing message on any problem.
    """
    if expression is None or expression.strip() == "":
        raise PageSelectionError("The page selection is empty.")

    if total_pages < 1:
        raise PageSelectionError("The source PDF has no pages.")

    ordered: List[int] = []
    elements = expression.split(",")

    for raw_element in elements:
        element = raw_element.strip()
        if element == "":
            raise PageSelectionError(
                "Empty element found. Do not leave blank values between commas."
            )

        if "-" in element:
            parts = element.split("-")
            if len(parts) != 2:
                raise PageSelectionError(
                    f"Invalid range '{element}'. Use the form START-END (e.g. 10-20)."
                )
            start_text, end_text = parts[0].strip(), parts[1].strip()
            if not (start_text.isdigit() and end_text.isdigit()):
                raise PageSelectionError(
                    f"Invalid range '{element}'. Both ends must be positive whole numbers."
                )
            start, end = int(start_text), int(end_text)
            if start < 1 or end < 1:
                raise PageSelectionError(
                    f"Invalid range '{element}'. Page numbers start at 1."
                )
            if start > end:
                raise PageSelectionError(
                    f"Reversed range '{element}'. The start must not be greater than the end."
                )
            if end > total_pages:
                raise PageSelectionError(
                    f"Range '{element}' exceeds the document length "
                    f"({total_pages} pages)."
                )
            ordered.extend(range(start, end + 1))
        else:
            if not element.isdigit():
                raise PageSelectionError(
                    f"Invalid page '{element}'. Use positive whole numbers only."
                )
            page = int(element)
            if page < 1:
                raise PageSelectionError(
                    f"Invalid page '{element}'. Page numbers start at 1."
                )
            if page > total_pages:
                raise PageSelectionError(
                    f"Page {page} exceeds the document length ({total_pages} pages)."
                )
            ordered.append(page)

    # Remove duplicates while preserving first occurrence and order.
    seen = set()
    deduped: List[int] = []
    for page in ordered:
        if page not in seen:
            seen.add(page)
            deduped.append(page)

    return PageSelectionResult(
        pages=deduped,
        duplicates_removed=len(deduped) != len(ordered),
    )


@dataclass
class PageGroup:
    """One output group when splitting an extract expression by '|'."""

    text: str                   # Original group text (trimmed), used for naming.
    pages: List[int]            # 1-based page numbers, ordered, de-duplicated.
    duplicates_removed: bool    # True when duplicate pages were dropped.


def parse_multi_file_selection(expression: str, total_pages: int) -> List[PageGroup]:
    """Parse an extract expression that may contain '|' group separators.

    The vertical bar '|' separates independent output files. Within each group
    the normal comma/range syntax applies and produces a single combined file.

    Examples:
        "6-37,39-85,353-375"   -> 1 group  -> one combined file
        "6-37|39-85|353-375"   -> 3 groups -> three separate files
        "6-37,39-85|353-375"   -> 2 groups -> first file = 6-37 + 39-85,
                                              second file = 353-375

    Raises:
        PageSelectionError: with a clear, user-facing message on any problem.
    """
    if expression is None or expression.strip() == "":
        raise PageSelectionError("The page selection is empty.")

    raw_groups = expression.split("|")
    groups: List[PageGroup] = []
    for raw in raw_groups:
        text = raw.strip()
        if text == "":
            raise PageSelectionError(
                "Empty group found. Do not leave a blank value next to '|'."
            )
        result = parse_page_selection(text, total_pages)
        groups.append(
            PageGroup(
                text=text,
                pages=result.pages,
                duplicates_removed=result.duplicates_removed,
            )
        )
    return groups


def compute_chunks(
    total_pages: int,
    chunk_size: int,
    first_page: int = 1,
    last_page: Optional[int] = None,
) -> List[Tuple[int, int]]:
    """Compute inclusive 1-based (start, end) page ranges for fixed-size chunks.

    Chunking is performed across the sub-range ``[first_page, last_page]``.
    When ``first_page``/``last_page`` are omitted, the whole document is used.
    The final chunk contains any remaining pages when the covered span is not an
    exact multiple of ``chunk_size``.

    Raises:
        ChunkSizeError: when the chunk size or the page range is invalid.
    """
    if not isinstance(chunk_size, int):
        raise ChunkSizeError("The chunk size must be a whole number.")
    if chunk_size < 1:
        raise ChunkSizeError("The chunk size must be a positive whole number.")
    if total_pages < 1:
        raise ChunkSizeError("The source PDF has no pages.")

    if last_page is None:
        last_page = total_pages

    if first_page < 1:
        raise ChunkSizeError("The start page must be at least 1.")
    if last_page > total_pages:
        raise ChunkSizeError(
            f"The end page exceeds the document length ({total_pages} pages)."
        )
    if first_page > last_page:
        raise ChunkSizeError(
            "The start page must not be greater than the end page."
        )

    chunks: List[Tuple[int, int]] = []
    start = first_page
    while start <= last_page:
        end = min(start + chunk_size - 1, last_page)
        chunks.append((start, end))
        start = end + 1
    return chunks


def parse_page_number(text: str, default: int, total_pages: int, label: str) -> int:
    """Parse a single 1-based page number with an Enter-to-default fallback.

    An empty input returns ``default``. Rejects non-numeric, decimal, zero,
    negative, and out-of-range values with a clear message.
    """
    if text is None or text.strip() == "":
        return default
    value = text.strip()
    if "." in value:
        raise ChunkSizeError(f"The {label} must be a whole number, not a decimal.")
    if value.startswith("-") or not value.isdigit():
        raise ChunkSizeError(f"The {label} must be a positive whole number.")
    number = int(value)
    if number < 1:
        raise ChunkSizeError(f"The {label} must be at least 1.")
    if number > total_pages:
        raise ChunkSizeError(
            f"The {label} ({number}) exceeds the document length "
            f"({total_pages} pages)."
        )
    return number


def parse_chunk_size(text: str) -> int:
    """Parse and validate a chunk-size string into a positive integer.

    Rejects empty, non-numeric, decimal, zero, and negative values.
    """
    if text is None or text.strip() == "":
        raise ChunkSizeError("No chunk size was entered.")
    value = text.strip()
    # Reject decimals explicitly for a clearer message than isdigit alone.
    if re.fullmatch(r"-?\d+\.\d+", value) or "." in value:
        raise ChunkSizeError("The chunk size must be a whole number, not a decimal.")
    if value.startswith("-"):
        raise ChunkSizeError("The chunk size must be a positive whole number.")
    if not value.isdigit():
        raise ChunkSizeError("The chunk size must be a positive whole number.")
    number = int(value)
    if number < 1:
        raise ChunkSizeError("The chunk size must be at least 1.")
    return number


def parse_index_list(expression: str, count: int) -> List[int]:
    """Parse a comma-separated 1-based index list (e.g. ``1,3``) into ints.

    Validates every index is within ``1..count``. Duplicates are removed and
    the result is returned in ascending order.

    Raises:
        ValueError: with a clear, user-facing message on any problem.
    """
    if expression is None or expression.strip() == "":
        raise ValueError("No selection entered.")
    seen = set()
    for raw in expression.split(","):
        token = raw.strip()
        if token == "":
            raise ValueError("Empty value between commas.")
        if not token.isdigit():
            raise ValueError(f"Invalid number '{token}'. Use whole numbers only.")
        value = int(token)
        if value < 1 or value > count:
            raise ValueError(f"Choice {value} is out of range (1..{count}).")
        seen.add(value)
    return sorted(seen)


def sanitize_selection_text(expression: str) -> str:
    """Convert a page-selection expression into a filename-safe fragment.

    Example: "10-20, 25, 30-50" -> "10-20_25_30-50".
    """
    compact = expression.replace(" ", "")
    compact = compact.replace(",", "_")
    compact = _sanitize_for_filename(compact)
    return compact.strip("_") or "selection"


def build_extract_output_name(
    source_stem: str,
    selection_text: str,
    page_count: int,
    max_stem_length: int = 120,
) -> str:
    """Build a descriptive, length-safe output filename for extracted pages.

    Falls back to a compact name when the descriptive name would be too long.
    """
    fragment = sanitize_selection_text(selection_text)
    descriptive = f"{source_stem}_pages_{fragment}.pdf"
    if len(descriptive) <= max_stem_length:
        return descriptive
    return f"{source_stem}_selected_{page_count}_pages.pdf"


def build_chunk_output_name(source_stem: str, start: int, end: int, pad_width: int) -> str:
    """Build a zero-padded chunk filename, e.g. ``Name_pages_001-050.pdf``."""
    return f"{source_stem}_pages_{start:0{pad_width}d}-{end:0{pad_width}d}.pdf"


def pad_width_for(total_pages: int) -> int:
    """Return the zero-padding width based on the total page count."""
    return max(3, len(str(total_pages)))


def image_dpi_for_quality(quality: str) -> int:
    """Return the render DPI for a quality name ('low' / 'medium' / 'high').

    Raises:
        ValueError: when the quality name is not recognized.
    """
    key = quality.strip().lower()
    if key not in IMAGE_QUALITY_DPI:
        raise ValueError(f"Unknown image quality: {quality!r}")
    return IMAGE_QUALITY_DPI[key]


def build_page_image_name(page_number: int) -> str:
    """Return the PNG filename for a 1-based page number (e.g. 2 -> ``2.png``).

    Files are named after the page number itself, as requested, with no
    zero-padding, so page 2 becomes ``2.png``.
    """
    return f"{page_number}.png"


def default_images_output_dir(source: Path) -> Path:
    """Default folder (pre-uniqueness) for PNG page images of ``source``."""
    return source.parent / f"{source.stem}_images"


def default_image_pdf_output(source: Path) -> Path:
    """Default path (pre-uniqueness) for the rasterized image-only PDF."""
    return source.parent / f"{source.stem}_image.pdf"


def unique_file_path(path: Path) -> Path:
    """Return a non-existing file path, adding ``_2``, ``_3`` suffixes if needed."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def unique_dir_path(path: Path) -> Path:
    """Return a non-existing directory path, adding ``_2``, ``_3`` suffixes if needed."""
    if not path.exists():
        return path
    name, parent = path.name, path.parent
    counter = 2
    while True:
        candidate = parent / f"{name}_{counter}"
        if not candidate.exists():
            return candidate
        counter += 1


def strip_surrounding_quotes(text: str) -> str:
    """Remove a single matching pair of surrounding single or double quotes."""
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def natural_sort_key(name: str) -> List[Tuple[int, int, str]]:
    """Return a case-insensitive, natural-ordering sort key for a filename.

    The name is split into alternating text and numeric chunks so that embedded
    numbers compare by value, not lexically. This orders ``1.pdf``, ``2.pdf``,
    ``10.pdf`` as 1, 2, 10 instead of the lexical 1, 10, 2. Text chunks are
    lower-cased for case-insensitive ordering.

    Each element is a fully comparable ``(kind, number, text)`` tuple where
    ``kind`` is 0 for text chunks and 1 for numeric chunks. Because the split
    always alternates (text at even indices, number at odd indices), the same
    kind is compared at each position, so the keys never mix incomparable
    types. Used as a Python ``sort`` key, equal keys keep their original
    relative order (stable sort).
    """
    tokens = re.split(r"(\d+)", name)
    key: List[Tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        if index % 2 == 1:  # Captured numeric chunk.
            key.append((1, int(token), ""))
        else:               # Surrounding text chunk (may be empty).
            key.append((0, 0, token.lower()))
    return key


def discover_pdfs_in_folder(folder: Path) -> List[Path]:
    """Return the ``*.pdf`` files directly inside ``folder`` (non-recursive).

    The result is sorted by file name using a natural, case-insensitive, stable
    order (see :func:`natural_sort_key`) so files such as ``1.pdf``, ``2.pdf``,
    and ``10.pdf`` are ordered 1, 2, 10. Only regular files with a ``.pdf``
    suffix are returned; subdirectories are not traversed.
    """
    folder = Path(folder)
    pdfs = [
        entry
        for entry in folder.iterdir()
        if entry.is_file() and entry.suffix.lower() == ".pdf"
    ]
    # Natural, case-insensitive, stable ordering (1, 2, 10 -> not 1, 10, 2).
    pdfs.sort(key=lambda p: natural_sort_key(p.name))
    logger.debug(
        "Discovered %d PDF(s) in folder '%s' (natural case-insensitive order).",
        len(pdfs), folder,
    )
    return pdfs


# --------------------------------------------------------------------------- #
# PDF I/O layer (pypdf)
# --------------------------------------------------------------------------- #

def _import_pypdf():
    """Import pypdf lazily so the core module imports without the dependency."""
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore
        from pypdf.errors import PdfReadError  # type: ignore
        return PdfReader, PdfWriter, PdfReadError
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'pypdf' library is required but not installed. "
            "Run the application through Run.ps1 to install dependencies."
        ) from exc


class PdfOpenError(Exception):
    """Raised when a PDF cannot be opened or is unusable."""


def open_source_pdf(path: Path, password_prompt=None):
    """Open and validate a source PDF, handling encryption.

    Args:
        path: Path to the source PDF.
        password_prompt: Optional callable returning a password string when the
            PDF is encrypted and the empty password fails.

    Returns:
        A ``PdfReader`` ready for reading.

    Raises:
        PdfOpenError: with a clear message on any failure.
    """
    PdfReader, _PdfWriter, PdfReadError = _import_pypdf()

    logger.debug("Opening source PDF: '%s'", path)
    try:
        reader = PdfReader(str(path))
    except PdfReadError as exc:
        logger.error("PDF read error for '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF appears to be corrupted or unreadable: {exc}") from exc
    except OSError as exc:
        logger.error("OS error opening '%s': %s", path, exc)
        raise PdfOpenError(f"Could not open the file: {exc}") from exc
    except Exception as exc:  # pypdf can raise various low-level errors
        logger.error("Failed to parse '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF could not be parsed: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        logger.info("Source PDF is encrypted; attempting empty password.")
        decrypted = False
        try:
            # pypdf returns 0 on failure, 1/2 on success.
            if reader.decrypt("") != 0:
                decrypted = True
        except Exception:  # noqa: BLE001 - treat any decrypt error as failure
            decrypted = False

        if not decrypted and password_prompt is not None:
            password = password_prompt()
            if password is not None:
                try:
                    if reader.decrypt(password) != 0:
                        decrypted = True
                except Exception:  # noqa: BLE001
                    decrypted = False
            # Drop the local reference; the password is never logged or stored.
            del password

        if not decrypted:
            raise PdfOpenError(
                "The PDF is encrypted and could not be decrypted with the "
                "provided password."
            )
        logger.info("Source PDF decrypted successfully.")

    try:
        page_count = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not determine page count for '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF page count could not be determined: {exc}") from exc

    if page_count < 1:
        logger.error("Source PDF '%s' contains no pages.", path)
        raise PdfOpenError("The PDF contains no pages.")

    logger.info("Opened source PDF '%s' (%d page(s)).", path, page_count)
    return reader


def write_pages_to_pdf(reader, pages_zero_based: Sequence[int], out_path: Path,
                       progress=None) -> int:
    """Write the given 0-based pages to ``out_path`` using a safe temp file.

    The data is first written to a temporary file in the destination directory,
    validated, then atomically renamed to the final path. Temporary files are
    removed on failure. Returns the number of pages written.
    """
    _PdfReader, PdfWriter, _PdfReadError = _import_pypdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    writer = PdfWriter()
    total = len(pages_zero_based)
    logger.debug("Writing %d page(s) to '%s'.", total, out_path)
    for index, page_index in enumerate(pages_zero_based, start=1):
        writer.add_page(reader.pages[page_index])
        if progress is not None:
            progress(index, total)

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    tmp_path = Path(tmp_name)
    logger.debug("Temporary write file: '%s'", tmp_path)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            writer.write(handle)

        # Validate the temporary PDF before promoting it to the final name.
        _validate_written_pdf(tmp_path, expected_pages=total)
        logger.debug("Validated temporary output (%d page(s)).", total)

        # Atomic promotion. The final path is guaranteed unique by the caller,
        # so os.replace will not clobber an unrelated file.
        os.replace(tmp_path, out_path)
    except Exception:
        # Clean up only the temporary file created by this operation.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
                logger.debug("Removed temporary file after failure: '%s'", tmp_path)
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise
    finally:
        writer.close()

    elapsed = time.perf_counter() - started
    logger.info(
        "Wrote '%s' (%d page(s)) in %.2fs.", out_path, total, elapsed
    )
    return total


def _validate_written_pdf(path: Path, expected_pages: int) -> None:
    """Reopen a freshly written PDF and confirm its page count."""
    PdfReader, _PdfWriter, _PdfReadError = _import_pypdf()
    check = PdfReader(str(path))
    actual = len(check.pages)
    if actual != expected_pages:
        raise PdfOpenError(
            f"Output validation failed: expected {expected_pages} pages, "
            f"found {actual}."
        )


def _validate_merged_pdf(path: Path, expected_pages: int) -> None:
    """Reopen a freshly merged PDF and confirm it is usable.

    Verifies the output can be opened, is not encrypted, and contains exactly
    the expected total page count.
    """
    PdfReader, _PdfWriter, _PdfReadError = _import_pypdf()
    check = PdfReader(str(path))
    if getattr(check, "is_encrypted", False):
        raise PdfOpenError("Output validation failed: the merged PDF is encrypted.")
    actual = len(check.pages)
    if actual != expected_pages:
        raise PdfOpenError(
            f"Output validation failed: expected {expected_pages} pages, "
            f"found {actual}."
        )


def write_merged_pdfs_to_pdf(readers, out_path: Path, progress=None) -> int:
    """Merge already-opened PDF readers into a single PDF at ``out_path``.

    Pages from each reader are appended in order using ``PdfWriter.add_page``.
    The data is written to a temporary file in the destination directory,
    validated (openable, not encrypted, correct page count), then atomically
    renamed to the final path. Temporary files are removed on failure. Returns
    the total number of pages written.
    """
    _PdfReader, PdfWriter, _PdfReadError = _import_pypdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Expected total used for progress and post-write validation.
    total = sum(len(reader.pages) for reader in readers)
    started = time.perf_counter()
    logger.debug(
        "Merging %d source reader(s), %d total page(s) into '%s'.",
        len(readers), total, out_path,
    )

    writer = PdfWriter()
    written = 0
    for reader_index, reader in enumerate(readers, start=1):
        page_count = len(reader.pages)
        for page in reader.pages:
            writer.add_page(page)
            written += 1
            if progress is not None:
                progress(written, total)
        logger.debug(
            "Appended source %d/%d (%d page(s); running total=%d).",
            reader_index, len(readers), page_count, written,
        )

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    tmp_path = Path(tmp_name)
    logger.debug("Temporary merge file: '%s'", tmp_path)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            writer.write(handle)

        # Validate the temporary PDF before promoting it to the final name.
        _validate_merged_pdf(tmp_path, expected_pages=total)
        logger.debug("Validated merged output (%d page(s)).", total)

        # Atomic promotion. The final path is guaranteed unique by the caller.
        os.replace(tmp_path, out_path)
    except Exception:
        # Clean up only the temporary file created by this operation.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
                logger.debug("Removed temporary merge file after failure: '%s'", tmp_path)
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise
    finally:
        writer.close()

    elapsed = time.perf_counter() - started
    logger.info(
        "Merged %d source(s) -> '%s' (%d page(s)) in %.2fs.",
        len(readers), out_path, total, elapsed,
    )
    return total


def resolves_to_same_file(a: Path, b: Path) -> bool:
    """Return True when two paths resolve to the same file on disk."""
    try:
        return os.path.realpath(str(a)).lower() == os.path.realpath(str(b)).lower() \
            if os.name == "nt" else \
            os.path.realpath(str(a)) == os.path.realpath(str(b))
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Image rendering layer (pypdfium2 + Pillow)
# --------------------------------------------------------------------------- #

# PDFium document-load error code indicating a password is required.
# (Equivalent to pypdfium2.raw.FPDF_ERR_PASSWORD.)
_PDFIUM_ERR_PASSWORD = 4


def _import_pdfium():
    """Import pypdfium2 lazily so the core module imports without the dependency."""
    try:
        import pypdfium2 as pdfium  # type: ignore
        return pdfium
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'pypdfium2' library is required for image conversion but is not "
            "installed. Run the application through Run.ps1 to install dependencies."
        ) from exc


def _import_pillow():
    """Import Pillow lazily so the core module imports without the dependency."""
    try:
        from PIL import Image  # type: ignore
        return Image
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'Pillow' library is required for image conversion but is not "
            "installed. Run the application through Run.ps1 to install dependencies."
        ) from exc


def open_pdfium_document(path: Path, password_prompt=None):
    """Open a PDF for rendering with pypdfium2, handling encryption.

    Returns:
        A ``(document, page_count)`` tuple. The caller is responsible for
        closing the document with ``document.close()``.

    Raises:
        PdfOpenError: with a clear message on any failure.
    """
    pdfium = _import_pdfium()

    logger.debug("Opening PDF for rendering: '%s'", path)
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        # A password-required document is the only case we can recover from.
        if getattr(exc, "err_code", None) == _PDFIUM_ERR_PASSWORD and password_prompt is not None:
            logger.info("PDF is encrypted; prompting for a password (render path).")
            password = password_prompt()
            if password is None:
                raise PdfOpenError(
                    "The PDF is encrypted and no password was provided."
                ) from exc
            try:
                pdf = pdfium.PdfDocument(str(path), password=password)
            except pdfium.PdfiumError as exc2:
                logger.error("Render open failed after password for '%s': %s", path, exc2)
                raise PdfOpenError(
                    "The PDF is encrypted and could not be opened with the "
                    "provided password."
                ) from exc2
            finally:
                # The password is never logged or stored.
                del password
        else:
            logger.error("Render open failed for '%s': %s", path, exc)
            raise PdfOpenError(
                f"The PDF could not be opened for rendering: {exc}"
            ) from exc
    except OSError as exc:
        logger.error("OS error opening '%s' for rendering: %s", path, exc)
        raise PdfOpenError(f"Could not open the file: {exc}") from exc

    try:
        page_count = len(pdf)
    except Exception as exc:  # noqa: BLE001
        pdf.close()
        raise PdfOpenError(f"The PDF page count could not be determined: {exc}") from exc

    if page_count < 1:
        pdf.close()
        raise PdfOpenError("The PDF contains no pages.")

    logger.info("Opened PDF for rendering '%s' (%d page(s)).", path, page_count)
    return pdf, page_count


def _validate_image_file(path: Path) -> None:
    """Reopen a freshly written image and confirm it is a valid, non-empty file."""
    Image = _import_pillow()
    if path.stat().st_size <= 0:
        raise PdfOpenError("Output validation failed: the image file is empty.")
    with Image.open(path) as image:
        image.verify()  # Raises if the image is truncated or corrupt.


def _render_page_to_rgb_image(pdf, page_index: int, dpi: int):
    """Render one 0-based page to an RGB PIL image at the given DPI."""
    Image = _import_pillow()  # Ensure Pillow is present before rendering.
    scale = dpi / 72.0
    page = pdf[page_index]
    try:
        bitmap = page.render(scale=scale)
        try:
            image = bitmap.to_pil().convert("RGB")
        finally:
            bitmap.close()
    finally:
        page.close()
    return image


def render_pages_to_pngs(pdf, pages_zero_based: Sequence[int], out_dir: Path,
                         dpi: int, progress=None) -> List[Path]:
    """Render the given 0-based pages to PNG files named after their page number.

    Each page is written safely (temporary file -> validate -> atomic rename) and
    never overwrites an existing file. Returns the list of created file paths.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    total = len(pages_zero_based)
    created: List[Path] = []
    logger.debug("Rendering %d page(s) to PNG at %d DPI in '%s'.", total, dpi, out_dir)

    for index, page_index in enumerate(pages_zero_based, start=1):
        page_number = page_index + 1
        image = _render_page_to_rgb_image(pdf, page_index, dpi)
        final_path = unique_file_path(out_dir / build_page_image_name(page_number))

        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp", prefix=".pdfforge_", dir=str(out_dir)
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            image.save(tmp_path, "PNG")
            _validate_image_file(tmp_path)
            os.replace(tmp_path, final_path)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                logger.warning("Failed to remove temporary file: %s", tmp_path)
            raise
        finally:
            image.close()

        created.append(final_path)
        logger.debug("Wrote page %d -> '%s'.", page_number, final_path.name)
        if progress is not None:
            progress(index, total)

    elapsed = time.perf_counter() - started
    logger.info(
        "Rendered %d PNG(s) at %d DPI into '%s' in %.2fs.",
        len(created), dpi, out_dir, elapsed,
    )
    return created


def render_pdf_to_image_pdf(pdf, page_count: int, out_path: Path, dpi: int,
                            progress=None) -> int:
    """Rasterize every page and assemble the images into one image-only PDF.

    Every page is rendered to an image at the given DPI, then Pillow writes all
    images into a single PDF (temporary file -> validate -> atomic rename). The
    result contains no selectable text, which is the point: the output is not
    editable. Returns the number of pages written.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    logger.debug(
        "Rasterizing %d page(s) at %d DPI into image-only PDF '%s'.",
        page_count, dpi, out_path,
    )

    images = []
    try:
        for page_index in range(page_count):
            images.append(_render_page_to_rgb_image(pdf, page_index, dpi))
            if progress is not None:
                progress(page_index + 1, page_count)

        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            # 'resolution' sets the DPI metadata so each rasterized page keeps
            # its original physical size (pixels / dpi == original inches).
            images[0].save(
                tmp_path,
                "PDF",
                save_all=True,
                append_images=images[1:],
                resolution=float(dpi),
            )
            # Reuse the merged-PDF validation: openable, not encrypted, page count.
            _validate_merged_pdf(tmp_path, expected_pages=page_count)
            os.replace(tmp_path, out_path)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                logger.warning("Failed to remove temporary file: %s", tmp_path)
            raise
    finally:
        for image in images:
            try:
                image.close()
            except Exception:  # noqa: BLE001 - closing must never mask errors
                pass

    elapsed = time.perf_counter() - started
    logger.info(
        "Wrote image-only PDF '%s' (%d page(s)) at %d DPI in %.2fs.",
        out_path, page_count, dpi, elapsed,
    )
    return page_count


# --------------------------------------------------------------------------- #
# Watermark removal layer (repeated image XObjects)
# --------------------------------------------------------------------------- #

@dataclass
class WatermarkCandidate:
    """A repeated image XObject that may be a watermark.

    Images are grouped by a lightweight signature ``(width, height, length)``
    where ``length`` is the raw (undecoded) stream length. Identical images
    referenced from many pages therefore collapse into one candidate without
    decoding any pixels, which keeps scanning fast even on large documents.
    """

    signature: Tuple[int, int, int]
    pages: set                 # 1-based page numbers where the image appears.
    width: int
    height: int
    sample_page_index: int     # 0-based page used to render a preview.


def _iter_page_image_xobjects(page):
    """Yield ``(name, image_object)`` for each image XObject on a page."""
    resources = page.get("/Resources")
    if not resources:
        return
    xobjects = resources.get_object().get("/XObject")
    if not xobjects:
        return
    for name, ref in xobjects.get_object().items():
        obj = ref.get_object()
        if obj.get("/Subtype") == "/Image":
            yield str(name), obj


def _stream_raw_length(image_obj) -> int:
    """Return the raw (encoded) stream length without decoding pixels.

    Prefers the stored raw buffer; falls back to the declared ``/Length``.
    Used only to build a cheap image signature, so an occasional 0 is harmless.
    """
    data = getattr(image_obj, "_data", None)
    if data:
        return len(data)
    try:
        length = image_obj.get("/Length")
        if length is not None:
            return int(length.get_object() if hasattr(length, "get_object") else length)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _image_signature(image_obj) -> Tuple[int, int, int]:
    """Return a cheap ``(width, height, raw-length)`` signature for an image.

    Identical images referenced from many pages share the same signature
    without decoding any pixels, which keeps scanning fast on large documents.
    """
    return (
        int(image_obj.get("/Width", 0)),
        int(image_obj.get("/Height", 0)),
        _stream_raw_length(image_obj),
    )


def scan_watermark_candidates(pages, min_pages: int = 2, max_candidates: int = 10):
    """Find image XObjects that repeat across pages (watermark candidates).

    Returns ``(candidates, total_pages)`` where candidates are sorted by page
    coverage (descending) and then by image area. Only images that appear on at
    least ``min_pages`` pages are returned.
    """
    from collections import defaultdict

    groups = defaultdict(lambda: {"pages": set(), "w": 0, "h": 0, "sample": None})
    total = len(pages)
    for index, page in enumerate(pages):
        for _name, obj in _iter_page_image_xobjects(page):
            sig = _image_signature(obj)
            group = groups[sig]
            group["pages"].add(index + 1)
            group["w"], group["h"] = sig[0], sig[1]
            if group["sample"] is None:
                group["sample"] = index

    candidates = [
        WatermarkCandidate(sig, g["pages"], g["w"], g["h"], g["sample"])
        for sig, g in groups.items()
        if len(g["pages"]) >= min_pages
    ]
    candidates.sort(
        key=lambda c: (len(c.pages), c.width * c.height), reverse=True
    )
    logger.debug(
        "Watermark scan: %d repeated image group(s) over %d page(s).",
        len(candidates), total,
    )
    return candidates[:max_candidates], total


def export_watermark_preview(pages, candidate: WatermarkCandidate, out_path: Path) -> bool:
    """Save a PNG preview of a candidate image. Returns True on success.

    The preview is decoded from the candidate's sample page using Pillow via
    pypdf's image extraction, matched by pixel dimensions.
    """
    page = pages[candidate.sample_page_index]
    try:
        for image_file in page.images:
            pil = image_file.image
            if pil is None:
                continue
            if pil.width == candidate.width and pil.height == candidate.height:
                pil.convert("RGB").save(out_path, "PNG")
                return True
    except Exception as exc:  # noqa: BLE001 - preview is best-effort
        logger.warning("Preview export failed for %s: %s", candidate.signature, exc)
    return False


def remove_watermark_images(reader, signatures_to_remove, out_path: Path,
                            progress=None) -> int:
    """Remove the paint calls for the given image signatures from every page.

    For each page, the ``<name> Do`` operators that draw a matching image are
    dropped from the content stream and the image is removed from the page
    resources. Content streams are recompressed to avoid file-size bloat. The
    result is written safely (temporary file -> validate -> atomic rename).
    Returns the number of pages modified.
    """
    _PdfReader, _PdfWriter, _PdfReadError = _import_pypdf()
    from pypdf import PdfWriter
    from pypdf.generic import ContentStream, NameObject

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    targets = set(signatures_to_remove)

    writer = PdfWriter()
    writer.append(reader)

    total = len(writer.pages)
    expected_pages = total
    modified = 0
    dropped_ops = 0

    for index, page in enumerate(writer.pages):
        resources = page.get("/Resources")
        xobjects = resources.get_object().get("/XObject") if resources else None
        names_to_drop = set()
        if xobjects:
            xobjects = xobjects.get_object()
            for name, obj in _iter_page_image_xobjects(page):
                if _image_signature(obj) in targets:
                    names_to_drop.add(name)

        if not names_to_drop:
            if progress is not None:
                progress(index + 1, total)
            continue

        content = ContentStream(page.get_contents(), writer)
        kept_ops = []
        for operands, operator in content.operations:
            if operator == b"Do" and operands and str(operands[0]) in names_to_drop:
                dropped_ops += 1
                continue  # Drop the paint call; balanced q/cm/Q remain harmless.
            kept_ops.append((operands, operator))
        content.operations = kept_ops
        page[NameObject("/Contents")] = writer._add_object(content)

        # Also drop the now-unused image from the page resources.
        for name in names_to_drop:
            key = NameObject(name)
            if key in xobjects:
                del xobjects[key]

        try:
            page.compress_content_streams()
        except Exception:  # noqa: BLE001 - compression is best-effort
            logger.warning("Content compression failed on page %d.", index + 1)

        modified += 1
        if progress is not None:
            progress(index + 1, total)

    # Physically drop the now-unreferenced watermark image(s) and merge any
    # duplicate objects to keep the file compact. This only removes unused
    # objects and dedupes identical ones; it never re-encodes retained image
    # data, so the visible content stays byte-for-byte lossless.
    try:
        writer.compress_identical_objects()
        logger.debug("Compacted objects (removed unreferenced, merged duplicates).")
    except Exception:  # noqa: BLE001 - optimization is best-effort
        logger.warning("Object compaction step failed; writing without it.")

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    tmp_path = Path(tmp_name)
    logger.debug("Temporary watermark-removal file: '%s'", tmp_path)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            writer.write(handle)
        _validate_written_pdf(tmp_path, expected_pages=expected_pages)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise

    elapsed = time.perf_counter() - started
    logger.info(
        "Watermark removal: modified=%d page(s), dropped=%d paint op(s), "
        "output='%s' in %.2fs.",
        modified, dropped_ops, out_path, elapsed,
    )
    return modified


# --------------------------------------------------------------------------- #
# Interactive terminal interface
# --------------------------------------------------------------------------- #

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


def _print_progress(prefix: str, current: int, total: int) -> None:
    """Print a single-line progress indicator without flooding the console."""
    if total <= 0:
        return
    # Limit updates to avoid excessive output on large documents.
    step = max(1, total // 50)
    if current == total or current % step == 0:
        percent = int(current * 100 / total)
        # Color each segment distinctly from the surrounding text.
        line = (
            "\r"
            + colorize(f"{prefix}: ", Color.AQUA)
            + colorize(f"{current}/{total}", Color.GOLD)
            + colorize(" (", Color.DIM)
            + colorize(f"{percent}%", Color.LIME)
            + colorize(")", Color.DIM)
        )
        sys.stdout.write(line)
        sys.stdout.flush()
        if current == total:
            sys.stdout.write("\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Operation 1: extract selected pages
# --------------------------------------------------------------------------- #

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

    total_pages = len(reader.pages)
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
        _extract_single_file(reader, source, total_pages, groups[0])
    else:
        _extract_multiple_files(reader, source, total_pages, groups)


def _extract_single_file(reader, source: Path, total_pages: int, group: "PageGroup") -> None:
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
    print_kv("Default output", default_path, Color.AQUA)

    out_path = _choose_output_file(default_path, source)
    if out_path is None:
        print_warning("Returning to menu.")
        return

    if not ask_yes_no("Create this PDF now?", default_yes=True):
        print_warning("Cancelled. Returning to menu.")
        return

    pages_zero_based = [p - 1 for p in group.pages]
    try:
        written = write_pages_to_pdf(
            reader,
            pages_zero_based,
            out_path,
            progress=lambda c, t: _print_progress("Extracting", c, t),
        )
    except Exception as exc:  # noqa: BLE001 - present a clean message, log details
        print_error(f"Failed to create the output PDF: {exc}")
        logger.exception("Extraction failed for output '%s'", out_path)
        return

    print_success(f"Done. Wrote {written} page(s) to:\n  {out_path}")
    logger.info("Extract complete: output='%s' pages=%d", out_path, written)


def _extract_multiple_files(reader, source: Path, total_pages: int,
                            groups: "List[PageGroup]") -> None:
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

    out_dir = _choose_output_dir_for_files(source.parent)
    if out_dir is None:
        print_warning("Returning to menu.")
        return

    if not ask_yes_no(f"Create {len(groups)} file(s) now?", default_yes=True):
        print_warning("Cancelled. Returning to menu.")
        return

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print_error(f"Could not create output directory: {exc}")
        logger.error("Failed to create output dir '%s': %s", out_dir, exc)
        return

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
            written = write_pages_to_pdf(reader, pages_zero_based, out_path)
        except Exception as exc:  # noqa: BLE001
            sys.stdout.write("\n")
            print_error(f"Failed while writing '{out_path.name}': {exc}")
            logger.exception("Extract (multi) write failed: '%s'", out_path)
            print_warning(
                f"{len(created_files)} file(s) were completed before the failure."
            )
            _report_created(created_files, total_written, out_dir)
            return
        created_files.append(out_path)
        total_written += written

    print_success(
        f"Done. Created {len(created_files)} file(s), {total_written} page(s) total."
    )
    print_success(f"Output directory:\n  {out_dir}")
    logger.info(
        "Extract (multi) complete: files=%d pages=%d dir='%s'",
        len(created_files), total_written, out_dir,
    )


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


def _format_pages(pages: Sequence[int], limit: int = 30) -> str:
    """Render a page list compactly for display."""
    if len(pages) <= limit:
        return ", ".join(str(p) for p in pages)
    head = ", ".join(str(p) for p in pages[:limit])
    return f"{head}, ... (+{len(pages) - limit} more)"


def summarize_ranges(pages: Sequence[int]) -> str:
    """Collapse a page list into a compact range string.

    Consecutive ascending runs become "start-end"; isolated pages stay single.
    Example: [6,7,...,37, 39,...,85] -> "6-37, 39-85".
    The input order is respected (a new run starts whenever the step != +1).
    """
    if not pages:
        return ""
    parts: List[str] = []
    start = prev = pages[0]
    for page in pages[1:]:
        if page == prev + 1:
            prev = page
        else:
            parts.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = prev = page
    parts.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ", ".join(parts)


def _choose_output_file(default_path: Path, source: Path) -> Optional[Path]:
    """Let the user accept the default output or provide a custom directory/file.

    Guarantees the returned path never resolves to the source PDF and never
    overwrites an existing file.
    """
    prompt = question_prompt("Output", default=f"{default_path.name} beside source")
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


# --------------------------------------------------------------------------- #
# Operation 2: split into fixed-size chunks
# --------------------------------------------------------------------------- #

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

    total_pages = len(reader.pages)
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

    out_dir = _choose_output_dir(default_folder)
    if out_dir is None:
        print_warning("Returning to menu.")
        return

    if not ask_yes_no("Create these files now?", default_yes=True):
        print_warning("Cancelled. Returning to menu.")
        return

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print_error(f"Could not create output directory: {exc}")
        logger.error("Failed to create output dir '%s': %s", out_dir, exc)
        return

    created_files: List[Path] = []
    total_written = 0
    for index, (start, end) in enumerate(chunks, start=1):
        name = build_chunk_output_name(source.stem, start, end, pad)
        # Guarantee uniqueness even if a stray file exists in a reused folder.
        out_path = unique_file_path(out_dir / name)
        pages_zero_based = list(range(start - 1, end))
        _print_progress("Writing chunks", index, len(chunks))
        try:
            written = write_pages_to_pdf(reader, pages_zero_based, out_path)
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
        created_files.append(out_path)
        total_written += written

    print_success(
        f"Done. Created {len(created_files)} file(s), {total_written} page(s) total."
    )
    print_success(f"Output directory:\n  {out_dir}")
    logger.info(
        "Split complete: files=%d pages=%d dir='%s'",
        len(created_files), total_written, out_dir,
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


# --------------------------------------------------------------------------- #
# Operation 3: merge multiple PDFs
# --------------------------------------------------------------------------- #

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

    Requires at least 2 distinct PDF files. Pressing Enter on an empty prompt
    finishes once enough files are gathered. Entering '0' cancels (Back). The
    merge order matches the order entered. Duplicate files are rejected.
    """
    print_note(
        "Enter PDF paths one at a time. Add at least 2 files, then press Enter "
        "to finish."
    )
    selected: List[Path] = []
    while True:
        default = "finish" if len(selected) >= 2 else None
        prompt = question_prompt(f"PDF file #{len(selected) + 1}", default=default)
        raw = _input(prompt)
        cleaned = strip_surrounding_quotes(raw)

        if cleaned == "":
            if len(selected) >= 2:
                return selected
            print_error("Add at least 2 PDF files before finishing.")
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
        if len(pdfs) < 2:
            print_error(
                f"Found {len(pdfs)} PDF file(s) in that folder; at least 2 are "
                "required to merge."
            )
            return None
        return pdfs


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


def _choose_output_file_for_merge(default_path: Path,
                                  sources: Sequence[Path]) -> Optional[Path]:
    """Choose the merged output path (Enter = default beside the source).

    Guarantees the result never resolves to any source PDF and never overwrites
    an existing file.
    """
    prompt = question_prompt("Output", default=f"{default_path.name}")
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
    print_kv("Output path", out_path, Color.AQUA)
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

    # Open every source up front. Fail before writing if any source cannot be
    # opened, so no partial output is ever created.
    readers = []
    total_pages = 0
    current = sources[0]
    try:
        for current in sources:
            reader = open_source_pdf(current, password_prompt=prompt_password)
            readers.append(reader)
            total_pages += len(reader.pages)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(f"Cannot merge: failed to open '{current.name}': {exc}")
        logger.error("Merge aborted; failed to open '%s': %s", current, exc)
        return

    logger.info(
        "All %d merge source(s) opened successfully; total pages=%d (sort=%s).",
        len(sources), total_pages, _describe_merge_sort_mode(mode),
    )

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

    if not ask_yes_no("Create merged PDF?", default_yes=True):
        print_warning("Cancelled. Returning to the merge menu.")
        logger.info("Merge cancelled at confirmation by user.")
        return

    logger.info("Merge start: sources=%d output='%s'", len(sources), out_path)
    try:
        written = write_merged_pdfs_to_pdf(
            readers,
            out_path,
            progress=lambda c, t: _print_progress("Merging pages", c, t),
        )
    except Exception as exc:  # noqa: BLE001 - present a clean message, log details
        print_error(f"Failed to create the merged PDF: {exc}")
        logger.exception("Merge failed for output '%s'", out_path)
        return

    print_success(
        f"Done. Merged {len(sources)} file(s), {written} page(s) into:\n  {out_path}"
    )
    logger.info("Merge complete: output='%s' pages=%d", out_path, written)


# --------------------------------------------------------------------------- #
# Operation 4/5: PDF -> images (PNG) and PDF -> image-only PDF
# --------------------------------------------------------------------------- #

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


def operation_images_all_pages() -> None:
    """Render every page of a PDF to a PNG named after its page number."""
    reset_questions()
    print_heading("\nPDF to images: all pages")
    logger.info("Operation started: PDF to images (all pages).")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    try:
        pdf, total_pages = open_pdfium_document(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s' for rendering: %s", source, exc)
        return

    try:
        print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
        default_folder = unique_dir_path(default_images_output_dir(source))

        print_heading("\nSummary")
        print_kv("Source file", source.name, Color.CYAN)
        print_kv("Total source pages", total_pages, Color.GOLD)
        print_kv("Pages to export", f"all ({total_pages})", Color.LIME)
        print_kv("Image format", "PNG", Color.ORANGE)
        print_kv("Quality", f"{dpi} DPI", Color.PINK)
        print_kv("Output directory", default_folder, Color.AQUA)

        out_dir = _choose_output_dir(default_folder)
        if out_dir is None:
            print_warning("Returning to menu.")
            return

        if not ask_yes_no("Create these images now?", default_yes=True):
            print_warning("Cancelled. Returning to menu.")
            logger.info("PDF-to-images (all) cancelled at confirmation.")
            return

        pages_zero_based = list(range(total_pages))
        _render_pngs_and_report(pdf, pages_zero_based, out_dir, dpi)
    finally:
        pdf.close()


def operation_images_selected_pages() -> None:
    """Render a chosen selection of pages to PNGs named after their page number."""
    reset_questions()
    print_heading("\nPDF to images: selected pages")
    logger.info("Operation started: PDF to images (selected pages).")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    try:
        pdf, total_pages = open_pdfium_document(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s' for rendering: %s", source, exc)
        return

    try:
        print_success(f"Loaded '{source.name}' - {total_pages} page(s).")

        selection_prompt = question_prompt(
            "Pages to export as images",
            details="e.g. 5 or 10-20 or 10-20,25,30-50",
        )
        while True:
            expression = _input(selection_prompt).strip()
            if expression == "0":
                return
            if expression.lower() in ("exit", "quit"):
                raise _ExitRequested()
            try:
                result = parse_page_selection(expression, total_pages)
                break
            except PageSelectionError as exc:
                print_error(f"Invalid selection: {exc}")

        if result.duplicates_removed:
            print_warning("Duplicate pages were removed; first occurrence kept.")
        logger.info(
            "Image selection parsed: expression='%s' pages=%d",
            expression, len(result.pages),
        )

        default_folder = unique_dir_path(default_images_output_dir(source))

        print_heading("\nSummary")
        print_kv("Source file", source.name, Color.CYAN)
        print_kv("Total source pages", total_pages, Color.GOLD)
        print_kv("Pages to export", summarize_ranges(result.pages), Color.LIME)
        print_kv("Image count", len(result.pages), Color.MAGENTA)
        print_kv("Image format", "PNG", Color.ORANGE)
        print_kv("Quality", f"{dpi} DPI", Color.PINK)
        print_kv("Output directory", default_folder, Color.AQUA)

        out_dir = _choose_output_dir(default_folder)
        if out_dir is None:
            print_warning("Returning to menu.")
            return

        if not ask_yes_no("Create these images now?", default_yes=True):
            print_warning("Cancelled. Returning to menu.")
            logger.info("PDF-to-images (selected) cancelled at confirmation.")
            return

        pages_zero_based = [p - 1 for p in result.pages]
        _render_pngs_and_report(pdf, pages_zero_based, out_dir, dpi)
    finally:
        pdf.close()


def _render_pngs_and_report(pdf, pages_zero_based: Sequence[int], out_dir: Path,
                            dpi: int) -> None:
    """Render pages to PNGs, then report the result (shared by both PNG flows)."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print_error(f"Could not create output directory: {exc}")
        logger.error("Failed to create output dir '%s': %s", out_dir, exc)
        return

    try:
        created = render_pages_to_pngs(
            pdf,
            pages_zero_based,
            out_dir,
            dpi,
            progress=lambda c, t: _print_progress("Rendering pages", c, t),
        )
    except Exception as exc:  # noqa: BLE001 - present a clean message, log details
        print_error(f"Failed to render images: {exc}")
        logger.exception("PNG rendering failed for output '%s'", out_dir)
        return

    print_success(
        f"Done. Created {len(created)} image(s) in:\n  {out_dir}"
    )
    logger.info("PDF-to-images complete: images=%d dir='%s'", len(created), out_dir)


def operation_images_batch_folder() -> None:
    """Batch: render every page of every PDF in a folder to PNG images.

    Each PDF is converted independently into its own ``<name>_images`` folder.
    A failure on one file is reported and skipped without stopping the batch.
    """
    reset_questions()
    print_heading("\nPDF to images: batch folder")
    logger.info("Operation started: PDF to images (batch folder).")

    pdfs = prompt_source_folder_pdfs()
    if pdfs is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    folder = pdfs[0].parent
    print_heading("\nSummary")
    print_kv("Folder", folder, Color.CYAN)
    print_kv("PDF files", len(pdfs), Color.MAGENTA)
    print_kv("Image format", "PNG", Color.ORANGE)
    print_kv("Quality", f"{dpi} DPI", Color.PINK)
    print_kv("Per-file output", "<name>_images folder beside each PDF", Color.AQUA)
    print(colorize("\n  Files:", Color.GRAY))
    _print_merge_order(pdfs)

    if not ask_yes_no("Convert all these PDFs to images?", default_yes=True):
        print_warning("Cancelled. Returning to menu.")
        logger.info("Batch image conversion cancelled at confirmation.")
        return

    logger.info("Batch image start: folder='%s' files=%d dpi=%d", folder, len(pdfs), dpi)
    ok = failed = total_images = 0
    for index, src in enumerate(pdfs, start=1):
        print_info(f"[{index}/{len(pdfs)}] {src.name}")
        try:
            pdf, page_count = open_pdfium_document(src, password_prompt=prompt_password)
        except (PdfOpenError, RuntimeError) as exc:
            print_error(f"  Skipped (could not open): {exc}")
            logger.error("Batch image: failed to open '%s': %s", src, exc)
            failed += 1
            continue
        try:
            out_dir = unique_dir_path(default_images_output_dir(src))
            created = render_pages_to_pngs(
                pdf, list(range(page_count)), out_dir, dpi,
                progress=lambda c, t: _print_progress("  Rendering", c, t),
            )
            total_images += len(created)
            ok += 1
            print_success(f"  -> {len(created)} image(s) in {out_dir.name}")
        except Exception as exc:  # noqa: BLE001 - keep the batch going
            print_error(f"  Failed: {exc}")
            logger.exception("Batch image render failed for '%s'", src)
            failed += 1
        finally:
            pdf.close()

    print_success(
        f"Done. Converted {ok} file(s), {failed} failed, {total_images} image(s) total."
    )
    logger.info(
        "Batch image complete: ok=%d failed=%d images=%d", ok, failed, total_images
    )


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


def operation_pdf_to_image_pdf() -> None:
    """Rasterize a single PDF and rebuild it as an image-only (non-editable) PDF."""
    reset_questions()
    print_heading("\nPDF to image-only PDF: single file")
    logger.info("Operation started: PDF to image-only PDF (single file).")

    try:
        source = prompt_source_pdf()
    except _ExitRequested:
        raise
    if source is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    try:
        pdf, total_pages = open_pdfium_document(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s' for rendering: %s", source, exc)
        return

    try:
        print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
        default_path = unique_file_path(default_image_pdf_output(source))

        print_heading("\nSummary")
        print_kv("Source file", source.name, Color.CYAN)
        print_kv("Total source pages", total_pages, Color.GOLD)
        print_kv("Quality", f"{dpi} DPI", Color.PINK)
        print_kv("Result", "image-only PDF (not editable)", Color.LIME)
        print_kv("Default output", default_path, Color.AQUA)

        out_path = _choose_output_file(default_path, source)
        if out_path is None:
            print_warning("Returning to menu.")
            return

        print_warning(
            "The output will be rasterized: text becomes images and is no longer "
            "selectable or editable. This usually increases the file size."
        )
        if not ask_yes_no("Create image-only PDF?", default_yes=True):
            print_warning("Cancelled. Returning to menu.")
            logger.info("PDF-to-image-PDF cancelled at confirmation.")
            return

        logger.info(
            "Image-only PDF start: pages=%d dpi=%d output='%s'",
            total_pages, dpi, out_path,
        )
        try:
            written = render_pdf_to_image_pdf(
                pdf,
                total_pages,
                out_path,
                dpi,
                progress=lambda c, t: _print_progress("Rasterizing pages", c, t),
            )
        except Exception as exc:  # noqa: BLE001 - clean message, log details
            print_error(f"Failed to create the image-only PDF: {exc}")
            logger.exception("Image-only PDF failed for output '%s'", out_path)
            return

        print_success(
            f"Done. Wrote {written} rasterized page(s) to:\n  {out_path}"
        )
        logger.info("Image-only PDF complete: output='%s' pages=%d", out_path, written)
    finally:
        pdf.close()


def operation_image_pdf_batch_folder() -> None:
    """Batch: rasterize every PDF in a folder into its own image-only PDF.

    Each PDF becomes ``<name>_image.pdf`` beside it. A failure on one file is
    reported and skipped without stopping the batch.
    """
    reset_questions()
    print_heading("\nPDF to image-only PDF: batch folder")
    logger.info("Operation started: PDF to image-only PDF (batch folder).")

    pdfs = prompt_source_folder_pdfs()
    if pdfs is None:
        return

    dpi = prompt_image_quality()
    if dpi is None:
        return

    folder = pdfs[0].parent
    print_heading("\nSummary")
    print_kv("Folder", folder, Color.CYAN)
    print_kv("PDF files", len(pdfs), Color.MAGENTA)
    print_kv("Quality", f"{dpi} DPI", Color.PINK)
    print_kv("Result", "image-only PDF per file (not editable)", Color.LIME)
    print_kv("Per-file output", "<name>_image.pdf beside each PDF", Color.AQUA)
    print(colorize("\n  Files:", Color.GRAY))
    _print_merge_order(pdfs)

    print_warning(
        "Each output will be rasterized: text becomes images and is no longer "
        "selectable or editable. This usually increases the file size."
    )
    if not ask_yes_no("Convert all these PDFs to image-only PDFs?", default_yes=True):
        print_warning("Cancelled. Returning to menu.")
        logger.info("Batch image-only PDF cancelled at confirmation.")
        return

    logger.info(
        "Batch image-only PDF start: folder='%s' files=%d dpi=%d", folder, len(pdfs), dpi
    )
    ok = failed = total_pages = 0
    for index, src in enumerate(pdfs, start=1):
        print_info(f"[{index}/{len(pdfs)}] {src.name}")
        try:
            pdf, page_count = open_pdfium_document(src, password_prompt=prompt_password)
        except (PdfOpenError, RuntimeError) as exc:
            print_error(f"  Skipped (could not open): {exc}")
            logger.error("Batch image-only PDF: failed to open '%s': %s", src, exc)
            failed += 1
            continue
        try:
            out_path = unique_file_path(default_image_pdf_output(src))
            written = render_pdf_to_image_pdf(
                pdf, page_count, out_path, dpi,
                progress=lambda c, t: _print_progress("  Rasterizing", c, t),
            )
            total_pages += written
            ok += 1
            print_success(f"  -> {out_path.name} ({written} page(s))")
        except Exception as exc:  # noqa: BLE001 - keep the batch going
            print_error(f"  Failed: {exc}")
            logger.exception("Batch image-only PDF failed for '%s'", src)
            failed += 1
        finally:
            pdf.close()

    print_success(
        f"Done. Converted {ok} file(s), {failed} failed, {total_pages} page(s) total."
    )
    logger.info(
        "Batch image-only PDF complete: ok=%d failed=%d pages=%d", ok, failed, total_pages
    )


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


# --------------------------------------------------------------------------- #
# Operation 6: remove a repeated image watermark
# --------------------------------------------------------------------------- #

def _temp_dir() -> Path:
    """The project-local scratch folder (``PDF Forge/temp``).

    Used for transient files such as watermark preview images. It lives next to
    the script so it is always in a known, writable location regardless of where
    the source PDF is.
    """
    return Path(__file__).resolve().parent / "temp"


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
        reader = open_source_pdf(source, password_prompt=prompt_password)
    except (PdfOpenError, RuntimeError) as exc:
        print_error(str(exc))
        logger.error("Failed to open '%s': %s", source, exc)
        return

    total_pages = len(reader.pages)
    print_success(f"Loaded '{source.name}' - {total_pages} page(s).")
    print_info("Scanning for repeated images (watermark candidates)...")
    candidates, total = scan_watermark_candidates(reader.pages)

    if not candidates:
        print_warning(
            "No repeated images were found. This tool only removes image-based "
            "watermarks that repeat across pages (not text or flattened scans)."
        )
        logger.info("Watermark scan found no repeated images in '%s'.", source)
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
        ok = export_watermark_preview(reader.pages, cand, preview_path)
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
            back="cancel=0, quit=exit",
        )
        indices: List[int] = []
        while True:
            raw = _input(sel_prompt).strip()
            if raw == "0" or raw == "":
                print_warning("Cancelled. Returning to menu.")
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

        default_path = unique_file_path(source.parent / f"{source.stem}_nowatermark.pdf")
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
        print_kv("Output", out_path, Color.AQUA)
        logger.info(
            "Watermark removal chosen: candidates=%s pages=%d output='%s'",
            indices, len(affected_pages), out_path,
        )

        if not ask_yes_no("Remove the selected watermark(s)?", default_yes=True):
            print_warning("Cancelled. Returning to menu.")
            logger.info("Watermark removal cancelled at confirmation.")
            return

        try:
            modified = remove_watermark_images(
                reader,
                chosen_sigs,
                out_path,
                progress=lambda c, t: _print_progress("Cleaning pages", c, t),
            )
        except Exception as exc:  # noqa: BLE001 - clean message, log details
            print_error(f"Failed to remove the watermark: {exc}")
            logger.exception("Watermark removal failed for output '%s'", out_path)
            return

        print_success(
            f"Done. Removed watermark from {modified} page(s):\n  {out_path}"
        )
        logger.info(
            "Watermark removal complete: pages=%d output='%s'", modified, out_path
        )
    finally:
        # Remove the preview folder now that the operation is done, and drop the
        # temp parent too if it is now empty.
        shutil.rmtree(preview_dir, ignore_errors=True)
        try:
            temp = _temp_dir()
            if temp.exists() and not any(temp.iterdir()):
                temp.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Main menu loop
# --------------------------------------------------------------------------- #

def show_menu() -> None:
    """Render the main menu: light-blue header and numbered options."""
    print()
    print(colorize(f"{APP_NAME} Main menu:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} Page tools "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Merge multiple PDFs")
    print(f"  {colorize('3.', Color.LIGHT_BLUE)} PDF to images (PNG)")
    print(f"  {colorize('4.', Color.LIGHT_BLUE)} PDF to image-only PDF")
    print(f"  {colorize('5.', Color.LIGHT_BLUE)} Remove image watermark")
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
        # After completing an operation, loop back to the submenu.


def main_menu() -> int:
    """Run the interactive main menu loop. Returns a process exit code."""
    while True:
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
            print_success("Goodbye.")
            logger.info("Application exit requested by user.")
            return 0

        logger.debug("Main menu selection: '%s'", choice)
        try:
            if choice == "1":
                page_tools_menu()
            elif choice == "2":
                operation_merge_pdfs()
            elif choice == "3":
                pdf_to_images_menu()
            elif choice == "4":
                pdf_to_image_pdf_menu()
            elif choice == "5":
                operation_remove_watermark()
            else:
                print_error("Invalid option. Please choose 1-5 or 0.")
                continue
        except _ExitRequested:
            print_success("Goodbye.")
            logger.info("Application exit requested during operation.")
            return 0
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Operation interrupted by user (KeyboardInterrupt).")
        # After completing an operation, loop back to the main menu.


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Application entry point."""
    enable_ansi_colors()
    script_dir = Path(__file__).resolve().parent
    log_path = setup_logging(script_dir)

    logger.info("=== %s v%s starting ===", APP_NAME, APP_VERSION)
    logger.info("Python %s on %s", sys.version.split()[0], sys.platform)
    logger.info("Executable: %s", sys.executable)
    logger.info("Operating system: %s", os.name)
    logger.info("Script directory: %s", script_dir)
    logger.info("Working directory: %s", os.getcwd())
    if log_path is not None:
        logger.info("Log file: %s", log_path)
    else:
        logger.warning("Persistent file logging is unavailable; using console fallback.")

    # Clear the project-local temp folder (e.g. leftover preview images).
    cleanup_temp_dir()

    print_banner(APP_NAME)
    if log_path is not None:
        print_note(f"Logging to: {log_path}")

    # Verify the PDF backend early for a friendly message.
    try:
        _import_pypdf()
    except RuntimeError as exc:
        print_error(str(exc))
        logger.critical("pypdf import failed: %s", exc)
        return 2

    try:
        exit_code = main_menu()
    except KeyboardInterrupt:
        print_warning("\nInterrupted. Exiting.")
        logger.warning("Application interrupted at top level.")
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 - log any unexpected top-level error
        print_error(f"Unexpected error: {exc}")
        logger.exception("Unhandled top-level exception.")
        exit_code = 1
    finally:
        logger.info("=== %s shutting down (exit code %s) ===", APP_NAME, exit_code)
        logging.shutdown()

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
