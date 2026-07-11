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

__all__ = ['_sanitize_for_filename', 'PageSelectionError', 'ChunkSizeError', 'PageSelectionResult', 'parse_page_selection', 'PageGroup', 'parse_multi_file_selection', 'parse_delete_pages', 'compute_deletion', 'build_delete_output_name', 'compute_chunks', 'parse_page_number', 'parse_chunk_size', 'parse_index_list', 'sanitize_selection_text', 'build_extract_output_name', 'build_chunk_output_name', 'pad_width_for', 'image_dpi_for_quality', 'build_page_image_name', 'default_images_output_dir', 'default_image_pdf_output', 'unique_file_path', 'unique_dir_path', 'strip_surrounding_quotes', 'natural_sort_key', 'discover_pdfs_in_folder', 'summarize_ranges']

def _sanitize_for_filename(name: str) -> str:
    """Remove characters that are unsafe in a filename."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()


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


def parse_delete_pages(expression: str) -> List[int]:
    """Parse a page expression into a sorted, de-duplicated list of page numbers.

    Supports the same forms as extraction (``5``, ``1,2``, ``10-20``,
    ``10-20,25,30-50``) but applies **no upper bound**: the document length is
    checked later, per file, so a batch can target pages that exist in some
    files and not others.

    Raises:
        PageSelectionError: with a clear, user-facing message on any problem.
    """
    if expression is None or expression.strip() == "":
        raise PageSelectionError("The page selection is empty.")

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
