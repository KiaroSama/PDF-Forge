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
    manifest = tmp_path_factory.mktemp("pdfforge_state") / "outputs.json"
    monkeypatch.setattr(app.core, "_manifest_path", lambda: manifest)
    app.clear_reservations()
    app.taskqueue._task_queue.clear()
    app.set_operation_prompt(None)
    yield
    app.clear_reservations()
    app.taskqueue._task_queue.clear()
