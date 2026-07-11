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


def _run_task_queue() -> None:
    """Execute every queued task in order, isolating per-task failures."""
    count = len(_task_queue)
    print_heading(f"\nRunning {count} queued task(s)...")
    logger.info("Running task queue: %d task(s).", count)
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


def finalize_queue() -> None:
    """Show the full queue, confirm once, then run it (or discard on 'no').

    Clears the queue afterwards either way. Does nothing when the queue is empty.
    """
    if not _task_queue:
        return
    print_heading(f"\nComplete summary - {len(_task_queue)} task(s) queued")
    for index, task in enumerate(_task_queue, start=1):
        print_kv(f"Task {index}", task.summary, Color.AQUA)

    if ask_yes_no("\nStart now?", default_yes=True):
        _run_task_queue()
    else:
        print_warning("Cancelled. Discarded the queued task(s).")
        logger.info(
            "Queue discarded at start confirmation (%d task(s)).", len(_task_queue)
        )
    _task_queue.clear()
