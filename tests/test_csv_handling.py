# -*- coding: utf-8 -*-
"""Regression tests for CSV detection, normalization and cleanup.

Covers PF-010 (a sample cut mid-code-point must not turn valid UTF-8 into
Windows-1252), PF-011 (normalization streams instead of materializing the file),
PF-021 (delimiter scoring ranks consistency before column count) and PF-022
(temporary directories never leak).
"""

import codecs
import csv
import hashlib
import io
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from helpers import file_hash as digest  # noqa: E402

SAMPLE_LIMIT = 65536


# --------------------------------------------------------------------------- #
# PF-010 - boundary-safe decoding
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label,char", [
    ("e_acute", "é"),        # 2-byte
    ("persian", "س"),        # 2-byte
    ("cjk", "漢"),           # 3-byte
    ("emoji", "😀"),         # 4-byte
])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_multibyte_char_split_across_the_sample_limit(tmp_path, label, char, offset):
    """The character straddles the read boundary at several alignments."""
    nbytes = len(char.encode("utf-8"))
    pad = SAMPLE_LIMIT - nbytes + 1 + offset
    path = tmp_path / f"{label}{offset}.csv"
    path.write_bytes(("a" * pad + char + ",b\nx,y\n").encode("utf-8"))

    dialect = app.detect_csv_dialect(path)
    assert dialect.encoding == "UTF-8", (
        f"valid UTF-8 misread as {dialect.encoding} at a split code point"
    )


def test_genuine_windows_1252_is_still_detected(tmp_path):
    path = tmp_path / "cp1252.csv"
    # 0x92 (curly apostrophe) is invalid UTF-8 but valid cp1252.
    path.write_bytes(b"name,note\nann,it\x92s fine\nbob,ok\n")
    assert app.detect_csv_dialect(path).encoding == "windows-1252"


def test_invalid_utf8_falls_back_not_crashes(tmp_path):
    path = tmp_path / "invalid.csv"
    path.write_bytes(b"a,b\n\xff\xfe\xfa,2\n")
    dialect = app.detect_csv_dialect(path)
    assert dialect.encoding in ("windows-1252", "UTF-8")


def test_utf8_bom_is_handled(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf" + "naïve,b\n1,2\n".encode("utf-8"))
    dialect = app.detect_csv_dialect(path)
    assert dialect.encoding == "UTF-8"
    assert any("BOM" in n for n in dialect.notes)


def test_utf16_bom_is_recognised(tmp_path):
    path = tmp_path / "u16.csv"
    path.write_bytes("naïve,b\n1,2\n".encode("utf-16"))
    assert app.detect_csv_dialect(path).encoding == "UTF-16"


# --------------------------------------------------------------------------- #
# C-15 - the sample boundary and the BOM must never corrupt the data
# --------------------------------------------------------------------------- #

SAMPLE_UNITS = SAMPLE_LIMIT // 2          # UTF-16 code units inside the sample
_BOMS = {"le": codecs.BOM_UTF16_LE, "be": codecs.BOM_UTF16_BE}
_CODECS = {"le": "utf-16-le", "be": "utf-16-be"}


def _utf16_text(units_before_tail: int, tail: str) -> str:
    """Build CSV text whose first ``units_before_tail`` UTF-16 units are BMP.

    ``tail`` then starts exactly at that code-unit offset, so the caller can put
    a surrogate pair (or an ordinary character) astride the sample boundary.
    """
    text = "name,note\n"
    while len(text) < units_before_tail - 32:
        text += "ann,filler text\n"
    pad = units_before_tail - len(text)
    assert pad >= 5, "padding cell does not fit"
    text += "zzz," + "x" * (pad - 4)
    assert len(text) == units_before_tail
    return text + tail + "\nbob,end\n"


def _write_utf16(path: Path, text: str, order: str) -> None:
    path.write_bytes(_BOMS[order] + text.encode(_CODECS[order]))


def _logical_rows(text: str) -> list:
    return list(csv.reader(io.StringIO(text, newline="")))


def _normalized_rows(tmp_path: Path, src: Path, dialect) -> list:
    out = tmp_path / (src.stem + "_norm.csv")
    app.normalize_csv_for_import(src, dialect, out)
    with open(out, "r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


@pytest.mark.parametrize("order", ["le", "be"])
def test_utf16_surrogate_pair_split_by_the_sample_boundary(tmp_path, order):
    """The 65536-byte sample ends between the two halves of a surrogate pair."""
    # The BOM is code unit 0, so the last text unit inside the sample is index
    # SAMPLE_UNITS - 2: put the pair's high half there and its low half past
    # the boundary.
    text = _utf16_text(SAMPLE_UNITS - 2, "\U0001F600")
    src = tmp_path / f"surrogate_{order}.csv"
    _write_utf16(src, text, order)
    assert src.stat().st_size > SAMPLE_LIMIT

    dialect = app.detect_csv_dialect(src)
    assert dialect.encoding == "UTF-16", (
        f"valid UTF-16{order.upper()} misread as {dialect.encoding} at a split "
        "surrogate pair"
    )
    assert _normalized_rows(tmp_path, src, dialect) == _logical_rows(text)


@pytest.mark.parametrize("order", ["le", "be"])
def test_utf16_ordinary_boundary_split(tmp_path, order):
    """A plain BMP file larger than the sample must still read as UTF-16."""
    text = _utf16_text(SAMPLE_UNITS + 40, "سلام")
    src = tmp_path / f"plain_{order}.csv"
    _write_utf16(src, text, order)
    assert src.stat().st_size > SAMPLE_LIMIT

    dialect = app.detect_csv_dialect(src)
    assert dialect.encoding == "UTF-16"
    assert _normalized_rows(tmp_path, src, dialect) == _logical_rows(text)


def test_utf8_bom_with_a_quoted_first_field(tmp_path):
    """The BOM is an encoding marker, never part of the first cell."""
    src = tmp_path / "bom_quoted.csv"
    src.write_bytes(b'\xef\xbb\xbf"name","note"\n"a","b"\n')

    dialect = app.detect_csv_dialect(src)
    rows = _normalized_rows(tmp_path, src, dialect)
    assert rows[0][0] == "name", f"first cell was {rows[0][0]!r}"
    assert rows == [["name", "note"], ["a", "b"]]


def test_utf8_bom_with_persian_and_emoji(tmp_path):
    src = tmp_path / "bom_fa.csv"
    text = 'name,note\n"سما","فارسی 😀"\nbob,ok\n'
    src.write_bytes(codecs.BOM_UTF8 + text.encode("utf-8"))

    dialect = app.detect_csv_dialect(src)
    assert dialect.encoding == "UTF-8"
    assert _normalized_rows(tmp_path, src, dialect) == _logical_rows(text)


def test_detection_never_modifies_the_source(tmp_path):
    path = tmp_path / "src.csv"
    path.write_bytes(("é" * 40000 + ",b\nx,y\n").encode("utf-8"))
    before = digest(path)
    app.detect_csv_dialect(path)
    assert digest(path) == before


# --------------------------------------------------------------------------- #
# PF-021 - consistency-first delimiter scoring
# --------------------------------------------------------------------------- #

def test_prose_commas_do_not_beat_the_true_semicolon_delimiter(tmp_path):
    path = tmp_path / "noisy.csv"
    path.write_text(
        "name;note\n"
        "ann;hello, there, friend, indeed\n"
        "bob;a, b, c, d, e, f, g\n"
        "cid;short\n"
        "dee;one, two\n",
        encoding="utf-8",
    )
    assert app.detect_csv_dialect(path).delimiter == ";"


@pytest.mark.parametrize("delim", [",", ";", "\t", ":"])
def test_clean_files_detect_their_delimiter(tmp_path, delim):
    path = tmp_path / "clean.csv"
    rows = [["name", "city", "n"], ["ann", "kyiv", "1"], ["bob", "oslo", "2"],
            ["cid", "lima", "3"]]
    path.write_text("\n".join(delim.join(r) for r in rows), encoding="utf-8")
    dialect = app.detect_csv_dialect(path)
    assert dialect.delimiter == delim
    assert dialect.confidence == "high"


def test_quoted_delimiters_do_not_confuse_detection(tmp_path):
    path = tmp_path / "quoted.csv"
    path.write_text(
        'name,note\n"ann","a;b;c;d"\n"bob","x;y;z"\n"cid","p;q"\n',
        encoding="utf-8",
    )
    assert app.detect_csv_dialect(path).delimiter == ","


def test_single_row_sample_is_low_confidence(tmp_path):
    path = tmp_path / "one.csv"
    path.write_text("a,b,c\n", encoding="utf-8")
    assert app.detect_csv_dialect(path).confidence == "low"


def test_inconsistent_input_is_reported_as_ambiguous(tmp_path):
    path = tmp_path / "ragged.csv"
    path.write_text("a b c\nd,e\nf;g;h;i;j\nk\n", encoding="utf-8")
    assert app.detect_csv_dialect(path).confidence == "low"


# --------------------------------------------------------------------------- #
# PF-011 - streaming normalization with bounded memory
# --------------------------------------------------------------------------- #

def test_normalization_preserves_quoting_newlines_and_unicode(tmp_path):
    src = tmp_path / "rich.csv"
    rows = [
        ["name", "note"],
        ["ann", "multi\nline value"],
        ["bob", 'has "quotes" and, commas'],
        ["سما", "فارسی"],
    ]
    with open(src, "w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, delimiter=";").writerows(rows)

    dialect = app.detect_csv_dialect(src)
    out = tmp_path / "norm.csv"
    app.normalize_csv_for_import(src, dialect, out)

    with open(out, "r", encoding="utf-8", newline="") as handle:
        assert list(csv.reader(handle)) == rows


def test_normalization_leaves_the_source_untouched(tmp_path):
    src = tmp_path / "src.csv"
    src.write_text("a;b\n1;2\n", encoding="utf-8")
    before = digest(src)
    dialect = app.detect_csv_dialect(src)
    app.normalize_csv_for_import(src, dialect, tmp_path / "out.csv")
    assert digest(src) == before


def test_large_csv_normalizes_within_a_memory_ceiling(tmp_path):
    """A whole-file list() scaled with row count; streaming must not."""
    tracemalloc = pytest.importorskip("tracemalloc")
    src = tmp_path / "big.csv"
    rows = 200_000
    with open(src, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "name", "note"])
        for i in range(rows):
            writer.writerow([i, f"name{i}", "some padding text value here"])
    assert src.stat().st_size > 5_000_000

    dialect = app.detect_csv_dialect(src)
    out = tmp_path / "big_norm.csv"

    tracemalloc.start()
    try:
        app.normalize_csv_for_import(src, dialect, out)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Streaming keeps the peak far below the file size; a full materialization
    # of 200k rows would be tens of megabytes.
    assert peak < 8 * 1024 * 1024, f"peak allocation {peak} suggests buffering"
    with open(out, "r", encoding="utf-8", newline="") as handle:
        assert sum(1 for _ in handle) == rows + 1


def test_long_rows_are_handled(tmp_path):
    src = tmp_path / "wide.csv"
    wide = ",".join(str(i) for i in range(500))
    src.write_text(wide + "\n" + wide + "\n", encoding="utf-8")
    dialect = app.detect_csv_dialect(src)
    out = tmp_path / "wide_out.csv"
    app.normalize_csv_for_import(src, dialect, out)
    assert out.exists() and out.stat().st_size > 0


def test_malformed_row_error_does_not_leave_a_partial_claim(tmp_path):
    src = tmp_path / "bad.csv"
    # A NUL byte makes csv raise while reading.
    src.write_bytes(b"a,b\n1,2\n\x00broken\n")
    dialect = app.detect_csv_dialect(src)
    out = tmp_path / "bad_out.csv"
    try:
        app.normalize_csv_for_import(src, dialect, out)
    except (csv.Error, ValueError):
        pass
    assert digest(src) == hashlib.sha256(
        b"a,b\n1,2\n\x00broken\n").hexdigest(), "source must be untouched"


# --------------------------------------------------------------------------- #
# PF-022 - temporary directories never leak
# --------------------------------------------------------------------------- #

def _csv_temp_dirs():
    return list(Path(tempfile.gettempdir()).glob("pdfforge_csv_*"))


@pytest.mark.parametrize("failure", [
    "password", "bridge", "runtime", "validation", "timeout", "cancel", "interrupt",
])
def test_csv_temp_dir_is_removed_on_every_failure_path(tmp_path, monkeypatch, failure):
    """Inject each failure after the temp directory exists."""
    src = tmp_path / "in.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")
    before = set(_csv_temp_dirs())

    class FakeServer:
        port = 1

    errors = {
        "password": app.office_runtime.OfficeRuntimeError(
            app.office_runtime.PASSWORD_SENTINEL),
        "bridge": app.office_runtime.OfficeRuntimeError(
            app.office_runtime.BRIDGE_LOST_SENTINEL),
        "runtime": app.office_runtime.OfficeRuntimeError("boom"),
        "validation": app.PdfOpenError("invalid output"),
        "timeout": app.office_runtime.OfficeRuntimeError(
            app.office_runtime.BRIDGE_LOST_SENTINEL),
        "cancel": app._ExitRequested(),
        "interrupt": KeyboardInterrupt(),
    }

    def explode(*_a, **_k):
        raise errors[failure]

    monkeypatch.setattr(app.office_runtime, "convert_to_pdf", explode)
    monkeypatch.setattr(app.ops_office, "_prompt_convert_password",
                        lambda *_a, **_k: None)

    job = {"src": src, "family": "csv", "out": tmp_path / "in.pdf",
           "csv_dialect": app.detect_csv_dialect(src)}
    try:
        app.ops_office._convert_one(FakeServer(), job)
    except (app._ExitRequested, KeyboardInterrupt,
            app.office_runtime.OfficeRuntimeError):
        # Bridge loss is deliberately re-raised so the batch runner can restart
        # the runtime; cleanup must still have happened on the way out.
        pass

    leaked = set(_csv_temp_dirs()) - before
    assert not leaked, f"leaked CSV temp dirs: {leaked}"


def test_csv_temp_dir_is_removed_on_success(tmp_path, monkeypatch):
    src = tmp_path / "ok.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")
    before = set(_csv_temp_dirs())

    class FakeServer:
        port = 1

    def fake_convert(_server, in_path, out_path, **_kw):
        Path(out_path).write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(app.office_runtime, "convert_to_pdf", fake_convert)
    monkeypatch.setattr(app.ops_office, "_validate_pdf_output", lambda _p: None)

    job = {"src": src, "family": "csv", "out": tmp_path / "ok.pdf",
           "csv_dialect": app.detect_csv_dialect(src)}
    app.ops_office._convert_one(FakeServer(), job)

    assert not (set(_csv_temp_dirs()) - before)


# --------------------------------------------------------------------------- #
# Sibling defect: the conversion success path was never exercised
# --------------------------------------------------------------------------- #

def test_convert_one_success_path_finalizes_the_output(tmp_path, monkeypatch):
    """Regression: _convert_one's *success* branch raised NameError.

    Every unit test drove a failure path, so a missing import in the finalize
    step (promote_atomically) only surfaced during real conversion. This drives
    the success branch with a stubbed converter.
    """
    src = tmp_path / "ok.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")

    class FakeServer:
        port = 1

    def fake_convert(_server, _in_path, out_path, **_kw):
        Path(out_path).write_bytes(b"%PDF-1.4\n%stub\n")

    monkeypatch.setattr(app.office_runtime, "convert_to_pdf", fake_convert)
    monkeypatch.setattr(app.ops_office, "_validate_pdf_output", lambda _p: None)

    plan = app.ops_office._build_jobs([src])
    job = plan.accepted[0]
    assert app.ops_office._convert_one(FakeServer(), job) == "ok"
    assert job["out"].exists(), "the finalized PDF must exist"
    assert job["out"].read_bytes().startswith(b"%PDF")


def test_every_writer_module_resolves_the_promotion_helper():
    """A star-import cannot silently drop the shared promotion helper."""
    import importlib

    for name in ("pdf_io", "render", "watermark", "compress", "encrypt",
                 "unlock", "ops_office"):
        module = importlib.import_module(f"pdf_forge.{name}")
        assert hasattr(module, "promote_atomically"), (
            f"pdf_forge.{name} cannot resolve promote_atomically"
        )
