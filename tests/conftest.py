# -*- coding: utf-8 -*-
"""Shared pytest fixtures.

Keeps the test run hermetic: the generated-output manifest and the queue-time
path reservations are process-global state, so each test gets a clean slate and
the repository is never written to.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402

#: Project-local scratch root for the whole suite (git-ignored). One directory
#: rather than one per test: a LibreOffice profile is large, and the app already
#: creates a uniquely-named subdirectory inside whatever root it is given.
_TEST_SCRATCH = Path(__file__).resolve().parent.parent / ".pdfforge_test_tmp"
_TEST_SCRATCH.mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def isolate_global_state(tmp_path_factory, monkeypatch):
    """Redirect the output manifest to a temp file and clear reservations."""
    # The state store lives in per-user app data; point it at a temp dir so the
    # suite never touches real machine state (and never the checkout).
    state = tmp_path_factory.mktemp("pdfforge_state")
    monkeypatch.setenv("PDF_FORGE_STATE_DIR", str(state))
    # Keep the app's scratch inside the project too. A LibreOffice profile is a
    # ~200 MB tree that a killed run leaves behind, and in the user's %TEMP% it
    # is anonymous - nothing ties it back to the project that made it. Project
    # test artifacts belong under the project root, where the residue sweep can
    # see and attribute them.
    monkeypatch.setenv("PDF_FORGE_TEMP_DIR", str(_TEST_SCRATCH))
    app.clear_reservations()
    app.taskqueue._task_queue.clear()
    app.set_operation_prompt(None)
    yield
    app.clear_reservations()
    app.taskqueue._task_queue.clear()
