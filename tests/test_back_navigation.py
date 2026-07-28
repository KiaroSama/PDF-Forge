# -*- coding: utf-8 -*-
"""Back navigation: ``0`` steps back exactly ONE prompt, not to the menu.

Every multi-step operation used to abort to the menu the moment any prompt
returned ``None`` (a ``0``/back entry). The control hint says ``back=0``, so the
user reasonably expects ``0`` to reveal the *previous* question; only ``0`` at
the very first step returns to the menu.

These tests drive the real prompts through the single input reader
(``prompts._input``) and prove: (a) ``0`` at a later step re-shows the previous
prompt (and the operation still completes), and (b) ``0`` at the first step
returns to the menu with no task queued.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from pdf_forge import core  # noqa: E402
from helpers import make_pdf  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    core.clear_reservations()
    app.taskqueue._task_queue.clear()
    yield
    core.clear_reservations()
    app.taskqueue._task_queue.clear()


def _feed(monkeypatch, answers):
    """Feed successive answers to every prompt that reads through _input.

    Patches ``builtins.input`` (which every ``prompts._input`` ultimately calls)
    so inline ``_input`` loops in the ops modules are driven too, not only the
    named prompt helpers in prompts.py.
    """
    supplied = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a: next(supplied))


def _count_calls(monkeypatch, module, name):
    """Wrap ``module.name`` with a call counter that still runs the original."""
    original = getattr(module, name)
    calls = {"n": 0}

    def wrapper(*a, **k):
        calls["n"] += 1
        return original(*a, **k)

    monkeypatch.setattr(module, name, wrapper)
    return calls


# --------------------------------------------------------------------------- #
# PDF -> images (all pages): source -> quality -> output
# --------------------------------------------------------------------------- #

def test_output_back_returns_to_quality(tmp_path, monkeypatch):
    """0 at the output step re-shows the quality prompt; the op still queues."""
    src = make_pdf(tmp_path / "doc.pdf", 3)
    quality = _count_calls(monkeypatch, app.ops_convert, "prompt_image_quality")
    # source, quality=1, output=0 (back), quality=2, output=Enter (default).
    _feed(monkeypatch, [str(src), "1", "0", "2", ""])

    with pytest.raises(app.taskqueue._TaskQueued):
        app.ops_convert.operation_images_all_pages()

    assert quality["n"] == 2, "0 at output must re-show the quality prompt"
    assert app.taskqueue._task_queue, "the operation must still queue after stepping back"


def test_quality_back_returns_to_source(tmp_path, monkeypatch):
    """0 at the quality step re-shows the source prompt."""
    src = make_pdf(tmp_path / "doc.pdf", 2)
    source_calls = _count_calls(monkeypatch, app.ops_convert, "prompt_source_pdf")
    # source, quality=0 (back), source again, quality=1, output=Enter.
    _feed(monkeypatch, [str(src), "0", str(src), "1", ""])

    with pytest.raises(app.taskqueue._TaskQueued):
        app.ops_convert.operation_images_all_pages()

    assert source_calls["n"] == 2, "0 at quality must re-show the source prompt"


def test_source_back_returns_to_menu(tmp_path, monkeypatch):
    """0 at the first step returns to the menu: no task, no exception."""
    _feed(monkeypatch, ["0"])
    app.ops_convert.operation_images_all_pages()  # returns cleanly
    assert not app.taskqueue._task_queue, "0 at the first step must not queue anything"


# --------------------------------------------------------------------------- #
# PDF -> images (selected): source -> quality -> selection -> output (4 steps)
# --------------------------------------------------------------------------- #

def test_selected_output_back_returns_to_selection(tmp_path, monkeypatch):
    """0 at output re-shows the page-selection prompt (not the menu); op queues."""
    src = make_pdf(tmp_path / "doc.pdf", 3)
    # source, quality=1, selection="1", output=0 (back), selection="2", output=Enter.
    _feed(monkeypatch, [str(src), "1", "1", "0", "2", ""])

    with pytest.raises(app.taskqueue._TaskQueued):
        app.ops_convert.operation_images_selected_pages()

    assert app.taskqueue._task_queue, "stepping back from output must still let the op queue"


# --------------------------------------------------------------------------- #
# A deliberate cancel (protection "No") returns to the MENU, not one step back.
# --------------------------------------------------------------------------- #

def test_protection_cancel_returns_to_menu(tmp_path, monkeypatch):
    """resolve_protection cancel aborts the whole op (menu), never a step back."""
    src = make_pdf(tmp_path / "doc.pdf", 2)
    # Simulate the owner-restricted "create unprotected? No" cancel.
    monkeypatch.setattr(app.ops_convert, "resolve_protection", lambda *a, **k: None)
    _feed(monkeypatch, [str(src), "1"])  # source, quality; cancel fires before output

    app.ops_convert.operation_pdf_to_image_pdf()  # returns cleanly, no exception
    assert not app.taskqueue._task_queue, "a deliberate cancel must not queue anything"


# --------------------------------------------------------------------------- #
# Rolled-out operations in ops_pages / ops_compress: one step back per 0.
# --------------------------------------------------------------------------- #

def test_extract_output_back_returns_to_selection(tmp_path, monkeypatch):
    """Extract pages: 0 at output re-shows the page-selection prompt."""
    src = make_pdf(tmp_path / "doc.pdf", 3)
    # source, selection="1", output=0 (back to selection), selection="2", output=Enter.
    _feed(monkeypatch, [str(src), "1", "0", "2", ""])

    with pytest.raises(app.taskqueue._TaskQueued):
        app.ops_pages.operation_extract_pages()

    assert app.taskqueue._task_queue, "stepping back from output must still let extract queue"


def test_split_end_page_back_returns_to_start_page(tmp_path, monkeypatch):
    """Split: 0 at the end-page prompt steps back to the start-page prompt.

    Exercises the deep 5-step chain (source→chunk→start→end→output).
    """
    src = make_pdf(tmp_path / "doc.pdf", 6)
    source_calls = _count_calls(monkeypatch, app.ops_pages, "prompt_source_pdf")
    # source, chunk="2", start="1", end=0 (back to start), start="1", end="3", output=Enter.
    _feed(monkeypatch, [str(src), "2", "1", "0", "1", "3", ""])

    with pytest.raises(app.taskqueue._TaskQueued):
        app.ops_pages.operation_split_chunks()

    # Backing end→start must NOT re-open the source (only one source prompt).
    assert source_calls["n"] == 1, "a mid-wizard back must not re-run the source step"
    assert app.taskqueue._task_queue, "split must queue after stepping back one prompt"


def test_delete_single_output_back_returns_to_selection(tmp_path, monkeypatch):
    """Delete pages (single): 0 at output re-shows the page-selection prompt."""
    src = make_pdf(tmp_path / "doc.pdf", 3)
    # source, delete="1", output=0 (back to selection), delete="2", output=Enter.
    _feed(monkeypatch, [str(src), "1", "0", "2", ""])

    with pytest.raises(app.taskqueue._TaskQueued):
        app.ops_pages.operation_delete_pages_single()

    assert app.taskqueue._task_queue, "delete must queue after stepping back from output"


def test_compress_level_back_returns_to_source(tmp_path, monkeypatch):
    """Compress: 0 at the level prompt steps back to the source prompt."""
    src = make_pdf(tmp_path / "doc.pdf", 3)
    source_calls = _count_calls(monkeypatch, app.ops_compress, "prompt_source_pdf")
    # source, level=0 (back to source), source again, level="6" (ultra), output=Enter.
    _feed(monkeypatch, [str(src), "0", str(src), "6", ""])

    with pytest.raises(app.taskqueue._TaskQueued):
        app.ops_compress.operation_compress_pdf()

    assert source_calls["n"] == 2, "0 at the level prompt must re-show the source prompt"
