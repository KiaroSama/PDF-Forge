from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from .constants import *  # noqa: F401,F403
from .safeio import load_generated_outputs

__all__ = ['_sanitize_for_filename', 'PageSelectionError', 'ChunkSizeError', 'PageSelectionResult', 'parse_page_selection', 'PageGroup', 'parse_multi_file_selection', 'parse_delete_pages', 'compute_deletion', 'build_delete_output_name', 'compute_chunks', 'parse_page_number', 'parse_chunk_size', 'parse_index_list', 'sanitize_selection_text', 'build_extract_output_name', 'build_chunk_output_name', 'pad_width_for', 'build_page_image_name', 'default_images_output_dir', 'default_image_pdf_output', 'unique_file_path', 'unique_dir_path', 'strip_surrounding_quotes', 'natural_sort_key', 'discover_pdfs_in_folder', 'FolderScanError', 'summarize_ranges', 'GUIDANCE_KEYWORDS', 'drag_drop_guidance', 'BATCH_PASSWORD_NOTICE', 'normalized_path_key', 'reserve_unique_file', 'reserve_unique_dir', 'release_reservations', 'clear_reservations',
]

# Command keywords picked out in the guidance (see ui.guidance_text): the
# highlighted parts are the things you can actually type.
GUIDANCE_KEYWORDS = ("b=", "done")


# Shown before queueing any batch that opens its files at run time. Single-file
# operations authenticate during configuration and never prompt mid-run; batch
# operations cannot know which files are encrypted until they open them, so the
# behaviour is disclosed up front instead (A13).
BATCH_PASSWORD_NOTICE = (
    "Encrypted files will ask for their password while the queue runs "
    "(unlimited attempts; 0/back/skip skips just that file and the batch "
    "continues)."
)


def drag_drop_guidance(kind: str = "file", repeated: bool = False) -> str:
    """Return the constant drag-and-drop / paste-path guidance string.

    ``kind`` is ``"file"`` or ``"folder"``. ``repeated=True`` is for prompts
    that collect several files in a row, which additionally offer ``b`` (go
    back and re-enter the previous file) and ``done`` (finish)::

        drag and drop a file here or paste a path; b=re-enter previous file; type done when finished

    A single-file or folder prompt gets the short form, because there is no
    previous file to re-enter and nothing to finish::

        drag and drop a folder here or paste a path
    """
    base = f"drag and drop a {kind} here or paste a path"
    if not repeated:
        return base
    return f"{base}; b=re-enter previous file; type done when finished"


def _sanitize_for_filename(name: str) -> str:
    """Remove characters that are unsafe in a filename."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()


class FolderScanError(OSError):
    """Raised when a folder cannot be listed (permissions, removed, I/O)."""


def _iter_dir(folder: Path):
    """List a directory, turning every OS failure into :class:`FolderScanError`.

    Discovery must never return a silently partial result or crash the CLI: the
    caller reports the reason and stays in the current menu.
    """
    try:
        return list(folder.iterdir())
    except PermissionError as exc:
        raise FolderScanError(
            f"Permission denied reading '{folder}'."
        ) from exc
    except FileNotFoundError as exc:
        raise FolderScanError(
            f"The folder '{folder}' no longer exists."
        ) from exc
    except NotADirectoryError as exc:
        raise FolderScanError(f"'{folder}' is not a folder.") from exc
    except OSError as exc:
        raise FolderScanError(f"Could not read '{folder}': {exc}") from exc


class PageSelectionError(ValueError):
    """Raised when a page-selection expression is invalid."""


class ChunkSizeError(ValueError):
    """Raised when a chunk size value is invalid."""


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


# Absolute ceiling on any single page number / range end when the real
# document length is not yet known (e.g. batch mode). No real PDF approaches
# this; it exists only to stop a pathological range like "1-999999999" from
# eagerly materializing billions of integers and exhausting memory.
_MAX_DELETE_PAGE = 1_000_000


def parse_delete_pages(expression: str, max_page: Optional[int] = None) -> List[int]:
    """Parse a page expression into a sorted, de-duplicated list of page numbers.

    Supports the same forms as extraction (``5``, ``1,2``, ``10-20``,
    ``10-20,25,30-50``).

    Bounds (both enforced *before* a range is expanded, so an unbounded range
    can never materialize an oversized list):
        * When ``max_page`` is given (the real document length, or a safe upper
          bound derived from a folder), any page or range end above it is
          rejected with a clear message.
        * Otherwise a hard sanity ceiling (:data:`_MAX_DELETE_PAGE`) applies, so
          ``"1-999999999"`` is rejected instead of exhausting memory.

    Raises:
        PageSelectionError: with a clear, user-facing message on any problem.
    """
    if expression is None or expression.strip() == "":
        raise PageSelectionError("The page selection is empty.")

    ceiling = max_page if max_page is not None else _MAX_DELETE_PAGE

    def _reject_too_large(value: int, element: str) -> None:
        if value > ceiling:
            if max_page is not None:
                raise PageSelectionError(
                    f"Page {value} in '{element}' exceeds the document length "
                    f"({max_page} pages)."
                )
            raise PageSelectionError(
                f"Page number {value} in '{element}' is unreasonably large "
                f"(over {ceiling}). Enter a realistic page number."
            )

    pages: set = set()
    for raw_element in expression.split(","):
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
            # Validate the bound BEFORE expanding, so a huge end never allocates.
            _reject_too_large(end, element)
            pages.update(range(start, end + 1))
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
            _reject_too_large(page, element)
            pages.add(page)
    return sorted(pages)


def compute_deletion(total_pages: int, requested_pages: Sequence[int]):
    """Split a deletion request against a document of ``total_pages`` pages.

    Returns ``(present, missing, kept_zero_based)`` where:
        * ``present``  - requested pages that exist (1-based, sorted)
        * ``missing``  - requested pages beyond the document (1-based, sorted)
        * ``kept_zero_based`` - 0-based indices to keep, in original order
    """
    requested = sorted(set(requested_pages))
    present = [p for p in requested if 1 <= p <= total_pages]
    missing = [p for p in requested if p > total_pages]
    to_delete = set(present)
    kept_zero_based = [i for i in range(total_pages) if (i + 1) not in to_delete]
    return present, missing, kept_zero_based


def build_delete_output_name(source_stem: str, selection_text: str,
                             max_stem_length: int = 120) -> str:
    """Build a length-safe output filename for a page-deletion result."""
    fragment = sanitize_selection_text(selection_text)
    descriptive = f"{source_stem}_deleted_{fragment}.pdf"
    if len(descriptive) <= max_stem_length:
        return descriptive
    return f"{source_stem}_pages_deleted.pdf"


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


# --------------------------------------------------------------------------- #
# Queue-time output-path reservation
#
# Output paths are chosen while operations are *configured*, but written later
# when the batch queue runs. Two operations configured with the same brand-new
# default path would each pass the on-disk uniqueness check (neither exists yet)
# and then collide at run time. The reservation registry closes that gap: a
# chosen path is reserved immediately, so the next operation's uniqueness check
# sees it and picks a different name. Files and directories are tracked
# separately; keys are normalized (case-insensitive on Windows).
# --------------------------------------------------------------------------- #

_reserved_files: Set[str] = set()
_reserved_dirs: Set[str] = set()


def normalized_path_key(path) -> str:
    """Absolute, case-folded-on-Windows key for reservation comparison.

    Uses ``abspath`` (not ``realpath``) so a not-yet-created output path still
    yields a stable key without touching the filesystem.
    """
    key = os.path.abspath(str(path))
    return key.lower() if os.name == "nt" else key


def _file_reserved_or_exists(path: Path) -> bool:
    return path.exists() or normalized_path_key(path) in _reserved_files


def _dir_reserved_or_exists(path: Path) -> bool:
    return path.exists() or normalized_path_key(path) in _reserved_dirs


def reserve_unique_file(path: Path) -> Path:
    """Pick a file path free on disk **and** unreserved, then reserve it.

    Adds ``_2``, ``_3`` suffixes as needed. Reserving and returning is a single
    synchronous step (no yield in between), so two operations can never receive
    the same path. Release with :func:`release_reservations` once the queue is
    done or discarded.
    """
    path = Path(path)
    candidate = path
    if _file_reserved_or_exists(candidate):
        stem, suffix, parent = path.stem, path.suffix, path.parent
        counter = 2
        candidate = parent / f"{stem}_{counter}{suffix}"
        while _file_reserved_or_exists(candidate):
            counter += 1
            candidate = parent / f"{stem}_{counter}{suffix}"
    _reserved_files.add(normalized_path_key(candidate))
    return candidate


def reserve_unique_dir(path: Path) -> Path:
    """Pick a directory path free on disk **and** unreserved, then reserve it."""
    path = Path(path)
    candidate = path
    if _dir_reserved_or_exists(candidate):
        name, parent = path.name, path.parent
        counter = 2
        candidate = parent / f"{name}_{counter}"
        while _dir_reserved_or_exists(candidate):
            counter += 1
            candidate = parent / f"{name}_{counter}"
    _reserved_dirs.add(normalized_path_key(candidate))
    return candidate


def release_reservations(files: Iterable[Path] = (), dirs: Iterable[Path] = ()) -> None:
    """Release specific file/directory reservations (safe if not reserved)."""
    for f in files:
        _reserved_files.discard(normalized_path_key(f))
    for d in dirs:
        _reserved_dirs.discard(normalized_path_key(d))


def clear_reservations() -> None:
    """Drop every reservation (called when the queue finishes or is discarded)."""
    _reserved_files.clear()
    _reserved_dirs.clear()


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


# Generated-output tracking now lives in pdf_forge.safeio (a locked, atomic,
# per-user state store). Folder discovery consumes it through the import below.
# --------------------------------------------------------------------------- #

def discover_pdfs_in_folder(folder: Path, include_generated: bool = False) -> List[Path]:
    """Return the ``*.pdf`` files directly inside ``folder`` (non-recursive).

    The result is sorted by file name using a natural, case-insensitive, stable
    order (see :func:`natural_sort_key`) so files such as ``1.pdf``, ``2.pdf``,
    and ``10.pdf`` are ordered 1, 2, 10. Only regular files with a ``.pdf``
    suffix are returned; subdirectories are not traversed.

    PDFs this application generated in an earlier run are skipped by exact path
    (see the manifest above), so running a folder tool twice never reprocesses
    its own output. Pass ``include_generated=True`` to bypass that.
    """
    folder = Path(folder)
    generated = set() if include_generated else load_generated_outputs()
    pdfs = []
    skipped = 0
    for entry in _iter_dir(folder):
        if not entry.is_file() or entry.suffix.lower() != ".pdf":
            continue
        if generated and normalized_path_key(entry) in generated:
            skipped += 1
            continue
        pdfs.append(entry)
    # Natural, case-insensitive, stable ordering (1, 2, 10 -> not 1, 10, 2).
    pdfs.sort(key=lambda p: natural_sort_key(p.name))
    logger.debug(
        "Discovered %d PDF(s) in folder '%s' (natural case-insensitive order); "
        "skipped %d previously generated output(s).",
        len(pdfs), folder, skipped,
    )
    return pdfs


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
