"""Office / spreadsheet source-file logic for the convert-to-PDF tool.

Pure, native-runtime-free helpers: file-family detection, structure validation,
folder discovery, and CSV dialect sniffing. The actual headless conversion lives
in :mod:`pdf_forge.office_runtime` and :mod:`pdf_forge.ops_office`; keeping the
logic here means it is unit-testable without LibreOffice installed.
"""
from __future__ import annotations

import csv
import io
import itertools
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .constants import *  # noqa: F401,F403
from .core import natural_sort_key, _iter_dir

__all__ = [
    'OFFICE_FAMILIES', 'SUPPORTED_OFFICE_EXTS', 'classify_office_file',
    'is_office_lock_file', 'validate_office_file', 'discover_office_files',
    'family_counts', 'CsvDialect', 'detect_csv_dialect',
    'is_encrypted_office_file', 'is_encrypted_ooxml', 'normalize_csv_for_import',
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


def _open_ole(path: Path):
    """Open an OLE2 compound file with the maintained parser, or ``None``.

    Uses ``olefile`` rather than scanning raw bytes for markers: a marker can
    appear anywhere in ordinary data (false positive) and the real directory can
    live far past any fixed scan window (false negative).
    """
    import olefile

    try:
        if not olefile.isOleFile(str(path)):
            return None
        return olefile.OleFileIO(str(path))
    except Exception:  # noqa: BLE001 - malformed OLE is simply "not OLE"
        return None


def _ole_stream_names(path: Path):
    """Top-level stream names inside an OLE2 container (lower-cased), or None."""
    ole = _open_ole(path)
    if ole is None:
        return None
    try:
        return {"/".join(entry).lower() for entry in ole.listdir(streams=True)}
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            ole.close()
        except Exception:  # noqa: BLE001
            pass


def is_encrypted_ooxml(path: Path) -> bool:
    """True for a password-to-open OOXML file (an OLE2 "encrypted package").

    A protected .docx/.xlsx/.pptx is not a ZIP at all: it is an OLE2 compound
    file holding ``EncryptionInfo`` and ``EncryptedPackage`` streams. Both must
    be present, so an arbitrary OLE2 file (e.g. a legacy .doc) is not accepted.
    """
    names = _ole_stream_names(path)
    if names is None:
        return False
    return "encryptioninfo" in names and "encryptedpackage" in names


# Required marker part and folder root per OOXML family, so a cross-family
# rename (an .xlsx called .docx) is rejected instead of confusing the converter.
_OOXML_FAMILY_RULES = {
    "word": {
        "root": "word/",
        "main": "word/document.xml",
        "content_type": "wordprocessingml.document.main+xml",
    },
    "excel": {
        "root": "xl/",
        "main": "xl/workbook.xml",
        "content_type": "spreadsheetml.sheet.main+xml",
    },
    "powerpoint": {
        "root": "ppt/",
        "main": "ppt/presentation.xml",
        "content_type": "presentationml.presentation.main+xml",
    },
}

# Validation-time decompression guards: a package whose declared content
# expands beyond these limits is treated as hostile rather than read into memory.
_MAX_VALIDATION_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200


def _validate_plain_ooxml(path: Path, family: str) -> tuple:
    """Validate an unencrypted OOXML package and confirm its real family."""
    rules = _OOXML_FAMILY_RULES[family]
    root, main = rules["root"], rules["main"]
    try:
        with zipfile.ZipFile(str(path)) as zf:
            infos = zf.infolist()
            names = [i.filename for i in infos]

            # Reject path traversal and absolute members outright.
            for name in names:
                normalized = name.replace("\\", "/")
                if normalized.startswith("/") or ".." in normalized.split("/"):
                    return False, "unsafe entry path in package: " + repr(name)

            # ZIP-bomb guards: total declared size and per-entry ratio.
            total = sum(i.file_size for i in infos)
            if total > _MAX_VALIDATION_BYTES:
                return False, "package expands to an implausible size"
            for info in infos:
                if info.compress_size > 0 and info.file_size > (1 << 20):
                    if info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO:
                        return False, (
                            "package entry has an implausible compression ratio"
                        )

            lowered = [n.lower() for n in names]
            if "[content_types].xml" not in lowered:
                return False, "not a valid OOXML package ([Content_Types].xml missing)"
            if lowered.count("[content_types].xml") > 1:
                return False, "package declares [Content_Types].xml more than once"

            index = lowered.index("[content_types].xml")
            with zf.open(infos[index]) as handle:
                content_types = handle.read(4 * 1024 * 1024).decode(
                    "utf-8", errors="replace"
                ).lower()

            if rules["content_type"] not in content_types:
                actual = None
                for fam, other in _OOXML_FAMILY_RULES.items():
                    if other["content_type"] in content_types:
                        actual = fam
                        break
                if actual:
                    return False, (
                        "this is really " + actual + " content saved with a "
                        + family + " extension"
                    )
                return False, "not a " + family + " package (main content type missing)"

            if not any(n.lower().startswith(root) for n in names):
                return False, "not a " + family + " package (" + root + " missing)"
            if main not in lowered:
                return False, "not a " + family + " package (" + main + " missing)"
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        return False, "corrupt OOXML package: " + str(exc)
    return True, ""


def validate_office_file(path: Path,
                        expected_family: Optional[str] = None) -> tuple:
    """Confirm a file's real container matches its extension.

    Returns ``(ok, reason)``; ``reason`` is empty when ``ok`` is True.

    ``expected_family`` states the family the caller planned for. Pass it
    whenever the filename is not trustworthy evidence - notably for a
    package that has just been decrypted, where every encrypted container
    looked alike beforehand and only the (renameable) extension suggested a
    family. Without it the extension decides, as before.

    An OOXML extension is accepted in exactly two shapes:
      * a plain ZIP package whose content types and parts match the family, or
      * an OLE2 "encrypted package" (password-to-open), accepted here precisely
        so the conversion flow can *ask for the password* rather than rejecting
        the file before any prompt can happen.

    Anything else - a fake ZIP with only [Content_Types].xml, a cross-family
    rename, an arbitrary OLE2 file renamed .docx - is rejected.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in OFFICE_FAMILIES:
        return False, "unsupported extension " + repr(ext)
    try:
        if not path.is_file():
            return False, "not a regular file"
        size = path.stat().st_size
    except OSError as exc:
        return False, "cannot access file: " + str(exc)
    if size == 0:
        return False, "file is empty"

    family = OFFICE_FAMILIES[ext]
    if expected_family is not None and family != expected_family:
        return False, (
            f"extension {ext!r} is a {family} file but a "
            f"{expected_family} file was expected"
        )
    if expected_family is not None:
        family = expected_family

    if ext in _OOXML_EXTS:
        if zipfile.is_zipfile(str(path)):
            return _validate_plain_ooxml(path, family)
        if is_encrypted_ooxml(path):
            return True, ""  # valid but locked: the caller requests the password
        if _ole_stream_names(path) is not None:
            return False, (
                "this is an OLE2 file that is not an encrypted OOXML package "
                "(a legacy .doc/.xls/.ppt renamed to a modern extension?)"
            )
        return False, "not a valid OOXML package (expected a ZIP container)"

    if ext in _CFB_EXTS:
        try:
            with open(path, "rb") as handle:
                head = handle.read(8)
        except OSError as exc:
            return False, "cannot read file: " + str(exc)
        if head != _CFB_MAGIC:
            return False, "not a valid legacy Office file (bad signature)"
        return True, ""

    # CSV: must be text, not a binary file renamed to .csv.
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(4096)
    except OSError as exc:
        return False, "cannot read file: " + str(exc)
    if b"\x00" in chunk:
        return False, "not a text CSV (contains NUL bytes - looks binary)"
    return True, ""


def is_encrypted_office_file(path: Path) -> bool:
    """Detect an encrypted Office/ODF document *before* handing it to LibreOffice.

    Recognized forms:
      * OOXML with password-to-open - an OLE2 container whose *parsed* directory
        holds ``EncryptionInfo`` and ``EncryptedPackage`` (never byte-scanned, so
        a directory beyond any fixed offset is still found and stray marker bytes
        in ordinary data cannot cause a false positive);
      * ODF with password-to-open - a ZIP whose ``META-INF/manifest.xml``
        declares ``encryption-data``.
    """
    path = Path(path)
    if is_encrypted_ooxml(path):
        return True

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
    the sniffed delimiter/encoding and re-emitted canonically.

    Rows are streamed straight from the reader to the writer, so memory stays
    bounded no matter how many rows the file has (a whole-file ``list()`` used
    to scale with the input). Quoting, embedded delimiters, embedded newlines,
    and Unicode are preserved by the csv module. The *source is only read* -
    never modified - and the copy lives in a temporary location.
    """
    encoding = _python_encoding(dialect.encoding, dialect.had_bom)
    with open(path, "r", encoding=encoding, newline="") as source, \
            open(out_path, "w", encoding="utf-8", newline="") as target:
        reader = csv.reader(source, delimiter=dialect.delimiter)
        writer = csv.writer(target, delimiter=",")
        for row in reader:
            writer.writerow(row)
    return out_path


def _python_encoding(label: str, had_bom: bool = False) -> str:
    if label == "UTF-8" and had_bom:
        # A BOM is an encoding marker, never cell data. Plain "utf-8" reads it
        # back as U+FEFF glued to the front of the first field, which also hides
        # that field's opening quote from csv.reader.
        return "utf-8-sig"
    # "utf-16" consumes its own BOM already.
    return {"UTF-8": "utf-8", "windows-1252": "cp1252",
            "UTF-16": "utf-16"}.get(label, "utf-8")


def discover_office_files(folder: Path) -> List[Path]:
    """Return supported office files directly inside ``folder`` (non-recursive).

    Office/LibreOffice lock files (``~$*``) are ignored. Results are in natural,
    case-insensitive, stable order (see :func:`natural_sort_key`).
    """
    folder = Path(folder)
    files = [
        entry
        for entry in _iter_dir(folder)
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
    had_bom: bool = False             # so normalization can strip it, not read it

    @property
    def delimiter_label(self) -> str:
        return {
            ",": "comma", ";": "semicolon", "\t": "tab", ":": "colon", " ": "space",
        }.get(self.delimiter, repr(self.delimiter))


def _decode_csv_sample(raw: bytes, complete: bool):
    """Decode a CSV *sample* to text without misreading a split code point.

    A fixed-size read can land in the middle of a multi-byte UTF-8 sequence.
    Decoding that strictly raises ``UnicodeDecodeError``, which previously made
    a perfectly valid UTF-8 file look like Windows-1252 and corrupted every
    non-ASCII character. An incremental decoder distinguishes the two cases:
    an *incomplete* trailing sequence is simply held back, while genuinely
    invalid bytes still fail and fall back.

    Returns ``(text, encoding, had_bom)``. ``complete`` says whether ``raw`` is
    the entire file (then a trailing partial sequence really is invalid).
    """
    import codecs

    had_bom = False
    encoding = "UTF-8"
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
        had_bom = True
    elif raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        # The BOM selects the byte order. Decode incrementally for the same
        # reason as UTF-8 below: a sample boundary can fall inside a code unit
        # or between the two halves of a surrogate pair, and a one-shot decode
        # rejected that - demoting a perfectly valid UTF-16 file all the way to
        # windows-1252 mojibake.
        try:
            text = codecs.getincrementaldecoder("utf-16")(errors="strict").decode(
                raw, complete)
            return text, "UTF-16", True
        except UnicodeDecodeError:
            pass

    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        # final=complete: mid-code-point tails are buffered, not treated as
        # invalid, unless this really is the end of the file.
        text = decoder.decode(raw, complete)
        return text, encoding, had_bom
    except UnicodeDecodeError:
        # Genuinely not UTF-8. cp1252 leaves five bytes undefined - 0x81, 0x8D,
        # 0x8F, 0x90, 0x9D - which appear routinely in CP437/CP850 DOS exports,
        # so this decode very much can raise; the previous comment asserting it
        # could not was simply wrong, and the UnicodeDecodeError (a ValueError,
        # not an OSError) escaped every caller and exited the application.
        return raw.decode("cp1252", errors="replace"), "windows-1252", had_bom


def detect_csv_dialect(path: Path, sample_bytes: int = 65536) -> CsvDialect:
    """Sniff a CSV file's encoding, delimiter, and header row.

    Detects: BOM; strict UTF-8 first, then a deterministic fallback; comma /
    semicolon / tab / colon / space delimiters (honouring quoted fields and
    multiline quoted values); and a likely header row. Sets ``confidence`` to
    ``"low"`` when the delimiter is ambiguous so the caller can offer a single
    correction prompt instead of guessing silently.
    """
    file_size = path.stat().st_size
    with open(path, "rb") as handle:
        raw = handle.read(sample_bytes)
    text, encoding, had_bom = _decode_csv_sample(raw, complete=len(raw) >= file_size)
    notes: List[str] = []
    if had_bom:
        notes.append("BOM detected")

    delimiter = ","
    confidence = "high"
    has_header = False

    # csv.Sniffer is treated as ONE signal, not as truth: it happily returns a
    # delimiter that shreds rows into ragged widths. Score every candidate on
    # row-width consistency first, and only accept the sniffer when the scoring
    # agrees with it.
    delimiter, confidence = _score_delimiter(text)
    sniffed = None
    try:
        sniffed = csv.Sniffer().sniff(
            text, delimiters="".join(_CSV_DELIMITERS)
        ).delimiter
    except (csv.Error, ValueError):
        # ValueError: the sniffer can infer a multi-character delimiter on
        # ragged input, which csv rejects. Either way it simply could not decide.
        notes.append("csv.Sniffer could not decide")
    if sniffed is not None and sniffed != delimiter:
        if confidence == "low":
            delimiter = sniffed  # scoring had nothing better to offer
        else:
            notes.append(
                "csv.Sniffer suggested " + repr(sniffed) + "; kept the more "
                "consistent " + repr(delimiter)
            )
    try:
        has_header = csv.Sniffer().has_header(text)
    except (csv.Error, ValueError):
        has_header = _guess_header(text, delimiter)

    return CsvDialect(
        encoding=encoding,
        delimiter=delimiter,
        has_header=has_header,
        confidence=confidence,
        notes=notes,
        had_bom=had_bom,
    )


def _score_delimiter(text: str) -> tuple:
    """Choose the delimiter with the most *consistent* row shape.

    Returns ``(delimiter, confidence)``. Consistency dominates column count:
    prose containing commas can yield more columns than the true delimiter while
    producing wildly ragged rows, which previously won on column count alone.

    Penalties: width variance, empty-column explosions, implausibly wide rows,
    and single-row samples (which carry almost no evidence). ``confidence`` is
    ``"low"`` when nothing scores convincingly, so the caller asks instead of
    silently picking.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()][:50]
    if not lines:
        return ",", "low"
    sample = "\n".join(lines)

    best = (",", -1.0, "low")
    for delim in _CSV_DELIMITERS:
        try:
            rows = [r for r in csv.reader(io.StringIO(sample), delimiter=delim) if r]
        except (csv.Error, ValueError):
            continue
        if not rows:
            continue
        widths = [len(r) for r in rows]
        modal = max(set(widths), key=widths.count)
        if modal < 2:
            continue  # not actually splitting anything
        consistency = widths.count(modal) / len(widths)
        # Variance penalty: how far off the ragged rows are.
        spread = (max(widths) - min(widths)) / modal
        # Empty-cell penalty: a wrong delimiter shreds text into blanks.
        cells = [c for row in rows for c in row]
        empties = sum(1 for c in cells if not c.strip()) / max(1, len(cells))
        score = consistency - 0.5 * min(spread, 1.0) - 0.5 * empties
        if modal > 64:
            score -= 0.5           # implausibly wide for a real sheet
        if len(rows) < 2:
            score -= 0.25          # one row proves very little
        if score > best[1]:
            confident = (
                consistency >= 0.9 and empties < 0.4 and spread <= 0.25
                and len(rows) >= 2
            )
            best = (delim, score, "high" if confident else "low")
    if best[1] < 0:
        return ",", "low"
    return best[0], best[2]


def _guess_header(text: str, delimiter: str) -> bool:
    """Heuristic: first row is a header when it is all-non-numeric and the
    second row contains at least one numeric cell."""
    try:
        # Only the first two rows matter; do not build the whole list.
        rows = list(itertools.islice(
            csv.reader(io.StringIO(text), delimiter=delimiter), 2
        ))
    except (csv.Error, ValueError):
        return False
    rows = [r for r in rows if r]
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
