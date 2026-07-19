# -*- coding: utf-8 -*-
"""Parsing of page selections, chunk sizes, multi-file input, and output names.

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
