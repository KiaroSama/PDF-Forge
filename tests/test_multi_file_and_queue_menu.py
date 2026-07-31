# -*- coding: utf-8 -*-
"""Two UX contracts added after the batch-queue feedback.

1. Watermark removal accepts SEVERAL PDFs in one pass: paths are entered until
   'done', each file is then scanned and asked about on its own, and everything
   configured lands in ONE queued task. Skipping a file must not abandon the
   rest.
2. While tasks are queued, the main menu offers one extra option that starts
   them. Its number is derived from the menu length, so adding a tool later can
   never collide with it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from helpers import stamped_pdf  # noqa: E402


@pytest.fixture(autouse=True)
def _empty_queue():
    """Every test starts and ends with an empty queue."""
    app.taskqueue._task_queue.clear()
    yield
    app.taskqueue._task_queue.clear()


def _feed(monkeypatch, answers):
    """Drive every prompt through the real builtin (see prompts._input)."""
    supplied = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_a: next(supplied))


# --------------------------------------------------------------------------- #
# 1. Multi-file watermark removal
# --------------------------------------------------------------------------- #

def test_two_files_are_configured_in_one_pass(tmp_path, monkeypatch):
    """Two PDFs, one queued task, both sources verified by the queue."""
    first = stamped_pdf(tmp_path / "a.pdf", pages=3, pad=64)
    second = stamped_pdf(tmp_path / "b.pdf", pages=2, pad=64)
    # a, b, done | file a: candidate=Enter, output=Enter | file b: same.
    _feed(monkeypatch, [str(first), str(second), "", "", "", "", ""])

    with pytest.raises(app.taskqueue._TaskQueued):
        app.operation_remove_watermark()

    assert len(app.taskqueue._task_queue) == 1, (
        "several files must queue ONE task, not one task per file"
    )
    task = app.taskqueue._task_queue[0]
    assert len(task.sources) == 2, "both sources must be verified before the run"

    task.run()
    assert (tmp_path / "a_no_watermark.pdf").exists()
    assert (tmp_path / "b_no_watermark.pdf").exists()


def test_skipping_one_file_keeps_the_others(tmp_path, monkeypatch):
    """0 at the selection skips just that file; the rest still queue."""
    first = stamped_pdf(tmp_path / "a.pdf", pages=3, pad=64)
    second = stamped_pdf(tmp_path / "b.pdf", pages=2, pad=64)
    # a, b, done | file a: 0 (skip) | file b: candidate=Enter, output=Enter.
    _feed(monkeypatch, [str(first), str(second), "", "0", "", ""])

    with pytest.raises(app.taskqueue._TaskQueued):
        app.operation_remove_watermark()

    task = app.taskqueue._task_queue[0]
    assert len(task.sources) == 1, "the skipped file must not be queued"

    task.run()
    assert not (tmp_path / "a_no_watermark.pdf").exists(), "skipped file was written"
    assert (tmp_path / "b_no_watermark.pdf").exists()


def test_skipping_every_file_queues_nothing(tmp_path, monkeypatch):
    """All files skipped: back to the menu with an empty queue, no exception."""
    src = stamped_pdf(tmp_path / "a.pdf", pages=2, pad=64)
    _feed(monkeypatch, [str(src), "", "0"])  # a, done, then skip it

    app.operation_remove_watermark()  # returns cleanly

    assert app.taskqueue._task_queue == []


def test_one_file_still_queues_a_single_named_task(tmp_path, monkeypatch):
    """The single-file path keeps its descriptive summary."""
    src = stamped_pdf(tmp_path / "solo.pdf", pages=2, pad=64)
    _feed(monkeypatch, [str(src), "", "", ""])  # path, done, candidate, output

    with pytest.raises(app.taskqueue._TaskQueued):
        app.operation_remove_watermark()

    summary = app.taskqueue._task_queue[0].summary
    assert "solo.pdf" in summary and "solo_no_watermark.pdf" in summary


# --------------------------------------------------------------------------- #
# 2. The temporary "start the queue" main-menu option
# --------------------------------------------------------------------------- #

def test_queue_option_is_hidden_while_the_queue_is_empty(capsys):
    app.menus.show_menu()
    assert "queued task" not in capsys.readouterr().out.lower()


def test_queue_option_appears_with_the_next_free_number(capsys):
    """Shown only when something is queued, numbered after the last tool."""
    app.taskqueue._task_queue.append(
        app.taskqueue._QueuedTask("demo", lambda: None))
    app.menus.show_menu()
    out = capsys.readouterr().out

    expected = str(len(app.menus._MAIN_ITEMS) + 1)
    assert f"{expected}." in out, "the entry must use the first free menu number"
    assert "1 queued task(s)" in out


def test_the_number_follows_the_menu_length():
    """Adding a tool later must move the entry, never collide with it."""
    assert app.menus._RUN_QUEUE_CHOICE == str(len(app.menus._MAIN_ITEMS) + 1)
    assert app.menus._RUN_QUEUE_CHOICE not in (
        set(app.menus._SUBMENUS) | {str(i) for i in range(len(app.menus._MAIN_ITEMS) + 1)}
    ), "the queue entry must not shadow an existing menu number"


def test_choosing_it_runs_the_queue(monkeypatch, capsys):
    """The menu entry starts the batch instead of asking for another task."""
    ran = []
    app.taskqueue._task_queue.append(
        app.taskqueue._QueuedTask("demo", lambda: ran.append(True)))

    # Pick the queue option, confirm "Start now?", then exit the menu loop.
    _feed(monkeypatch, [app.menus._RUN_QUEUE_CHOICE, "y", "0"])
    app.menus.main_menu()

    assert ran == [True], "the queued task never ran"
    assert app.taskqueue._task_queue == [], "the queue must be empty afterwards"


def test_it_is_rejected_when_nothing_is_queued(monkeypatch, capsys):
    """With an empty queue the number is not a valid option."""
    _feed(monkeypatch, [app.menus._RUN_QUEUE_CHOICE, "0"])
    app.menus.main_menu()

    assert "Invalid option" in capsys.readouterr().out
