# -*- coding: utf-8 -*-
"""Elapsed-work reporting: the reported time is WORK, never input waiting.

A naive ``end - start`` would credit a task with however long the user took to
answer a password prompt, reporting a five-minute conversion as forty minutes.
Every blocking read therefore routes through ``prompts._timed_input``, which
charges its duration to a process-wide accumulator that ``work_timer``
subtracts back out.

The load-bearing test is ``test_input_wait_is_not_counted_as_work``: it is the
only one that distinguishes this feature from a plain stopwatch.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from pdf_forge import core, prompts  # noqa: E402

INPUT_WAIT = 0.3   # what the fake human "takes" to answer
REAL_WORK = 0.05   # what the app actually does


@pytest.fixture(autouse=True)
def _clean_state():
    core.clear_reservations()
    app.taskqueue._task_queue.clear()
    yield
    core.clear_reservations()
    app.taskqueue._task_queue.clear()


def _slow_answer(answer: str):
    """A prompt reader that blocks like a human deciding what to type."""
    def read(*_a, **_k) -> str:
        time.sleep(INPUT_WAIT)
        return answer
    return read


def test_input_wait_is_not_counted_as_work(monkeypatch):
    """The whole point: waiting at a prompt is not work time."""
    monkeypatch.setattr("builtins.input", _slow_answer("typed"))

    with prompts.work_timer() as spent:
        assert prompts._input("Question: ") == "typed"
        time.sleep(REAL_WORK)

    assert spent["seconds"] < 0.2, (
        f"the {INPUT_WAIT}s spent waiting for the user was counted as work "
        f"({spent['seconds']:.3f}s recorded)"
    )
    assert spent["seconds"] >= 0.04, (
        f"the real work was not counted at all ({spent['seconds']:.3f}s)"
    )


def test_getpass_wait_is_not_counted(monkeypatch):
    """Hidden password prompts bypass ``input()`` - they must be timed too."""
    monkeypatch.setattr("getpass.getpass", _slow_answer("s3cret"))

    with prompts.work_timer() as spent:
        assert prompts.prompt_password() == "s3cret"
        time.sleep(REAL_WORK)

    assert spent["seconds"] < 0.2, (
        f"a hidden-password wait was counted as work "
        f"({spent['seconds']:.3f}s recorded)"
    )
    assert spent["seconds"] >= 0.04


def test_input_wait_is_charged_even_when_the_prompt_raises(monkeypatch):
    """An exit request raised from a prompt must still not inflate the work."""
    def raiser(*_a, **_k):
        time.sleep(INPUT_WAIT)
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raiser)

    with prompts.work_timer() as spent:
        with pytest.raises(KeyboardInterrupt):
            prompts._input("Question: ")
        time.sleep(REAL_WORK)

    assert spent["seconds"] < 0.2, f"{spent['seconds']:.3f}s recorded"


@pytest.mark.parametrize("seconds, expected", [
    (0.84, "0.8s"),
    (12.44, "12.4s"),
    (63, "1m 03s"),
    (3672, "1h 01m 12s"),
    (-5, "0.0s"),
])
def test_format_duration_shapes(seconds, expected):
    assert core.format_duration(seconds) == expected


def test_queue_prints_a_duration_line(capsys):
    app.taskqueue._task_queue.append(
        app.taskqueue._QueuedTask("trivial task", lambda: None)
    )
    app.taskqueue._run_task_queue()

    out = capsys.readouterr().out
    assert "Took " in out, "no per-task duration line was printed"
    assert "processed in " in out, "no batch duration was printed"


def test_failed_task_still_reports_its_duration(capsys):
    def boom() -> None:
        raise RuntimeError("task exploded")

    app.taskqueue._task_queue.append(app.taskqueue._QueuedTask("failing", boom))
    app.taskqueue._task_queue.append(app.taskqueue._QueuedTask("ok", lambda: None))
    app.taskqueue._run_task_queue()

    out = capsys.readouterr().out
    assert "task exploded" in out, "the failure was not reported"
    assert out.count("Took ") == 2, (
        "a failed task must still report how long it took before the batch "
        "moved on"
    )
    assert "processed in " in out
