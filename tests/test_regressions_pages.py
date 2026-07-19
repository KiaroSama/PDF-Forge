# -*- coding: utf-8 -*-
"""Page-tool regressions: merge completion, output reservation, delete
ranges, honest batch reporting, and no filesystem writes while configuring.

Split out of the former single test_regressions module. Each test targets
behaviour that was wrong (or absent) before its fix, so it fails against the
old implementation for the right reason. Tests use temporary directories and
generated files only; they never touch real user files and never require the
native LibreOffice runtime.
"""

import csv  # noqa: F401
import io  # noqa: F401
import os  # noqa: F401
import sys
from pathlib import Path

import pytest  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402,F401
import pymupdf  # noqa: E402,F401
from PIL import Image  # noqa: E402,F401
from helpers import (  # noqa: E402,F401
    label_of, make_encrypted, make_pdf, repeated_image_pdf, rgb_png, rgba_png,
    zip_ooxml,
)
from pypdf import PdfWriter  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# A1 - merge submenu 1 must finish reliably
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("terminator", ["", "done", "DONE", "Done"])
def test_merge_finishes_on_blank_finish_and_done(tmp_path, monkeypatch, terminator):
    a, b = make_pdf(tmp_path / "a.pdf", 1), make_pdf(tmp_path / "b.pdf", 1)
    answers = iter([str(a), str(b), terminator])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "b.pdf"]


def test_merge_minimum_count_error_keeps_flow(tmp_path, monkeypatch):
    """Finishing early errors and stays in the flow; duplicates stay rejected."""
    a, b = make_pdf(tmp_path / "a.pdf", 1), make_pdf(tmp_path / "b.pdf", 1)
    answers = iter(["", "done", str(a), str(a), str(b), "done"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "b.pdf"]


def test_merge_zero_returns_back(tmp_path, monkeypatch):
    answers = iter(["0"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    assert app.ops_merge.prompt_merge_source_files() is None


def test_merge_exit_raises_exit_request(tmp_path, monkeypatch):
    answers = iter(["exit"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    with pytest.raises(app._ExitRequested):
        app.ops_merge.prompt_merge_source_files()


# --------------------------------------------------------------------------- #
# A2 - queue-time output path reservation
# --------------------------------------------------------------------------- #

def test_reservations_prevent_queued_output_collision(tmp_path):
    app.clear_reservations()
    try:
        target = tmp_path / "out.pdf"
        chosen = [app.reserve_unique_file(target) for _ in range(3)]
        assert chosen[0].name == "out.pdf"
        assert len(set(chosen)) == 3, "queued tasks must not share an output path"
    finally:
        app.clear_reservations()


def test_files_and_dirs_are_reserved_separately(tmp_path):
    app.clear_reservations()
    try:
        d1 = app.reserve_unique_dir(tmp_path / "shared")
        d2 = app.reserve_unique_dir(tmp_path / "shared")
        assert d1 != d2
        # A file reservation does not consume a directory name and vice versa.
        assert app.reserve_unique_file(tmp_path / "shared") == tmp_path / "shared"
    finally:
        app.clear_reservations()


def test_reservation_is_case_insensitive_on_windows(tmp_path):
    app.clear_reservations()
    try:
        app.reserve_unique_file(tmp_path / "Case.pdf")
        second = app.reserve_unique_file(tmp_path / "case.pdf")
        if os.name == "nt":
            assert second.name == "case_2.pdf"
        else:
            assert second.name == "case.pdf"
    finally:
        app.clear_reservations()


def test_reservation_respects_existing_disk_file(tmp_path):
    app.clear_reservations()
    try:
        existing = make_pdf(tmp_path / "taken.pdf", 1)
        assert app.reserve_unique_file(existing).name == "taken_2.pdf"
    finally:
        app.clear_reservations()


def test_reservations_released_individually_and_globally(tmp_path):
    app.clear_reservations()
    first = app.reserve_unique_file(tmp_path / "x.pdf")
    app.release_reservations(files=[first])
    assert app.reserve_unique_file(tmp_path / "x.pdf") == first
    app.clear_reservations()
    assert app.reserve_unique_file(tmp_path / "x.pdf") == first
    app.clear_reservations()


# --------------------------------------------------------------------------- #
# A11 - pathological delete ranges
# --------------------------------------------------------------------------- #

def test_huge_delete_range_rejected_without_materializing():
    import time

    started = time.perf_counter()
    with pytest.raises(app.PageSelectionError):
        app.parse_delete_pages("1-999999999")
    assert time.perf_counter() - started < 1.0


def test_delete_range_bounded_by_document_length():
    assert app.parse_delete_pages("1-3", max_page=10) == [1, 2, 3]
    for bad in ("1-50", "999"):
        with pytest.raises(app.PageSelectionError):
            app.parse_delete_pages(bad, max_page=10)


def test_normal_delete_syntax_unchanged():
    assert app.parse_delete_pages("5") == [5]
    assert app.parse_delete_pages("3,1,2") == [1, 2, 3]
    assert app.parse_delete_pages("10-12,11") == [10, 11, 12]


# --------------------------------------------------------------------------- #
# A15 / A16 - honest batch reporting
# --------------------------------------------------------------------------- #

def test_folder_dpi_stats_counts_unscannable_files(tmp_path):
    make_pdf(tmp_path / "plain.pdf", 1)
    make_encrypted(tmp_path / "locked.pdf", pages=1, user_pw="secret",
                   owner_pw="secret")
    stats = app._folder_dpi_stats([tmp_path / "plain.pdf", tmp_path / "locked.pdf"])
    assert stats["files_not_scanned"] == 1, "encrypted files must not vanish"
    assert stats["files_text_only"] == 1
    assert "max" not in stats, "no image DPI can be claimed here"


def test_folder_dpi_stats_all_unscannable(tmp_path):
    make_encrypted(tmp_path / "a.pdf", pages=1, user_pw="s", owner_pw="s")
    make_encrypted(tmp_path / "b.pdf", pages=1, user_pw="s", owner_pw="s")
    stats = app._folder_dpi_stats([tmp_path / "a.pdf", tmp_path / "b.pdf"])
    assert stats["files_not_scanned"] == 2
    assert stats["files_with_images"] == 0 and stats["files_text_only"] == 0


def test_format_size_is_never_asked_for_negatives():
    assert app.ops_compress._format_size(0) == "0 B"
    assert app.ops_compress._format_size(2048) == "2.0 KB"
    assert app.ops_compress._format_size(5 * 1024 * 1024).endswith("MB")


# --------------------------------------------------------------------------- #
# A18 - no filesystem side effects during configuration
# --------------------------------------------------------------------------- #

def test_output_directory_not_created_during_configuration(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "s.pdf", 1)
    target_dir = tmp_path / "not_yet"
    monkeypatch.setattr(app.prompts, "_input", lambda _p: str(target_dir / "o.pdf"))
    monkeypatch.setattr(app.prompts, "ask_yes_no", lambda *_a, **_k: True)
    app.clear_reservations()
    try:
        chosen = app.prompts._choose_output_file(target_dir / "o.pdf", src)
        assert chosen.parent == target_dir
        assert not target_dir.exists(), "configuration must not create directories"
    finally:
        app.clear_reservations()
