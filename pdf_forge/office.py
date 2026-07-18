"""Office / spreadsheet source-file logic for the convert-to-PDF tool.

Pure, native-runtime-free helpers: file-family detection, structure validation,
folder discovery, and CSV dialect sniffing. The actual headless conversion lives
in :mod:`pdf_forge.office_runtime` and :mod:`pdf_forge.ops_office`; keeping the
logic here means it is unit-testable without LibreOffice installed.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .constants import *  # noqa: F401,F403
from .core import natural_sort_key

__all__ = [
    'OFFICE_FAMILIES', 'SUPPORTED_OFFICE_EXTS', 'classify_office_file',
    'is_office_lock_file', 'validate_office_file', 'discover_office_files',
    'family_counts', 'CsvDialect', 'detect_csv_dialect',
    'is_encrypted_office_file', 'normalize_csv_for_import',
]

# extension -> human family label. Detection is by extension; validation below
# confirms the real container structure so a renamed file is rejected.
OFFICE_FAMILIES: Dict[str, str] = {
    ".doc": "word", ".docx": "word",
    ".ppt": "powerpoint", ".pptx": "powerpoint",
    ".xls": "excel", ".xlsx": "excel",
    ".csv": "csv",
}

SUPPORTED_OFFICE_EXTS = tuple(sorted(OFFICE_FAMILIES))

# Modern OOXML packages are ZIP archives; legacy binary formats are OLE2
# Compound File Binary (CFB) documents with this signature.
_OOXML_EXTS = {".docx", ".pptx", ".xlsx"}
_CFB_EXTS = {".doc", ".ppt", ".xls"}
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# CSV delimiters we sniff for, in preference order (comma first).
_CSV_DELIMITERS = [",", ";", "\t", ":", " "]


def classify_office_file(path: Path) -> Optional[str]:
    """Return the family label for a supported extension, or ``None``.

    ``"word"``, ``"powerpoint"``, ``"excel"``, ``"csv"`` - by extension only,
    case-insensitive. Use :func:`validate_office_file` to confirm the contents.
    """
    return OFFICE_FAMILIES.get(Path(path).suffix.lower())


def is_office_lock_file(name: str) -> bool:
    """True for LibreOffice/Office lock files like ``~$name.docx``.

    These are not real documents and must be skipped during folder discovery.
    """
    return name.startswith("~$")


def validate_office_file(path: Path) -> tuple:
    """Confirm a file's real container matches its extension.

    Returns ``(ok, reason)``. ``reason`` is empty when ``ok`` is True.
    Rejects a binary file renamed to ``.csv`` and an OOXML/CFB file whose magic
    bytes do not match its claimed format, so a corrupt or mislabelled input
    fails per-file instead of crashing the converter.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in OFFICE_FAMILIES:
        return False, f"unsupported extension '{ext}'"
    try:
        if not path.is_file():
            return False, "not a regular file"
        size = path.stat().st_size
    except OSError as exc:
        return False, f"cannot access file: {exc}"
    if size == 0:
        return False, "file is empty"

    if ext in _OOXML_EXTS:
        if not zipfile.is_zipfile(str(path)):
            return False, "not a valid OOXML package (expected a ZIP container)"
        try:
            with zipfile.ZipFile(str(path)) as zf:
                names = zf.namelist()
        except (zipfile.BadZipFile, OSError) as exc:
            return False, f"corrupt OOXML package: {exc}"
        if "[Content_Types].xml" not in names:
            return False, "not a valid OOXML package ([Content_Types].xml missing)"
        return True, ""

    if ext in _CFB_EXTS:
        try:
            with open(path, "rb") as handle:
                head = handle.read(8)
        except OSError as exc:
            return False, f"cannot read file: {exc}"
        if head != _CFB_MAGIC:
            return False, "not a valid legacy Office file (bad signature)"
        return True, ""

    # CSV: must be text, not a binary file renamed to .csv.
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(4096)
    except OSError as exc:
        return False, f"cannot read file: {exc}"
    if b"\x00" in chunk:
        return False, "not a text CSV (contains NUL bytes - looks binary)"
    return True, ""


def is_encrypted_office_file(path: Path) -> bool:
    """Detect an encrypted Office/ODF document *before* handing it to LibreOffice.

    Detecting this up front means the password can be requested before the
    conversion is attempted. It also avoids relying on the converter's error
    path, which reports a password failure as an opaque marshalling error and
    can tear down the UNO bridge.

    Two real-world forms are recognized:
      * OOXML with password-to-open - the file is no longer a ZIP but an OLE2
        compound document holding an ``EncryptedPackage`` stream;
      * ODF with password-to-open - a ZIP whose ``META-INF/manifest.xml``
        declares ``encryption-data`` for its entries.
    """
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
            if head == _CFB_MAGIC:
                handle.seek(0)
                # The stream name is stored UTF-16LE in the CFB directory.
                blob = handle.read(1 << 20)
                return b"E\x00n\x00c\x00r\x00y\x00p\x00t\x00e\x00d\x00" in blob
    except OSError:
        return False

    if not zipfile.is_zipfile(str(path)):
        return False
    try:
        with zipfile.ZipFile(str(path)) as zf:
            if "META-INF/manifest.xml" not in zf.namelist():
                return False
            manifest = zf.read("META-INF/manifest.xml")
    except (zipfile.BadZipFile, OSError, KeyError):
        return False
    return b"encryption-data" in manifest


def normalize_csv_for_import(path: Path, dialect: "CsvDialect", out_path: Path) -> Path:
    """Write a canonical UTF-8, comma-separated copy of ``path``.

    LibreOffice's import-filter options cannot be passed through the converter
    API, so the detected dialect is applied here instead: the CSV is parsed with
    the sniffed delimiter/encoding and re-emitted canonically. The *source file
    is only read* - never modified - and the copy lives in a temporary location.
    """
    with open(path, "r", encoding=_python_encoding(dialect.encoding),
              newline="") as handle:
        rows = list(csv.reader(handle, delimiter=dialect.delimiter))
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, delimiter=",").writerows(rows)
    return out_path


def _python_encoding(label: str) -> str:
    return {"UTF-8": "utf-8", "windows-1252": "cp1252"}.get(label, "utf-8")


def discover_office_files(folder: Path) -> List[Path]:
    """Return supported office files directly inside ``folder`` (non-recursive).

    Office/LibreOffice lock files (``~$*``) are ignored. Results are in natural,
    case-insensitive, stable order (see :func:`natural_sort_key`).
    """
    folder = Path(folder)
    files = [
        entry
        for entry in folder.iterdir()
        if entry.is_file()
        and entry.suffix.lower() in OFFICE_FAMILIES
        and not is_office_lock_file(entry.name)
    ]
    files.sort(key=lambda p: natural_sort_key(p.name))
    return files


def family_counts(paths) -> Dict[str, int]:
    """Count files per family: ``{word, powerpoint, excel, csv}``."""
    counts = {"word": 0, "powerpoint": 0, "excel": 0, "csv": 0}
    for p in paths:
        fam = classify_office_file(p)
        if fam in counts:
            counts[fam] += 1
    return counts


@dataclass
class CsvDialect:
    """Sniffed CSV import properties passed to the LibreOffice import filter."""

    encoding: str
    delimiter: str
    has_header: bool
    confidence: str = "high"          # "high" | "low"
    notes: List[str] = field(default_factory=list)

    @property
    def delimiter_label(self) -> str:
        return {
            ",": "comma", ";": "semicolon", "\t": "tab", ":": "colon", " ": "space",
        }.get(self.delimiter, repr(self.delimiter))


def _decode_csv_bytes(raw: bytes):
    """Return ``(text, encoding, had_bom)`` decoding CSV bytes safely.

    UTF-8 (with or without BOM) is tried strictly first; a deterministic
    single-byte fallback (cp1252) is used only when strict UTF-8 fails, so a
    valid UTF-8 file is never mis-decoded.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "UTF-8", True
    try:
        return raw.decode("utf-8"), "UTF-8", False
    except UnicodeDecodeError:
        # Deterministic fallback; cp1252 maps every byte, so this never raises.
        return raw.decode("cp1252"), "windows-1252", False


def detect_csv_dialect(path: Path, sample_bytes: int = 65536) -> CsvDialect:
    """Sniff a CSV file's encoding, delimiter, and header row.

    Detects: BOM; strict UTF-8 first, then a deterministic fallback; comma /
    semicolon / tab / colon / space delimiters (honouring quoted fields and
    multiline quoted values); and a likely header row. Sets ``confidence`` to
    ``"low"`` when the delimiter is ambiguous so the caller can offer a single
    correction prompt instead of guessing silently.
    """
    with open(path, "rb") as handle:
        raw = handle.read(sample_bytes)
    text, encoding, had_bom = _decode_csv_bytes(raw)
    notes: List[str] = []
    if had_bom:
        notes.append("BOM detected")

    delimiter = ","
    confidence = "high"
    has_header = False

    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(text, delimiters="".join(_CSV_DELIMITERS))
        delimiter = dialect.delimiter
        try:
            has_header = sniffer.has_header(text)
        except csv.Error:
            has_header = _guess_header(text, delimiter)
    except csv.Error:
        # Sniffer failed: fall back to a per-delimiter field-count heuristic.
        delimiter, confidence = _guess_delimiter(text)
        has_header = _guess_header(text, delimiter)
        notes.append("delimiter guessed by field-count heuristic")

    return CsvDialect(
        encoding=encoding,
        delimiter=delimiter,
        has_header=has_header,
        confidence=confidence,
        notes=notes,
    )


def _guess_delimiter(text: str) -> tuple:
    """Pick the delimiter giving the most consistent multi-field split.

    Returns ``(delimiter, confidence)``. Confidence is ``"low"`` when no
    delimiter produces a consistent multi-column shape.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()][:20]
    if not lines:
        return ",", "low"
    best = (",", 0, 0.0)  # (delimiter, columns, consistency)
    for delim in _CSV_DELIMITERS:
        try:
            rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
        except csv.Error:
            continue
        counts = [len(r) for r in rows if r]
        if not counts:
            continue
        cols = max(counts)
        if cols < 2:
            continue
        consistency = sum(1 for c in counts if c == cols) / len(counts)
        if (cols, consistency) > (best[1], best[2]):
            best = (delim, cols, consistency)
    if best[1] < 2:
        return ",", "low"
    return best[0], ("high" if best[2] >= 0.8 else "low")


def _guess_header(text: str, delimiter: str) -> bool:
    """Heuristic: first row is a header when it is all-non-numeric and the
    second row contains at least one numeric cell."""
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return False
    rows = [r for r in rows if r][:2]
    if len(rows) < 2:
        return False
    first, second = rows[0], rows[1]

    def _numeric(cell: str) -> bool:
        cell = cell.strip().replace(",", "")
        if not cell:
            return False
        try:
            float(cell)
            return True
        except ValueError:
            return False

    first_numeric = any(_numeric(c) for c in first)
    second_numeric = any(_numeric(c) for c in second)
    return (not first_numeric) and second_numeric
