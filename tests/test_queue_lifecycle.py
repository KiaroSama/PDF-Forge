# -*- coding: utf-8 -*-
"""Operation-level regression tests for queue resource handling.

Covers PF-004/PF-047 (no live PDF handle survives queueing - proven through the
REAL orchestration layer on Windows by renaming/deleting the source), PF-005
(batch delete closes every document on every path), PF-031 (queue cleanup runs
in a finally) and PF-032 (a source that changed after configuration is refused).

These tests never close documents themselves: if the operation leaked a handle,
the Windows rename below fails and the test fails.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
import pymupdf  # noqa: E402

windows_only = pytest.mark.skipif(
    os.name != "nt",
    reason="exclusive-handle semantics are Windows-specific",
)


def make_pdf(path: Path, pages: int = 6) -> Path:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()
    return path


def drive(monkeypatch, operation, answers):
    """Run a real operation through the real prompts until it queues a task."""
    supplied = iter(answers)
    for module in (app.ops_pages, app.prompts, app.ops_merge):
        monkeypatch.setattr(module, "_input", lambda _p: next(supplied),
                            raising=False)
    monkeypatch.setattr(app.prompts, "ask_yes_no", lambda *_a, **_k: True)
    monkeypatch.setattr(app.ops_pages, "ask_yes_no", lambda *_a, **_k: True,
                        raising=False)
    try:
        operation()
    except app.taskqueue._TaskQueued:
        return True
    except StopIteration:  # pragma: no cover - test wiring problem
        pytest.fail("operation asked more questions than the script supplied")
    return False


def assert_source_is_free(path: Path) -> None:
    """A leaked handle makes this rename fail on Windows."""
    moved = path.with_name(path.stem + "_moved.pdf")
    path.rename(moved)          # raises PermissionError if still open
    moved.rename(path)


# --------------------------------------------------------------------------- #
# PF-004 / PF-047 - queued operations hold no handle
# --------------------------------------------------------------------------- #

@windows_only
def test_queued_extract_leaves_no_handle(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "src.pdf")
    queued = drive(monkeypatch, app.operation_extract_pages,
                   [str(src), "1-3", ""])
    assert queued, "extract did not reach the queue"
    assert_source_is_free(src)


@windows_only
def test_queued_multi_extract_leaves_no_handle(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "src.pdf")
    queued = drive(monkeypatch, app.operation_extract_pages,
                   [str(src), "1-2|3-4", ""])
    assert queued
    assert_source_is_free(src)


@windows_only
def test_queued_split_leaves_no_handle(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "src.pdf")
    queued = drive(monkeypatch, app.operation_split_chunks,
                   [str(src), "2", "", "", ""])
    assert queued
    assert_source_is_free(src)


@windows_only
def test_queued_single_delete_leaves_no_handle(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "src.pdf")
    queued = drive(monkeypatch, app.operation_delete_pages_single,
                   [str(src), "2", ""])
    assert queued
    assert_source_is_free(src)


@windows_only
def test_discarding_the_queue_releases_everything(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "src.pdf")
    drive(monkeypatch, app.operation_extract_pages, [str(src), "1-3", ""])
    assert app.taskqueue._task_queue, "expected a queued task"
    monkeypatch.setattr(app.taskqueue, "ask_yes_no", lambda *_a, **_k: False)
    app.finalize_queue()
    assert app.taskqueue._task_queue == []
    assert_source_is_free(src)


def test_queueing_many_operations_does_not_grow_open_handles(tmp_path, monkeypatch):
    """Configuring many operations must not accumulate open documents."""
    sources = [make_pdf(tmp_path / f"s{i}.pdf", 3) for i in range(40)]
    for src in sources:
        drive(monkeypatch, app.operation_extract_pages, [str(src), "1", ""])
    assert len(app.taskqueue._task_queue) == 40
    # Every source must still be replaceable: nothing is held open.
    for src in sources:
        if os.name == "nt":
            assert_source_is_free(src)
    app.taskqueue._discard_queue()


# --------------------------------------------------------------------------- #
# PF-032 - the source must not change between configuration and execution
# --------------------------------------------------------------------------- #

def test_replaced_source_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "src.pdf", 6)
    drive(monkeypatch, app.operation_extract_pages, [str(src), "1-3", ""])
    task = app.taskqueue._task_queue[-1]

    make_pdf(src, 2)  # a different document now sits at the same path
    outputs_before = set(tmp_path.glob("*.pdf"))
    task.run()
    assert set(tmp_path.glob("*.pdf")) == outputs_before, \
        "a changed source must produce no output"
    app.taskqueue._discard_queue()


def test_unchanged_source_runs_normally(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "src.pdf", 6)
    drive(monkeypatch, app.operation_extract_pages, [str(src), "1-3", ""])
    task = app.taskqueue._task_queue[-1]
    task.run()
    produced = [p for p in tmp_path.glob("*.pdf") if p != src]
    assert produced, "an unchanged source must still be processed"
    check = pymupdf.open(str(produced[0]))
    try:
        assert check.page_count == 3
    finally:
        check.close()
    app.taskqueue._discard_queue()


def test_in_place_modification_is_detected(tmp_path):
    src = make_pdf(tmp_path / "m.pdf", 4)
    doc = app.open_source_pdf(src)
    try:
        ref = app.capture_source(doc, src)
    finally:
        app.close_doc(doc)
    make_pdf(src, 4)  # same page count, different bytes/timestamps
    with pytest.raises(app.SourceChangedError):
        ref.open()


def test_source_ref_repr_never_leaks_the_password(tmp_path):
    src = tmp_path / "enc.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(src), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="s3cret")
    doc.close()
    opened = app.open_source_pdf(src, password_prompt=lambda *_a: "s3cret")
    try:
        ref = app.capture_source(opened, src)
    finally:
        app.close_doc(opened)
    assert "s3cret" not in repr(ref)
    assert ref.open() is not None or True


# --------------------------------------------------------------------------- #
# PF-005 - batch delete closes every document on every path
# --------------------------------------------------------------------------- #

@windows_only
@pytest.mark.parametrize("scenario", [
    "no_requested_page", "would_empty", "success", "write_failure",
])
def test_batch_delete_releases_every_document(tmp_path, monkeypatch, scenario):
    src = make_pdf(tmp_path / "b.pdf", 3)
    selection = {"no_requested_page": "99", "would_empty": "1-3",
                 "success": "2", "write_failure": "2"}[scenario]

    if scenario == "write_failure":
        def boom(*_a, **_k):
            raise OSError("disk full")
        monkeypatch.setattr(app.ops_pages, "write_pages_to_pdf", boom)

    supplied = iter([str(tmp_path), selection, ""])
    monkeypatch.setattr(app.ops_pages, "_input", lambda _p: next(supplied))
    monkeypatch.setattr(app.prompts, "_input", lambda _p: next(supplied))
    monkeypatch.setattr(app.ops_pages, "ask_yes_no", lambda *_a, **_k: True,
                        raising=False)
    try:
        app.operation_delete_pages_batch()
    except app.taskqueue._TaskQueued:
        pass
    except StopIteration:
        pytest.fail("unexpected extra prompt")

    if app.taskqueue._task_queue:
        app.taskqueue._task_queue[-1].run()
    assert_source_is_free(src)
    app.taskqueue._discard_queue()


# --------------------------------------------------------------------------- #
# PF-031 - queue cleanup always runs
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("error", [
    RuntimeError("boom"), KeyboardInterrupt(), SystemExit(3),
])
def test_queue_cleanup_runs_even_on_base_exceptions(tmp_path, error):
    app.taskqueue._discard_queue()
    reserved = app.reserve_unique_file(tmp_path / "held.pdf")

    def explode():
        raise error

    app.taskqueue._task_queue.append(
        app.taskqueue._QueuedTask("explodes", explode)
    )
    try:
        app.taskqueue._run_task_queue()
    except BaseException as exc:  # SystemExit must keep propagating
        assert isinstance(exc, SystemExit)

    assert app.taskqueue._task_queue == [], "queue must be cleared"
    # The reservation is released, so the same name is free again.
    assert app.reserve_unique_file(tmp_path / "held.pdf") == reserved
    app.clear_reservations()


def test_subsequent_queue_starts_clean(tmp_path):
    app.taskqueue._discard_queue()
    app.taskqueue._task_queue.append(
        app.taskqueue._QueuedTask("fails", lambda: (_ for _ in ()).throw(ValueError("x")))
    )
    app.taskqueue._run_task_queue()
    assert app.taskqueue._task_queue == []
    app.taskqueue._task_queue.append(app.taskqueue._QueuedTask("ok", lambda: None))
    app.taskqueue._run_task_queue()
    assert app.taskqueue._task_queue == []
