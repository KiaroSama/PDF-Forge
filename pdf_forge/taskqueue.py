from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Sequence

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .pdf_io import SourceChangedError, SourceRef

__all__ = ['_TaskQueued', '_QueuedTask', 'queue_task', '_run_task_queue', 'finalize_queue',
           'queued_count']

class _TaskQueued(Exception):
    """Signal that an operation has been fully configured and queued.

    Raised after a task is added to the batch queue so any nested submenu
    unwinds back to the main menu, where the user is asked whether to queue
    another task or start the batch.
    """


@dataclass
class _QueuedTask:
    """A configured-but-not-yet-run operation.

    ``sources`` holds the identity of every file the task was configured
    against. The queue verifies them immediately before the runner starts, so
    an operation can never write an output derived from a file that was edited
    or replaced after the user configured it (C-06). Verification lives here,
    once, rather than in each of a dozen runners that would each have to
    remember it.
    """
    summary: str
    run: Callable[[], None]
    sources: Sequence["SourceRef"] = ()


_task_queue: List[_QueuedTask] = []


def queue_task(summary: str, run: Callable[[], None], sources=()) -> None:
    """Add a configured operation to the batch queue, then unwind to the main menu.

    Prints a short per-task line, appends the runner, and raises ``_TaskQueued``
    so nested submenus fall back to the main menu for the next choice. Output
    paths are resolved now, at queue time.

    ``sources`` are the :class:`SourceRef` identities this task depends on; the
    queue re-verifies them just before running it.
    """
    _task_queue.append(_QueuedTask(summary, run, tuple(sources)))
    print_success(f"\nAdded to queue (#{len(_task_queue)}): {summary}")
    logger.info("Task queued (#%d): %s", len(_task_queue), summary)
    raise _TaskQueued()


def queued_count() -> int:
    """How many tasks are waiting to run (0 when nothing is queued)."""
    return len(_task_queue)


def _discard_queue() -> None:
    """Empty the queue and release every path reservation it held.

    Called after the queue runs, when it is discarded at the Start confirmation,
    and when the user exits - so no reservation ever outlives its queue.
    """
    _task_queue.clear()
    clear_reservations()


def _verify_sources(task: _QueuedTask) -> None:
    """Prove every configured source is still the file the user chose."""
    for ref in task.sources:
        ref.verify_unchanged()


def _run_task_queue() -> None:
    """Execute every queued task in order, isolating per-task failures.

    Empties the queue and releases reservations when finished (each output has
    been written to disk by then, so on-disk uniqueness protects later runs).
    """
    count = len(_task_queue)
    print_heading(f"\nRunning {count} queued task(s)...")
    logger.info("Running task queue: %d task(s).", count)
    # Cleanup lives in an outer `finally` so the queue and its path reservations
    # are always released - including on SystemExit, GeneratorExit, or any other
    # BaseException that is not caught per task. The original exception keeps
    # propagating; cleanup never swallows or replaces it.
    try:
        # Durations report *work*, not wall time: a task that stops to ask for a
        # password would otherwise be credited with however long the user took to
        # type it, which would make every figure meaningless.
        with work_timer() as batch:
            for index, task in enumerate(_task_queue, start=1):
                print(colorize(
                    f"\n=== Task {index}/{count}: {task.summary} ===",
                    Color.BOLD + Color.LIGHT_BLUE,
                ))
                with work_timer() as spent:
                    try:
                        _verify_sources(task)
                        task.run()
                    except SourceChangedError as exc:
                        print_error(f"Task {index} skipped: {exc}")
                        logger.error("Queued task %d skipped: %s", index, exc)
                    except KeyboardInterrupt:
                        print_warning("\nTask interrupted; continuing with the next one.")
                        logger.warning("Queued task %d interrupted.", index)
                    except Exception as exc:  # noqa: BLE001 - one task must not sink the batch
                        print_error(f"Task {index} failed: {exc}")
                        logger.exception("Queued task %d failed.", index)
                # Printed for failures too: a four-minute failure is worth knowing.
                print_info(f"Took {format_duration(spent['seconds'])}.")
                logger.info("Task %d finished in %.2fs of work.", index, spent["seconds"])
        print_success(
            f"\nAll {count} queued task(s) processed "
            f"in {format_duration(batch['seconds'])}."
        )
        logger.info("Task queue finished: %d task(s) in %.2fs of work.",
                    count, batch["seconds"])
    finally:
        _discard_queue()


def finalize_queue() -> bool:
    """Show the full queue, confirm once, then run it (or discard on 'no').

    Empties the queue and releases path reservations afterwards either way.
    Does nothing when the queue is empty. Returns ``True`` when the user typed
    ``exit``/``quit`` at the Start confirmation - a deliberate exit, which the
    caller turns into a normal application shutdown (the queue is discarded
    cleanly first, never surfaced as an unexpected top-level error).
    """
    if not _task_queue:
        # An empty queue owns no tasks, but a configuration that was abandoned
        # before queueing (e.g. the converter install was declined after output
        # paths were reserved) can still hold reservations. Releasing here is
        # safe by construction: with no queued task, no reservation can be
        # referenced by anything (C-01).
        _discard_queue()
        return False
    print_heading(f"\nComplete summary - {len(_task_queue)} task(s) queued")
    for index, task in enumerate(_task_queue, start=1):
        print_kv(f"Task {index}", task.summary, Color.AQUA)

    keep = False
    try:
        try:
            start = ask_yes_no("\nStart now?", default_yes=True)
        except _BackRequested:
            # 0 = one step back: return to the menu with the queue INTACT. It is
            # the only non-destructive way out of this prompt - 'n' discards
            # everything - and it matters most for exactly the task worth
            # keeping, the one that took a long time to configure. The main menu
            # grows a "run the queue" entry while tasks are waiting, so nothing
            # is stranded.
            keep = True
            print_info(
                f"Kept {len(_task_queue)} queued task(s); start them from the "
                "main menu when you are ready."
            )
            logger.info("Queue kept via 0/back at the Start confirmation (%d task(s)).",
                        len(_task_queue))
            return False
        except _ExitRequested:
            print_warning("Exiting; the queued task(s) were discarded.")
            logger.info("Queue discarded via exit/quit at the Start confirmation.")
            return True

        if start:
            _run_task_queue()
        else:
            print_warning("Cancelled. Discarded the queued task(s).")
            logger.info(
                "Queue discarded at start confirmation (%d task(s)).",
                len(_task_queue),
            )
        return False
    finally:
        # Whatever happened - ran, cancelled, exited, or an unexpected
        # BaseException - the queue and its reservations are released here. The
        # single exception is 0/back, which exists precisely to keep them: the
        # reservations must outlive this call or a kept task could lose the
        # output name it was configured with.
        if not keep:
            _discard_queue()
