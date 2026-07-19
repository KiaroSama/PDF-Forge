# -*- coding: utf-8 -*-
"""Queue regressions: document handles, task lifecycle, and folder/batch
runs not reprocessing their own output.

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
# A5 / A14 - handles and queue lifecycle
# --------------------------------------------------------------------------- #

def test_close_doc_is_idempotent(tmp_path):
    doc = app.open_source_pdf(make_pdf(tmp_path / "p.pdf", 1))
    app.close_doc(doc)
    app.close_doc(doc)          # must not raise
    app.close_doc(None)


def test_source_file_can_be_removed_after_use(tmp_path):
    """Windows: a leaked handle would make this fail with a sharing violation."""
    src = make_pdf(tmp_path / "s.pdf", 2)
    doc = app.open_source_pdf(src)
    out = tmp_path / "o.pdf"
    app.write_pages_to_pdf(doc, [0], out)
    app.close_doc(doc)
    src.rename(tmp_path / "renamed.pdf")
    (tmp_path / "renamed.pdf").unlink()
    assert not src.exists()


def test_exit_at_start_confirmation_is_a_clean_exit(tmp_path, monkeypatch):
    app.taskqueue._task_queue.clear()
    app.clear_reservations()
    app.taskqueue._task_queue.append(app.taskqueue._QueuedTask("t", lambda: None))
    app.reserve_unique_file(tmp_path / "reserved.pdf")

    monkeypatch.setattr(app.prompts, "_input", lambda _p: "exit")
    exited = app.finalize_queue()

    assert exited is True, "exit must be reported, never raised to the top level"
    assert not app.taskqueue._task_queue
    assert not app.core._reserved_files


def test_declining_start_discards_and_releases(tmp_path, monkeypatch):
    app.taskqueue._task_queue.clear()
    app.clear_reservations()
    ran = {"n": 0}
    app.taskqueue._task_queue.append(
        app.taskqueue._QueuedTask("t", lambda: ran.__setitem__("n", 1))
    )
    app.reserve_unique_file(tmp_path / "reserved.pdf")

    monkeypatch.setattr(app.prompts, "_input", lambda _p: "n")
    assert app.finalize_queue() is False
    assert ran["n"] == 0
    assert not app.taskqueue._task_queue
    assert not app.core._reserved_files


def test_running_queue_releases_reservations(tmp_path, monkeypatch):
    app.taskqueue._task_queue.clear()
    app.clear_reservations()
    ran = {"n": 0}
    app.taskqueue._task_queue.append(
        app.taskqueue._QueuedTask("t", lambda: ran.__setitem__("n", 1))
    )
    app.reserve_unique_file(tmp_path / "reserved.pdf")

    monkeypatch.setattr(app.prompts, "_input", lambda _p: "y")
    assert app.finalize_queue() is False
    assert ran["n"] == 1
    assert not app.taskqueue._task_queue
    assert not app.core._reserved_files


def test_empty_queue_finalize_is_a_noop():
    app.taskqueue._task_queue.clear()
    assert app.finalize_queue() is False


def test_password_never_appears_in_a_task_repr():
    task = app.taskqueue._QueuedTask("Protect secret.pdf -> secret_protected.pdf",
                                     lambda: None)
    assert "hunter2" not in repr(task)
    assert "user_pw" not in repr(task)


# --------------------------------------------------------------------------- #
# A6 - folder/batch runs must not reprocess their own outputs
# --------------------------------------------------------------------------- #

def test_generated_outputs_are_excluded_from_folder_discovery(tmp_path):
    """A second folder run must not pick up the first run's output."""
    app.forget_generated_outputs()
    try:
        source = make_pdf(tmp_path / "book.pdf", 2)
        assert [p.name for p in app.discover_pdfs_in_folder(tmp_path)] == ["book.pdf"]

        # Simulate a tool writing its result beside the source.
        doc = app.open_source_pdf(source)
        out = tmp_path / "book_compressed.pdf"
        app.write_pages_to_pdf(doc, [0], out)
        app.close_doc(doc)
        assert out.exists()

        names = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
        assert names == ["book.pdf"], "our own output must not be rediscovered"

        # The escape hatch still sees everything.
        every = [p.name for p in app.discover_pdfs_in_folder(tmp_path,
                                                             include_generated=True)]
        assert sorted(every) == ["book.pdf", "book_compressed.pdf"]
    finally:
        app.forget_generated_outputs()


def test_exclusion_is_by_exact_path_not_by_name_substring(tmp_path):
    """A user's own file named like an output must still be processed."""
    app.forget_generated_outputs()
    try:
        # The user genuinely owns this file; we never generated it.
        user_file = make_pdf(tmp_path / "holiday_compressed.pdf", 1)
        make_pdf(tmp_path / "plain.pdf", 1)
        names = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
        assert "holiday_compressed.pdf" in names, "substring matching would hide this"
        assert user_file.exists()
    finally:
        app.forget_generated_outputs()


def test_manifest_forgets_deleted_outputs(tmp_path):
    app.forget_generated_outputs()
    try:
        generated = make_pdf(tmp_path / "gen.pdf", 1)
        app.record_generated_output(generated)
        assert app.discover_pdfs_in_folder(tmp_path) == []

        # The user deletes our output and puts their own, different file there.
        generated.unlink()
        make_pdf(tmp_path / "gen.pdf", 5)
        names = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
        assert names == ["gen.pdf"], "a replaced path is a normal source again"
    finally:
        app.forget_generated_outputs()


def test_recording_is_idempotent(tmp_path):
    app.forget_generated_outputs()
    try:
        generated = make_pdf(tmp_path / "g.pdf", 1)
        for _ in range(3):
            app.record_generated_output(generated)
        assert len(app.load_generated_outputs()) == 1
    finally:
        app.forget_generated_outputs()
