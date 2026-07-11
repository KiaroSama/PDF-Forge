# -*- coding: utf-8 -*-
"""Automated tests for PDF Forge.

Covers page-selection parsing, chunk computation, filename generation,
unique path/folder generation, source/output collision prevention, safe
writing with temp-file cleanup, original-file integrity, PDF merging, folder
discovery, and Unicode/Persian paths. Tests use temporary directories and
generated small PDFs only; they never touch real user files.
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

# Make the application module importable regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402

from pypdf import PdfReader, PdfWriter  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_pdf(path: Path, pages: int) -> Path:
    """Create a small valid PDF with the requested number of blank pages."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# parse_page_selection
# --------------------------------------------------------------------------- #

def test_single_page():
    result = app.parse_page_selection("1", 10)
    assert result.pages == [1]
    assert result.duplicates_removed is False


def test_multiple_pages():
    assert app.parse_page_selection("1,2", 10).pages == [1, 2]


def test_inclusive_range():
    assert app.parse_page_selection("10-20", 50).pages == list(range(10, 21))


def test_mixed_selection():
    result = app.parse_page_selection("10-20,25,30-50", 60)
    expected = list(range(10, 21)) + [25] + list(range(30, 51))
    assert result.pages == expected


def test_whitespace_handling():
    assert app.parse_page_selection(" 1, 2, 5-10 ", 20).pages == [1, 2, 5, 6, 7, 8, 9, 10]
    assert app.parse_page_selection("10 - 20, 25", 30).pages == list(range(10, 21)) + [25]


def test_duplicate_removal_preserves_order():
    result = app.parse_page_selection("5,5,1,1,3", 10)
    assert result.pages == [5, 1, 3]
    assert result.duplicates_removed is True


def test_out_of_range_page():
    with pytest.raises(app.PageSelectionError):
        app.parse_page_selection("11", 10)


def test_out_of_range_in_range_expr():
    with pytest.raises(app.PageSelectionError):
        app.parse_page_selection("5-15", 10)


def test_reversed_range():
    with pytest.raises(app.PageSelectionError):
        app.parse_page_selection("20-10", 50)


def test_zero_page():
    with pytest.raises(app.PageSelectionError):
        app.parse_page_selection("0", 10)


def test_negative_page():
    with pytest.raises(app.PageSelectionError):
        app.parse_page_selection("-5", 10)


@pytest.mark.parametrize("expr", ["", "   ", ",", "1,,2", "abc", "1-2-3", "1-", "-", "5-x"])
def test_malformed_expressions(expr):
    with pytest.raises(app.PageSelectionError):
        app.parse_page_selection(expr, 100)


# --------------------------------------------------------------------------- #
# compute_chunks / parse_chunk_size
# --------------------------------------------------------------------------- #

def test_chunks_300_by_50():
    chunks = app.compute_chunks(300, 50)
    assert chunks == [
        (1, 50), (51, 100), (101, 150),
        (151, 200), (201, 250), (251, 300),
    ]


def test_chunks_323_by_50():
    chunks = app.compute_chunks(323, 50)
    assert chunks[-1] == (301, 323)
    assert len(chunks) == 7


def test_chunk_larger_than_document():
    assert app.compute_chunks(40, 50) == [(1, 40)]


def test_chunks_with_subrange():
    # 300-page doc, pages 20..280, chunk 50.
    chunks = app.compute_chunks(300, 50, first_page=20, last_page=280)
    assert chunks[0] == (20, 69)
    assert chunks[-1] == (270, 280)
    # Spans 261 pages -> ceil(261/50) = 6 files.
    assert len(chunks) == 6


def test_chunks_subrange_exact_multiple():
    chunks = app.compute_chunks(300, 50, first_page=51, last_page=150)
    assert chunks == [(51, 100), (101, 150)]


def test_chunks_invalid_subrange():
    with pytest.raises(app.ChunkSizeError):
        app.compute_chunks(100, 10, first_page=50, last_page=40)  # reversed
    with pytest.raises(app.ChunkSizeError):
        app.compute_chunks(100, 10, first_page=1, last_page=200)  # end beyond doc
    with pytest.raises(app.ChunkSizeError):
        app.compute_chunks(100, 10, first_page=0, last_page=50)   # start < 1


def test_parse_page_number_defaults_and_validation():
    assert app.parse_page_number("", 1, 300, "start page") == 1
    assert app.parse_page_number("   ", 300, 300, "end page") == 300
    assert app.parse_page_number("20", 1, 300, "start page") == 20
    for bad in ["0", "-5", "1.5", "abc", "301"]:
        with pytest.raises(app.ChunkSizeError):
            app.parse_page_number(bad, 1, 300, "start page")


# --------------------------------------------------------------------------- #
# Multi-file extraction ('|' separator)
# --------------------------------------------------------------------------- #

def test_multi_file_single_group():
    groups = app.parse_multi_file_selection("6-37,39-85,353-375", 400)
    assert len(groups) == 1
    assert groups[0].pages[0] == 6 and groups[0].pages[-1] == 375


def test_multi_file_three_groups():
    groups = app.parse_multi_file_selection("6-37|39-85|353-375", 400)
    assert len(groups) == 3
    assert groups[0].pages == list(range(6, 38))
    assert groups[1].pages == list(range(39, 86))
    assert groups[2].pages == list(range(353, 376))
    assert [g.text for g in groups] == ["6-37", "39-85", "353-375"]


def test_multi_file_mixed_groups():
    # First group combines two ranges; second group is a single range.
    groups = app.parse_multi_file_selection("6-37,39-85|353-375", 400)
    assert len(groups) == 2
    assert groups[0].pages == list(range(6, 38)) + list(range(39, 86))
    assert groups[1].pages == list(range(353, 376))


def test_multi_file_empty_group_rejected():
    for bad in ["6-37||39-85", "|6-37", "6-37|", "6-37| "]:
        with pytest.raises(app.PageSelectionError):
            app.parse_multi_file_selection(bad, 400)


def test_multi_file_invalid_group_rejected():
    with pytest.raises(app.PageSelectionError):
        app.parse_multi_file_selection("6-37|20-10", 400)  # reversed range in group 2


@pytest.mark.parametrize("bad", ["0", "-1", "1.5", "abc", "", "  ", "3.0"])
def test_invalid_chunk_size(bad):
    with pytest.raises(app.ChunkSizeError):
        app.parse_chunk_size(bad)


def test_valid_chunk_size():
    assert app.parse_chunk_size(" 50 ") == 50


# --------------------------------------------------------------------------- #
# Filename / path helpers
# --------------------------------------------------------------------------- #

def test_build_extract_output_name():
    name = app.build_extract_output_name("Doc", "10-20, 25, 30-50", 33)
    assert name == "Doc_pages_10-20_25_30-50.pdf"


def test_build_extract_output_name_fallback():
    long_sel = ",".join(str(i) for i in range(1, 80))
    name = app.build_extract_output_name("Doc", long_sel, 79)
    assert name == "Doc_selected_79_pages.pdf"


def test_build_chunk_output_name_padding():
    assert app.build_chunk_output_name("Doc", 1, 50, 3) == "Doc_pages_001-050.pdf"
    assert app.build_chunk_output_name("Doc", 301, 323, 3) == "Doc_pages_301-323.pdf"


def test_summarize_ranges():
    assert app.summarize_ranges(list(range(6, 38))) == "6-37"
    assert app.summarize_ranges(list(range(6, 38)) + list(range(39, 86))) == "6-37, 39-85"
    assert app.summarize_ranges([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10"
    assert app.summarize_ranges([5]) == "5"
    assert app.summarize_ranges([]) == ""


def test_strip_surrounding_quotes():
    assert app.strip_surrounding_quotes('"C:\\a b\\f.pdf"') == "C:\\a b\\f.pdf"
    assert app.strip_surrounding_quotes("'file.pdf'") == "file.pdf"
    assert app.strip_surrounding_quotes("plain.pdf") == "plain.pdf"


def test_unique_file_path(tmp_path):
    target = tmp_path / "out.pdf"
    assert app.unique_file_path(target) == target
    target.write_bytes(b"x")
    assert app.unique_file_path(target) == tmp_path / "out_2.pdf"


def test_unique_dir_path(tmp_path):
    target = tmp_path / "split"
    assert app.unique_dir_path(target) == target
    target.mkdir()
    assert app.unique_dir_path(target) == tmp_path / "split_2"


# --------------------------------------------------------------------------- #
# I/O: writing, integrity, collision prevention, cleanup
# --------------------------------------------------------------------------- #

def test_write_pages_and_original_unchanged(tmp_path):
    src = make_pdf(tmp_path / "source.pdf", 10)
    original_hash = file_hash(src)

    reader = app.open_source_pdf(src)
    out = tmp_path / "extract.pdf"
    written = app.write_pages_to_pdf(reader, [0, 2, 4], out)

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
    written = app.write_pages_to_pdf(reader, [0, 1, 2], out)

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
        total += app.write_pages_to_pdf(reader, list(range(start - 1, end)), path)

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

    written = app.write_merged_pdfs_to_pdf(readers, out)

    assert written == 8
    assert out.exists()
    assert len(PdfReader(str(out)).pages) == 8


def test_merge_preserves_order(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", 2)
    b = make_pdf(tmp_path / "b.pdf", 4)
    c = make_pdf(tmp_path / "c.pdf", 1)
    readers = [app.open_source_pdf(c), app.open_source_pdf(a), app.open_source_pdf(b)]
    out = tmp_path / "ordered.pdf"
    written = app.write_merged_pdfs_to_pdf(readers, out)
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
    written = app.write_merged_pdfs_to_pdf(readers, out)

    assert written == 5
    assert out.exists()
    assert file_hash(a) == hash_a
    assert file_hash(b) == hash_b


# --------------------------------------------------------------------------- #
# Image conversion: quality mapping, naming, PNG rendering, image-only PDF
# --------------------------------------------------------------------------- #

def test_image_dpi_for_quality():
    assert app.image_dpi_for_quality("very low") == 72
    assert app.image_dpi_for_quality("low") == 96
    assert app.image_dpi_for_quality("MEDIUM") == 150
    assert app.image_dpi_for_quality("High") == 300
    assert app.image_dpi_for_quality("very high") == 450
    assert app.image_dpi_for_quality("ultra") == 600
    with pytest.raises(ValueError):
        app.image_dpi_for_quality("bogus")


def test_build_page_image_name():
    # Page number is used verbatim, with no zero-padding.
    assert app.build_page_image_name(2) == "2.png"
    assert app.build_page_image_name(10) == "10.png"


def test_default_image_output_names(tmp_path):
    src = tmp_path / "Report.pdf"
    assert app.default_images_output_dir(src) == tmp_path / "Report_images"
    assert app.default_image_pdf_output(src) == tmp_path / "Report_image.pdf"


def test_render_all_pages_to_pngs(tmp_path):
    from PIL import Image

    src = make_pdf(tmp_path / "doc.pdf", 3)
    pdf, n = app.open_render_document(src)
    try:
        assert n == 3
        created = app.render_pages_to_pngs(pdf, list(range(n)), tmp_path / "imgs", dpi=96)
    finally:
        pdf.close()

    assert sorted(p.name for p in created) == ["1.png", "2.png", "3.png"]
    for path in created:
        with Image.open(path) as im:
            im.verify()  # Confirms each PNG is valid and not truncated.


def test_render_selected_pages_named_by_page_number(tmp_path):
    src = make_pdf(tmp_path / "doc.pdf", 12)
    pdf, n = app.open_render_document(src)
    try:
        # Pages 2 and 10 (0-based indices 1 and 9).
        created = app.render_pages_to_pngs(pdf, [1, 9], tmp_path / "imgs", dpi=96)
    finally:
        pdf.close()
    # Each file is named after its own page number.
    assert sorted(p.name for p in created) == ["10.png", "2.png"]


def test_render_pdf_to_image_pdf(tmp_path):
    src = make_pdf(tmp_path / "doc.pdf", 4)
    original_hash = file_hash(src)
    pdf, n = app.open_render_document(src)
    out = tmp_path / "image_only.pdf"
    try:
        written = app.render_pdf_to_image_pdf(pdf, n, out, dpi=96)
    finally:
        pdf.close()

    assert written == 4
    reader = PdfReader(str(out))
    assert len(reader.pages) == 4
    assert reader.is_encrypted is False
    # The rasterized output has no extractable text (non-editable).
    assert reader.pages[0].extract_text().strip() == ""
    # Source is untouched.
    assert file_hash(src) == original_hash


def test_png_temp_cleanup_on_failure(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "doc.pdf", 2)
    pdf, _n = app.open_render_document(src)
    out_dir = tmp_path / "imgs"

    def boom(*_args, **_kwargs):
        raise app.PdfOpenError("simulated image validation failure")

    monkeypatch.setattr(app.render, "_validate_image_file", boom)
    try:
        with pytest.raises(app.PdfOpenError):
            app.render_pages_to_pngs(pdf, [0], out_dir, dpi=96)
    finally:
        pdf.close()

    # No final image and no leftover temp files.
    assert list(out_dir.glob("*.png")) == []
    assert list(out_dir.glob(".pdfforge_*")) == []


def test_image_conversion_unicode_persian_paths(tmp_path):
    folder = tmp_path / "اسناد"  # "documents"
    folder.mkdir()
    src = make_pdf(folder / "گزارش.pdf", 2)  # "report"
    pdf, n = app.open_render_document(src)
    try:
        created = app.render_pages_to_pngs(pdf, list(range(n)), folder / "تصاویر", dpi=96)
    finally:
        pdf.close()
    assert sorted(p.name for p in created) == ["1.png", "2.png"]


# --------------------------------------------------------------------------- #
# Watermark removal: index parsing, candidate scanning, removal
# --------------------------------------------------------------------------- #

def make_repeated_image_pdf(path: Path, pages: int, size=(120, 80),
                            color=(200, 0, 0)) -> Path:
    """Create a PDF with the same image on every page (a stand-in watermark)."""
    from PIL import Image

    images = [Image.new("RGB", size, color) for _ in range(pages)]
    images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    return path


def test_parse_index_list():
    assert app.parse_index_list("1", 3) == [1]
    assert app.parse_index_list("1,3", 3) == [1, 3]
    assert app.parse_index_list("3,1,3", 3) == [1, 3]  # dedup + sort
    for bad in ["", "   ", "0", "4", "abc", "1,,2", "-1", "1.5"]:
        with pytest.raises(ValueError):
            app.parse_index_list(bad, 3)


def test_scan_watermark_candidates(tmp_path):
    src = make_repeated_image_pdf(tmp_path / "wm.pdf", 3)
    reader = PdfReader(str(src))
    candidates, total = app.scan_watermark_candidates(reader.pages)
    assert total == 3
    assert len(candidates) >= 1
    top = candidates[0]
    assert top.pages == {1, 2, 3}          # image repeats on every page
    assert (top.width, top.height) == (120, 80)


def test_remove_watermark_images(tmp_path):
    src = make_repeated_image_pdf(tmp_path / "wm.pdf", 3)
    reader = PdfReader(str(src))
    candidates, _ = app.scan_watermark_candidates(reader.pages)
    target_sig = candidates[0].signature

    out = tmp_path / "clean.pdf"
    modified = app.remove_watermark_images(reader, [target_sig], out)

    assert modified == 3
    result = PdfReader(str(out))
    assert len(result.pages) == 3
    assert result.is_encrypted is False
    # The repeated image is gone: no candidate with that signature remains.
    candidates_after, _ = app.scan_watermark_candidates(result.pages, min_pages=2)
    assert target_sig not in {c.signature for c in candidates_after}


# --------------------------------------------------------------------------- #
# Delete pages: parsing, deletion computation, naming, integrity
# --------------------------------------------------------------------------- #

def test_parse_delete_pages():
    assert app.parse_delete_pages("5") == [5]
    assert app.parse_delete_pages("3,1,2") == [1, 2, 3]           # sorted + unique
    assert app.parse_delete_pages("10-12,11") == [10, 11, 12]     # overlap dedup
    # No upper bound: large page numbers are allowed (checked per file later).
    assert app.parse_delete_pages("999") == [999]
    for bad in ["", "  ", "0", "-1", "abc", "1,,2", "20-10", "1-2-3"]:
        with pytest.raises(app.PageSelectionError):
            app.parse_delete_pages(bad)


def test_compute_deletion():
    present, missing, kept = app.compute_deletion(10, [2, 4, 99])
    assert present == [2, 4]
    assert missing == [99]
    assert kept == [0, 2, 4, 5, 6, 7, 8, 9]  # 0-based, pages 2 and 4 removed

    # Deleting everything leaves nothing to keep.
    present, missing, kept = app.compute_deletion(3, [1, 2, 3])
    assert present == [1, 2, 3] and missing == [] and kept == []

    # Nothing requested exists in the document.
    present, missing, kept = app.compute_deletion(3, [7, 8])
    assert present == [] and missing == [7, 8] and kept == [0, 1, 2]


def test_build_delete_output_name():
    assert app.build_delete_output_name("Doc", "10-20, 25") == "Doc_deleted_10-20_25.pdf"
    long_sel = ", ".join(str(i) for i in range(1, 80))
    assert app.build_delete_output_name("Doc", long_sel) == "Doc_pages_deleted.pdf"


def test_delete_pages_end_to_end(tmp_path):
    src = make_pdf(tmp_path / "doc.pdf", 6)
    original_hash = file_hash(src)
    reader = app.open_source_pdf(src)

    _present, _missing, kept = app.compute_deletion(6, [2, 5])  # delete pages 2 and 5
    out = tmp_path / "trimmed.pdf"
    written = app.write_pages_to_pdf(reader, kept, out)

    assert written == 4
    assert len(PdfReader(str(out)).pages) == 4
    # Source untouched.
    assert file_hash(src) == original_hash


def test_remove_watermark_preserves_other_pages(tmp_path):
    # Two shared images: one on all 3 pages (watermark), one only on page 1.
    src = make_repeated_image_pdf(tmp_path / "wm.pdf", 3)
    reader = PdfReader(str(src))
    candidates, _ = app.scan_watermark_candidates(reader.pages)
    out = tmp_path / "clean.pdf"
    app.remove_watermark_images(reader, [candidates[0].signature], out)
    # Output still opens and keeps its page count (no pages dropped).
    assert len(PdfReader(str(out)).pages) == 3


# --------------------------------------------------------------------------- #
# Compress PDF: presets, lossy shrink, lossless mode, source integrity
# --------------------------------------------------------------------------- #

def make_photo_pdf(path: Path, pages: int = 2, size=(1600, 1200)) -> Path:
    """Create an image-heavy PDF (a smooth gradient photo on every page)."""
    from PIL import Image

    images = []
    for _ in range(pages):
        img = Image.new("RGB", size)
        px = img.load()
        for x in range(0, size[0], 4):
            for y in range(0, size[1], 4):
                c = (x * 255 // size[0], y * 255 // size[1], 128)
                for dx in range(4):
                    for dy in range(4):
                        px[x + dx, y + dy] = c
        images.append(img)
    # Pillow embeds RGB images losslessly by default via its PDF writer.
    images[0].save(path, "PDF", save_all=True, append_images=images[1:],
                   resolution=150.0)
    return path


def test_compression_presets_shape():
    # Six named levels; ultra is the lossless-only sentinel.
    assert set(app.COMPRESSION_PRESETS) == {
        "very low", "low", "medium", "high", "very high", "ultra"
    }
    assert app.COMPRESSION_PRESETS["ultra"] is None
    # Lossy presets are (jpeg_quality, dpi_target) with sane ordering.
    q_low, dpi_low = app.COMPRESSION_PRESETS["very low"]
    q_high, dpi_high = app.COMPRESSION_PRESETS["very high"]
    assert q_low < q_high and dpi_low < dpi_high


def test_format_size():
    assert app._format_size(512) == "512 B"
    assert app._format_size(2048) == "2.0 KB"
    assert app._format_size(3 * 1024 * 1024) == "3.00 MB"


def test_compress_pdf_lossy_shrinks_image_pdf(tmp_path):
    src = make_photo_pdf(tmp_path / "photos.pdf")
    original_hash = file_hash(src)
    out = tmp_path / "photos_compressed.pdf"

    quality, dpi = app.COMPRESSION_PRESETS["medium"]
    stats = app.compress_pdf(src, out, quality, dpi)

    assert out.exists()
    assert stats["pages"] == 2
    assert stats["new_size"] < stats["original_size"]
    assert len(PdfReader(str(out)).pages) == 2
    # Source must remain byte-for-byte unchanged.
    assert file_hash(src) == original_hash


def test_compress_pdf_ultra_lossless_valid_output(tmp_path):
    src = make_pdf(tmp_path / "text.pdf", 5)
    out = tmp_path / "text_compressed.pdf"

    stats = app.compress_pdf(src, out, None, None)  # ultra: lossless only

    assert out.exists()
    assert stats["pages"] == 5
    assert len(PdfReader(str(out)).pages) == 5


def test_compress_temp_cleanup_on_failure(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "doc.pdf", 3)
    out = tmp_path / "fail.pdf"

    def boom(*_args, **_kwargs):
        raise app.PdfOpenError("simulated compression validation failure")

    monkeypatch.setattr(app.compress, "_validate_written_pdf", boom)

    with pytest.raises(app.PdfOpenError):
        app.compress_pdf(src, out, None, None)

    assert not out.exists()
    assert list(tmp_path.glob(".pdfforge_*")) == []
