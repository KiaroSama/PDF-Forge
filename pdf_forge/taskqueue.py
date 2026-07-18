from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403

__all__ = ['_TaskQueued', '_QueuedTask', 'queue_task', '_run_task_queue', 'finalize_queue']

class _TaskQueued(Exception):
    """Signal that an operation has been fully configured and queued.

    Raised after a task is added to the batch queue so any nested submenu
    unwinds back to the main menu, where the user is asked whether to queue
    another task or start the batch.
    """


@dataclass
class _QueuedTask:
    """A configured-but-not-yet-run operation: a label plus a zero-arg runner."""
    summary: str
    run: Callable[[], None]


_task_queue: List[_QueuedTask] = []


def queue_task(summary: str, run: Callable[[], None]) -> None:
    """Add a configured operation to the batch queue, then unwind to the main menu.

    Prints a short per-task line, appends the runner, and raises ``_TaskQueued``
    so nested submenus fall back to the main menu for the next choice. Output
    paths are resolved now, at queue time.
    """
    _task_queue.append(_QueuedTask(summary, run))
    print_success(f"\nAdded to queue (#{len(_task_queue)}): {summary}")
    logger.info("Task queued (#%d): %s", len(_task_queue), summary)
    raise _TaskQueued()


def _discard_queue() -> None:
    """Empty the queue and release every path reservation it held.

    Called after the queue runs, when it is discarded at the Start confirmation,
    and when the user exits - so no reservation ever outlives its queue.
    """
    _task_queue.clear()
    clear_reservations()


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
        for index, task in enumerate(_task_queue, start=1):
            print(colorize(
                f"\n=== Task {index}/{count}: {task.summary} ===",
                Color.BOLD + Color.LIGHT_BLUE,
            ))
            try:
                task.run()
            except KeyboardInterrupt:
                print_warning("\nTask interrupted; continuing with the next one.")
                logger.warning("Queued task %d interrupted.", index)
            except Exception as exc:  # noqa: BLE001 - one task must not sink the batch
                print_error(f"Task {index} failed: {exc}")
                logger.exception("Queued task %d failed.", index)
        print_success(f"\nAll {count} queued task(s) processed.")
        logger.info("Task queue finished: %d task(s).", count)
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
        return False
    print_heading(f"\nComplete summary - {len(_task_queue)} task(s) queued")
    for index, task in enumerate(_task_queue, start=1):
        print_kv(f"Task {index}", task.summary, Color.AQUA)

    try:
        try:
            start = ask_yes_no("\nStart now?", default_yes=True)
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
        # BaseException - the queue and its reservations are released here.
        _discard_queue()
