from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Optional

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403

__all__ = ['_utc_now', '_UtcFormatter', 'setup_logging']

# Generous on purpose: enough history to debug a report, small enough to stay
# out of packaging's way.
_MAX_LOG_FILES = 50


def _prune_old_logs(log_dir: Path, keep: int, current: Path) -> None:
    """Keep the newest ``keep`` log files; never touch the current run's file.

    One file per launch with no cap grew this directory to hundreds of entries
    in a portable checkout, and once broke wheel builds on any machine that had
    run the app (flat-layout discovery treated ``logs/`` as a package). It is
    best-effort: a failure here must never stop startup.
    """
    try:
        existing = sorted(
            (p for p in log_dir.glob("*.log") if p != current),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in existing[keep - 1:]:
            try:
                stale.unlink()
            except OSError:
                pass          # a locked file (another instance) is not an error
    except OSError:
        pass


def _utc_now() -> datetime.datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.datetime.now(datetime.timezone.utc)


class _UtcFormatter(logging.Formatter):
    """Formatter that renders timestamps as UTC, to the second, no milliseconds."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 (logging API name)
        dt = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def setup_logging(script_dir: Path) -> Optional[Path]:
    """Configure file + console logging.

    Creates a uniquely named UTC log file for every execution under ``logs/``.
    Returns the log file path, or ``None`` when persistent logging could not be
    initialized (in which case a console fallback is used).
    """
    log_dir = script_dir / "logs"
    safe_prefix = _sanitize_for_filename(LOG_PREFIX)
    timestamp = _utc_now().strftime("%Y-%m-%d_%H-%M-%S_UTC")

    logger.setLevel(logging.DEBUG)
    # Avoid duplicate handlers if setup runs more than once.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    log_path: Optional[Path] = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        base_name = f"{safe_prefix}_{timestamp}.log"
        log_path = log_dir / base_name
        # Collision-resistant suffix without altering the UTC timestamp format.
        counter = 2
        while log_path.exists():
            log_path = log_dir / f"{safe_prefix}_{timestamp}_{counter}.log"
            counter += 1

        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            _UtcFormatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        )
        logger.addHandler(file_handler)
        # Only now is this run's file both named and on disk, so the
        # "never delete the current file" rule can be enforced by identity.
        _prune_old_logs(log_dir, _MAX_LOG_FILES, log_path)
    except OSError as exc:
        # Console fallback; do not falsely claim a log file was created.
        print_error(f"Persistent logging unavailable: {exc}")
        log_path = None

    # Console handler kept quiet (warnings and above) to keep UX clean.
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.CRITICAL + 1)  # effectively silent
    logger.addHandler(console_handler)

    return log_path
