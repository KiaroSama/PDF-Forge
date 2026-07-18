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


@pytest.fixture(autouse=True)
def isolate_global_state(tmp_path_factory, monkeypatch):
    """Redirect the output manifest to a temp file and clear reservations."""
    # The state store lives in per-user app data; point it at a temp dir so the
    # suite never touches real machine state (and never the checkout).
    state = tmp_path_factory.mktemp("pdfforge_state")
    monkeypatch.setenv("PDF_FORGE_STATE_DIR", str(state))
    app.clear_reservations()
    app.taskqueue._task_queue.clear()
    app.set_operation_prompt(None)
    yield
    app.clear_reservations()
    app.taskqueue._task_queue.clear()
