# -*- coding: utf-8 -*-
"""Writing pages, collision prevention, source integrity, Unicode paths, merging.

Split out of the former single test_pdf_forge module. Tests use temporary
directories and generated small PDFs only; they never touch real user files.
"""

import sys
from pathlib import Path

import pytest  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402,F401
from helpers import file_hash, make_pdf  # noqa: E402,F401
from pypdf import PdfReader, PdfWriter  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# I/O: writing, integrity, collision prevention, cleanup
# --------------------------------------------------------------------------- #

def test_write_pages_and_original_unchanged(tmp_path):
    src = make_pdf(tmp_path / "source.pdf", 10)
    original_hash = file_hash(src)

    reader = app.open_source_pdf(src)
    out = tmp_path / "extract.pdf"
    written = app.write_pages_to_pdf(reader, [0, 2, 4], out).count

    assert written == 3
    assert out.exists()
    assert len(PdfReader(str(out)).pages) == 3
    # Original must remain byte-for-byte unchanged.
    assert file_hash(src) == original_hash


def test_source_output_collision_detection(tmp_path):
    src = make_pdf(tmp_path / "doc.pdf", 5)
    assert app.resolves_to_same_file(src, tmp_path / "doc.pdf") is True
    assert app.resolves_to_same_file(src, tmp_path / "other.pdf") is False


def test_temp_cleanup_on_write_failure(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "source.pdf", 5)
    reader = app.open_source_pdf(src)
    out = tmp_path / "fail.pdf"

    # Force validation to fail after the temp file is written.
    def boom(*_args, **_kwargs):
        raise app.PdfOpenError("simulated validation failure")

    monkeypatch.setattr(app.pdf_io, "_validate_written_pdf", boom)

    with pytest.raises(app.PdfOpenError):
        app.write_pages_to_pdf(reader, [0, 1], out)

    # No final output and no leftover temp files.
    assert not out.exists()
    leftovers = list(tmp_path.glob(".pdfforge_*"))
    assert leftovers == []


def test_no_pages_pdf_rejected(tmp_path):
    # A zero-page PDF is invalid input for our flows.
    empty = make_pdf(tmp_path / "empty.pdf", 0)
    with pytest.raises(app.PdfOpenError):
        app.open_source_pdf(empty)


# --------------------------------------------------------------------------- #
# Unicode / Persian paths
# --------------------------------------------------------------------------- #

def test_unicode_and_persian_paths(tmp_path):
    folder = tmp_path / "اسناد محرمانه"  # "confidential documents" in Persian
    folder.mkdir()
    src = make_pdf(folder / "نمونه فایل.pdf", 6)  # "sample file"
    original_hash = file_hash(src)

    reader = app.open_source_pdf(src)
    out = folder / "خروجی.pdf"  # "output"
    written = app.write_pages_to_pdf(reader, [0, 1, 2], out).count

    assert written == 3
    assert out.exists()
    assert file_hash(src) == original_hash


# --------------------------------------------------------------------------- #
# End-to-end chunk write integrity
# --------------------------------------------------------------------------- #

def test_full_chunk_split_integrity(tmp_path):
    src = make_pdf(tmp_path / "big.pdf", 23)
    original_hash = file_hash(src)
    reader = app.open_source_pdf(src)

    chunks = app.compute_chunks(23, 10)  # (1,10),(11,20),(21,23)
    pad = app.pad_width_for(23)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    total = 0
    for start, end in chunks:
        name = app.build_chunk_output_name("big", start, end, pad)
        path = out_dir / name
        total += app.write_pages_to_pdf(
            reader, list(range(start - 1, end)), path).count

    assert total == 23
    files = sorted(out_dir.glob("*.pdf"))
    assert len(files) == 3
    assert files[0].name == "big_pages_001-010.pdf"
    assert files[-1].name == "big_pages_021-023.pdf"
    # Source intact.
    assert file_hash(src) == original_hash


# --------------------------------------------------------------------------- #
# Merge: core I/O, discovery, integrity, collision, cleanup, unicode
# --------------------------------------------------------------------------- #

def test_merge_two_pdfs_page_count(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", 3)
    b = make_pdf(tmp_path / "b.pdf", 5)
    readers = [app.open_source_pdf(a), app.open_source_pdf(b)]
    out = tmp_path / "merged.pdf"

    written = app.write_merged_pdfs_to_pdf(readers, out).count

    assert written == 8
    assert out.exists()
    assert len(PdfReader(str(out)).pages) == 8


def test_merge_preserves_order(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", 2)
    b = make_pdf(tmp_path / "b.pdf", 4)
    c = make_pdf(tmp_path / "c.pdf", 1)
    readers = [app.open_source_pdf(c), app.open_source_pdf(a), app.open_source_pdf(b)]
    out = tmp_path / "ordered.pdf"
    written = app.write_merged_pdfs_to_pdf(readers, out).count
    # c(1) + a(2) + b(4) = 7 pages in that order.
    assert written == 7
    assert len(PdfReader(str(out)).pages) == 7


def test_merge_source_integrity(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", 3)
    b = make_pdf(tmp_path / "b.pdf", 4)
    hash_a, hash_b = file_hash(a), file_hash(b)

    readers = [app.open_source_pdf(a), app.open_source_pdf(b)]
    app.write_merged_pdfs_to_pdf(readers, tmp_path / "out.pdf")

    assert file_hash(a) == hash_a
    assert file_hash(b) == hash_b


def test_merge_output_collision_unique_name(tmp_path):
    out = tmp_path / "merged.pdf"
    out.write_bytes(b"existing")
    # The merge flow resolves the final path through unique_file_path.
    assert app.unique_file_path(out) == tmp_path / "merged_2.pdf"


def test_merge_reject_output_same_as_source(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", 2)
    b = make_pdf(tmp_path / "b.pdf", 2)
    sources = [a, b]
    # An output path equal to any source must be detected.
    assert any(app.resolves_to_same_file(a, src) for src in sources) is True
    assert any(app.resolves_to_same_file(tmp_path / "c.pdf", src) for src in sources) is False


def test_default_merge_output_naming(tmp_path):
    a = make_pdf(tmp_path / "first.pdf", 1)
    b = make_pdf(tmp_path / "second.pdf", 1)
    files_default = app._default_merge_output("files", [a, b])
    assert files_default == tmp_path / "first_merged.pdf"

    folder = tmp_path / "docs"
    folder.mkdir()
    f1 = make_pdf(folder / "x.pdf", 1)
    f2 = make_pdf(folder / "y.pdf", 1)
    folder_default = app._default_merge_output("folder", [f1, f2])
    assert folder_default == folder / "docs_merged.pdf"


def test_discover_pdfs_sorted_case_insensitive(tmp_path):
    # Create PDFs with unordered, mixed-case names plus a non-PDF file.
    make_pdf(tmp_path / "Banana.pdf", 1)
    make_pdf(tmp_path / "apple.pdf", 1)
    make_pdf(tmp_path / "Cherry.pdf", 1)
    (tmp_path / "notes.txt").write_text("ignore me")
    # A subdirectory PDF must NOT be discovered (non-recursive).
    sub = tmp_path / "sub"
    sub.mkdir()
    make_pdf(sub / "deep.pdf", 1)

    found = app.discover_pdfs_in_folder(tmp_path)
    names = [p.name for p in found]
    assert names == ["apple.pdf", "Banana.pdf", "Cherry.pdf"]


def test_discover_pdfs_fewer_than_two(tmp_path):
    make_pdf(tmp_path / "only.pdf", 1)
    found = app.discover_pdfs_in_folder(tmp_path)
    assert len(found) == 1


def test_natural_sort_key_orders_numbers_by_value():
    names = ["10.pdf", "1.pdf", "2.pdf"]
    ordered = sorted(names, key=app.natural_sort_key)
    assert ordered == ["1.pdf", "2.pdf", "10.pdf"]


def test_discover_pdfs_natural_numeric_order(tmp_path):
    # Lexical order would give 1, 10, 2; natural order must give 1, 2, 10.
    for n in (1, 2, 10, 21, 3):
        make_pdf(tmp_path / f"{n}.pdf", 1)
    found = app.discover_pdfs_in_folder(tmp_path)
    assert [p.name for p in found] == ["1.pdf", "2.pdf", "3.pdf", "10.pdf", "21.pdf"]


def test_discover_pdfs_natural_mixed_case_and_numbers(tmp_path):
    make_pdf(tmp_path / "Chapter2.pdf", 1)
    make_pdf(tmp_path / "chapter10.pdf", 1)
    make_pdf(tmp_path / "Chapter1.pdf", 1)
    found = app.discover_pdfs_in_folder(tmp_path)
    # Case-insensitive grouping of "chapter" + natural numeric ordering.
    assert [p.name for p in found] == ["Chapter1.pdf", "Chapter2.pdf", "chapter10.pdf"]


def test_describe_merge_sort_mode():
    assert "natural" in app._describe_merge_sort_mode("folder").lower()
    assert "manual" in app._describe_merge_sort_mode("files").lower()


def test_merge_temp_cleanup_on_write_failure(tmp_path, monkeypatch):
    a = make_pdf(tmp_path / "a.pdf", 2)
    b = make_pdf(tmp_path / "b.pdf", 2)
    readers = [app.open_source_pdf(a), app.open_source_pdf(b)]
    out = tmp_path / "fail.pdf"

    def boom(*_args, **_kwargs):
        raise app.PdfOpenError("simulated merge validation failure")

    monkeypatch.setattr(app.pdf_io, "_validate_merged_pdf", boom)

    with pytest.raises(app.PdfOpenError):
        app.write_merged_pdfs_to_pdf(readers, out)

    assert not out.exists()
    assert list(tmp_path.glob(".pdfforge_*")) == []


def test_merge_unicode_persian_paths(tmp_path):
    folder = tmp_path / "اسناد محرمانه"  # "confidential documents"
    folder.mkdir()
    a = make_pdf(folder / "سند الف.pdf", 2)  # "document A"
    b = make_pdf(folder / "سند ب.pdf", 3)    # "document B"
    hash_a, hash_b = file_hash(a), file_hash(b)

    readers = [app.open_source_pdf(a), app.open_source_pdf(b)]
    out = folder / "ادغام.pdf"  # "merged"
    written = app.write_merged_pdfs_to_pdf(readers, out).count

    assert written == 5
    assert out.exists()
    assert file_hash(a) == hash_a
    assert file_hash(b) == hash_b
