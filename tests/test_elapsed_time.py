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


class _FakeTime:
    """Stands in for the ``time`` module inside ``prompts``, clock-driven.

    These tests are about arithmetic - wall time minus input wait - so they
    drive the clock instead of sleeping. Real sleeps made the assertion a race
    against the OS scheduler: a 0.05s "work" sleep asserted under a 0.2s ceiling
    leaves 0.15s of slack, which a loaded machine can eat, and that produced a
    real failure. Advancing an explicit clock is both deterministic AND a
    stricter check, because the expected value is exact rather than a bound.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def clock(monkeypatch):
    """Replace the clock `prompts` reads, leaving the real one alone elsewhere."""
    fake = _FakeTime()
    monkeypatch.setattr(prompts, "time", fake)
    return fake


def _slow_answer(clock, answer: str):
    """A prompt reader that costs the wall clock what a human would."""
    def read(*_a, **_k) -> str:
        clock.advance(INPUT_WAIT)
        return answer
    return read


def test_input_wait_is_not_counted_as_work(monkeypatch, clock):
    """The whole point: waiting at a prompt is not work time."""
    monkeypatch.setattr("builtins.input", _slow_answer(clock, "typed"))

    with prompts.work_timer() as spent:
        assert prompts._input("Question: ") == "typed"
        clock.advance(REAL_WORK)

    assert spent["seconds"] == pytest.approx(REAL_WORK), (
        f"the {INPUT_WAIT}s spent waiting for the user was counted as work "
        f"({spent['seconds']:.3f}s recorded)"
    )


def test_getpass_wait_is_not_counted(monkeypatch, clock):
    """Hidden password prompts bypass ``input()`` - they must be timed too."""
    monkeypatch.setattr("getpass.getpass", _slow_answer(clock, "s3cret"))

    with prompts.work_timer() as spent:
        assert prompts.prompt_password() == "s3cret"
        clock.advance(REAL_WORK)

    assert spent["seconds"] == pytest.approx(REAL_WORK), (
        f"a hidden-password wait was counted as work "
        f"({spent['seconds']:.3f}s recorded)"
    )


def test_input_wait_is_charged_even_when_the_prompt_raises(monkeypatch, clock):
    """An exit request raised from a prompt must still not inflate the work."""
    def raiser(*_a, **_k):
        clock.advance(INPUT_WAIT)
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raiser)

    with prompts.work_timer() as spent:
        with pytest.raises(KeyboardInterrupt):
            prompts._input("Question: ")
        clock.advance(REAL_WORK)

    assert spent["seconds"] == pytest.approx(REAL_WORK), (
        f"the wait before the exception was counted as work "
        f"({spent['seconds']:.3f}s recorded)"
    )


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
