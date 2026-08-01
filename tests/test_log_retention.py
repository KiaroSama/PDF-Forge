# -*- coding: utf-8 -*-
"""The log directory must stay bounded, without ever losing the current run.

PDF Forge writes one log file per launch and used to remove none, so a working
copy accumulated hundreds. That is not only clutter in a checkout users copy
between machines: flat-layout package discovery treated ``logs/`` as a
top-level package, so a wheel build failed on exactly the machines that had
actually run the app.

The cap is best-effort by design. Two properties are load-bearing and are what
these tests defend: the run's own log is never deleted (it is the evidence a
user is about to send you), and a prune failure never breaks startup.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf_forge import logsetup  # noqa: E402

KEEP = 50


def _make_logs(directory: Path, count: int) -> list:
    """``count`` log files, oldest first, with strictly increasing mtimes."""
    made = []
    for index in range(count):
        path = directory / f"pdf_forge_2026-08-01_00-00-{index:02d}_UTC.log"
        path.write_text(f"entry {index}\n", encoding="utf-8")
        # Explicit mtimes: writing 60 files can take less than the filesystem's
        # timestamp resolution, which would make "newest" ambiguous.
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
        made.append(path)
    return made


@pytest.fixture
def quiet_logger():
    """Restore the package logger, and close handlers so Windows can unlink."""
    logger = logging.getLogger("pdf_forge")
    saved = list(logger.handlers)
    saved_level = logger.level
    yield logger
    for handler in list(logger.handlers):
        if handler not in saved:
            logger.removeHandler(handler)
            handler.close()
    logger.handlers[:] = saved
    logger.setLevel(saved_level)


def test_prune_keeps_the_newest_and_removes_the_rest(tmp_path):
    files = _make_logs(tmp_path, 60)
    current = files[-1]          # this run's file is the newest

    logsetup._prune_old_logs(tmp_path, KEEP, current)

    remaining = sorted(tmp_path.glob("*.log"))
    assert len(remaining) == KEEP, [p.name for p in remaining]
    assert remaining == sorted(files[-KEEP:]), "the survivors are not the newest 50"


def test_prune_never_removes_the_current_file(tmp_path):
    """The run's own log is the one file a prune may never take."""
    files = _make_logs(tmp_path, 60)
    current = files[0]           # deliberately the OLDEST file

    logsetup._prune_old_logs(tmp_path, KEEP, current)

    assert current.exists(), "the prune deleted the log this run is writing to"
    assert len(list(tmp_path.glob("*.log"))) == KEEP


def test_prune_survives_an_unremovable_file(tmp_path, monkeypatch):
    """A file locked by a concurrent instance is skipped, not an error."""
    files = _make_logs(tmp_path, 60)
    current = files[-1]
    locked = files[0]            # oldest, so it is inside the delete range

    real_unlink = Path.unlink

    def refusing_unlink(self, *args, **kwargs):
        if self == locked:
            raise OSError(32, "The process cannot access the file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)

    logsetup._prune_old_logs(tmp_path, KEEP, current)   # must not raise

    assert locked.exists(), "the locked file should have been skipped"
    remaining = sorted(tmp_path.glob("*.log"))
    # Everything else in the delete range still went: only the locked one stayed.
    assert len(remaining) == KEEP + 1, [p.name for p in remaining]


def test_prune_survives_a_file_that_vanishes_mid_scan(tmp_path, monkeypatch):
    """A concurrent instance may delete a file between the glob and the stat.

    Without the prune's own outer guard that FileNotFoundError escapes into
    startup. Nothing else in this module exercises that path, so removing the
    guard used to leave every test green.
    """
    files = _make_logs(tmp_path, 60)
    current = files[-1]
    vanishing = files[5]

    real_stat = Path.stat

    def racing_stat(self, *args, **kwargs):
        if self == vanishing:
            raise FileNotFoundError(2, "No such file or directory")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", racing_stat)

    logsetup._prune_old_logs(tmp_path, KEEP, current)   # must not raise


def test_setup_logging_still_works_when_pruning_fails(tmp_path, quiet_logger,
                                                      monkeypatch):
    """Startup must not depend on housekeeping succeeding."""
    def exploding_prune(*args, **kwargs):
        raise OSError("prune blew up")

    monkeypatch.setattr(logsetup, "_prune_old_logs", exploding_prune)

    logsetup.setup_logging(tmp_path)          # must not raise

    written = list((tmp_path / "logs").glob("*.log"))
    assert len(written) == 1, "setup_logging did not create this run's log file"
