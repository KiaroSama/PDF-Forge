# -*- coding: utf-8 -*-
"""Prompt regressions: hierarchical numbering and path-guidance text.

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
# D - hierarchical prompt numbering
# --------------------------------------------------------------------------- #

def test_retries_advance_only_the_local_counter():
    app.set_operation_prompt("1")
    labels = [label_of(app.question_prompt(f"PDF file #{n}")) for n in (1, 2, 2, 2)]
    assert labels == ["1-1", "1-2", "1-3", "1-4"]


def test_prefix_follows_the_selected_submenu_item():
    app.set_operation_prompt("2")
    assert label_of(app.question_prompt("Folder")) == "2-1"
    assert label_of(app.question_prompt("Output")) == "2-2"
    app.set_operation_prompt("7")
    assert label_of(app.question_prompt("Source")) == "7-1"


def test_counter_resets_when_operation_restarts():
    app.set_operation_prompt("1")
    app.question_prompt("a")
    app.question_prompt("b")
    app.set_operation_prompt("1")
    assert label_of(app.question_prompt("a")) == "1-1"


def test_menu_and_queue_prompts_use_plain_numbering():
    app.set_operation_prompt(None)
    assert label_of(app.question_prompt("Start now?")) == "1"
    assert label_of(app.question_prompt("Queue another?")) == "2"


def test_reset_questions_keeps_the_prefix():
    app.set_operation_prompt("3")
    app.question_prompt("x")
    app.reset_questions()
    assert label_of(app.question_prompt("y")) == "3-1"


def test_nested_helper_prompts_stay_in_the_same_context():
    """Custom quality / confirmations continue the operation's numbering."""
    app.set_operation_prompt("1")
    app.question_prompt("Source PDF path")
    app.question_prompt("Output image quality")
    assert label_of(app.question_prompt("Custom DPI")) == "1-3"


# --------------------------------------------------------------------------- #
# E - path prompt guidance
# --------------------------------------------------------------------------- #

def test_drag_drop_guidance_exact_text():
    assert app.drag_drop_guidance() == "drag and drop a file here or paste a path"
    assert app.drag_drop_guidance(kind="folder") == (
        "drag and drop a folder here or paste a path"
    )
    assert app.drag_drop_guidance(repeated=True) == (
        "drag and drop a file here or paste a path; b=re-enter previous file; "
        "type done when finished"
    )


def test_pdf_file_prompt_exact_rendering_without_colour():
    """The documented multi-file prompt line, with colours disabled."""
    app.set_operation_prompt("1")
    app.question_prompt("PDF file #1")
    prompt = app.question_prompt(
        "PDF file #2",
        details=app.guidance_text(app.drag_drop_guidance(repeated=True),
                                  app.GUIDANCE_KEYWORDS),
    )
    assert prompt == (
        "\n1-2. PDF file #2 (drag and drop a file here or paste a path; "
        "b=re-enter previous file; type done when finished) {back=0, quit=exit}: "
    )
    assert "[" not in prompt, "a multi-file prompt carries no default marker"


def test_folder_prompt_exact_rendering():
    """A folder prompt gets the short guidance: no previous file, nothing to finish."""
    app.set_operation_prompt("2")
    prompt = app.question_prompt(
        "Folder containing PDFs",
        details=app.guidance_text(app.drag_drop_guidance(kind="folder"),
                                  app.GUIDANCE_KEYWORDS),
    )
    assert prompt == (
        "\n2-1. Folder containing PDFs (drag and drop a folder here or paste a "
        "path) {back=0, quit=exit}: "
    )


def test_guidance_colouring_matches_ffmwiz_split(monkeypatch):
    """Hint-coloured body, typeable keywords picked out in light blue."""
    monkeypatch.setattr(app.ui, "_COLOR_ENABLED", True)
    coloured = app.guidance_text(app.drag_drop_guidance(repeated=True),
                                 app.GUIDANCE_KEYWORDS)
    assert app.Color.LIGHT_BLUE + "b=" in coloured
    assert app.Color.LIGHT_BLUE + "done" in coloured
    # Each highlight restores the hint colour so the tail is not left plain.
    assert coloured.count(app.Color.HINT_YELLOW) == len(app.GUIDANCE_KEYWORDS)
    assert "drag and drop a file here" in coloured
    # With colour off the guidance is exactly the plain text.
    monkeypatch.setattr(app.ui, "_COLOR_ENABLED", False)
    assert app.guidance_text("plain text", app.GUIDANCE_KEYWORDS) == "plain text"


def test_quoted_and_unicode_paths_are_accepted(tmp_path):
    persian = tmp_path / "پوشه" / "سند.pdf"
    assert app.strip_surrounding_quotes('"' + str(persian) + '"') == str(persian)
    assert app.strip_surrounding_quotes("'" + str(persian) + "'") == str(persian)


def test_guidance_is_defined_once_not_duplicated():
    """The literal lives in core only, so prompts cannot drift apart."""
    package = Path(app.__file__).resolve().parent
    hits = [
        path.name for path in package.glob("*.py")
        if "drag and drop a" in path.read_text(encoding="utf-8")
    ]
    assert hits == ["core.py"], hits
