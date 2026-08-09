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


# --------------------------------------------------------------------------- #
# 0 means "one prompt back" at EVERY prompt, yes/no questions included.
# --------------------------------------------------------------------------- #

def test_zero_at_a_yes_no_question_asks_to_go_back(monkeypatch):
    """`ask_yes_no` must signal back, not argue about y/n.

    It cannot say so in its return value - the bool has no spare state - so it
    raises. That is deliberate: a third return value would silently read as
    "no" at every call site not updated.
    """
    _feed(monkeypatch, ["0"])
    with pytest.raises(app.prompts._BackRequested):
        app.prompts.ask_yes_no("Anything?", default_yes=True)

    _feed(monkeypatch, ["back"])
    with pytest.raises(app.prompts._BackRequested):
        app.prompts.ask_yes_no("Anything?", default_yes=False)


def test_a_yes_no_prompt_advertises_back(monkeypatch):
    """The hint must say back=0. A prompt that hides it is how 0 got missed."""
    seen = []
    monkeypatch.setattr("builtins.input", lambda text="": (seen.append(text), "y")[1])
    app.prompts.ask_yes_no("Anything?")
    assert "back=0" in seen[0], f"the yes/no prompt does not offer back: {seen[0]!r}"


def test_back_inside_a_step_steps_back_rather_than_aborting(monkeypatch):
    """navigate_steps turns _BackRequested from a step into one step back."""
    visited = []

    def first():
        visited.append("first")
        return True

    def second():
        visited.append("second")
        if visited.count("second") == 1:
            raise app.prompts._BackRequested()
        return True

    assert app.prompts.navigate_steps([first, second]) is True
    assert visited == ["first", "second", "first", "second"], (
        f"back did not re-show the previous step: {visited}")


def test_zero_at_start_now_keeps_the_queue(monkeypatch, tmp_path):
    """The one that matters: 0 must NOT discard what you just configured.

    'n' at this prompt throws the whole queue away, so before this there was no
    non-destructive way out - worst for exactly the task worth keeping, the one
    that took the longest to set up.
    """
    ran = []
    # queue_task signals "configured, back to the menu" by raising; that is the
    # normal path, not an error.
    with pytest.raises(app.taskqueue._TaskQueued):
        app.taskqueue.queue_task("a long job", lambda: ran.append(True))
    assert app.taskqueue.queued_count() == 1

    _feed(monkeypatch, ["0"])
    exited = app.taskqueue.finalize_queue()

    assert exited is False, "0 is a step back, not an application exit"
    assert not ran, "0 must not start the queue"
    assert app.taskqueue.queued_count() == 1, (
        "the queued task was discarded by a back request")


def test_declining_at_start_now_still_discards(monkeypatch):
    """The historical 'n' behaviour is unchanged - back is a NEW third answer."""
    ran = []
    with pytest.raises(app.taskqueue._TaskQueued):
        app.taskqueue.queue_task("a job", lambda: ran.append(True))

    _feed(monkeypatch, ["n"])
    assert app.taskqueue.finalize_queue() is False
    assert not ran
    assert app.taskqueue.queued_count() == 0, "'n' must still discard the queue"


def test_every_prompt_in_the_package_offers_back():
    """0 means back at EVERY prompt - so no prompt may be added without one.

    This is a structural guard, not a behavioural one: it reads the source of
    every function that blocks on user input and requires a literal "0" in it.
    Without this, the next prompt someone adds silently becomes the one that
    rejects 0, which is exactly how ask_yes_no stayed inconsistent.
    """
    import ast
    import re

    package = Path(__file__).resolve().parent.parent / "pdf_forge"
    # _input/_timed_input are the plumbing every prompt reads THROUGH; they
    # decide nothing and have no answer to interpret.
    plumbing = {"_input", "_timed_input"}
    missing = []
    for source_file in sorted(package.glob("*.py")):
        text = source_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.FunctionDef) or node.name in plumbing:
                continue
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            if not re.search(r"\b_input\(|getpass\.getpass", body):
                continue
            if not re.search(r'["\']0["\']', body):
                missing.append(f"{source_file.name}:{node.lineno} {node.name}")

    assert not missing, (
        "these prompts read user input but never interpret '0' as back:\n  "
        + "\n  ".join(missing))
