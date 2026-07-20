# -*- coding: utf-8 -*-
"""End-to-end tests for the REAL interactive flows that queue work.

F-03 - Split configures its document, closes it, and reopens inside the runner.
The runner must write from the document it reopened; a leftover reference to the
closed configure-time reader breaks every split at execution time.

F-02 - Remove watermark must queue its task with the captured source identity so
the common queue gate (``_verify_sources``) proves the file is unchanged before
the runner writes anything. A runner that reopens a raw path instead bypasses
the gate entirely.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
import pymupdf  # noqa: E402
from helpers import rgb_png  # noqa: E402

windows_only = pytest.mark.skipif(
    os.name != "nt",
    reason="exclusive-handle semantics are Windows-specific",
)


# --------------------------------------------------------------------------- #
# Fixtures and harness
# --------------------------------------------------------------------------- #

def numbered_pdf(path: Path, pages: int) -> Path:
    """A PDF whose every page carries its own 1-based number as text.

    Blank pages cannot prove page order or range membership; these can.
    """
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=200, height=200)
        page.insert_text((20, 100), f"PAGE{number}")
    doc.save(str(path))
    doc.close()
    return path


def stamped_pdf(path: Path, pages: int = 3, pad: int = 0) -> Path:
    """A PDF with the same image on every page (a stand-in watermark).

    ``pad`` appends that many bytes of trailing PDF comment. Everything after
    ``%%EOF`` is ignored by any reader, so the file stays completely valid while
    giving a test a region it can rewrite in place without corrupting it.
    """
    doc = pymupdf.open()
    data = rgb_png(size=(120, 90), color=(200, 30, 30))
    for _ in range(pages):
        page = doc.new_page(width=400, height=500)
        page.insert_image(pymupdf.Rect(50, 50, 170, 140), stream=data)
        page.insert_text((60, 300), "body text")
    doc.save(str(path))
    doc.close()
    if pad:
        with open(str(path), "ab") as handle:
            handle.write(b"\n%" + b"A" * pad + b"\n")
    return path


def rewrite_tail_in_place(path: Path, count: int, filler: bytes = b"B") -> None:
    """Replace the last ``count`` bytes: same size, same file id, same mtime."""
    before = os.stat(str(path))
    with open(str(path), "r+b") as handle:
        handle.seek(-count, os.SEEK_END)
        handle.write(filler * count)
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(str(path), ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(str(path))
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    if before.st_ino:
        assert after.st_ino == before.st_ino, "the file id must be unchanged"


def drive(monkeypatch, operation, answers, ask=None):
    """Run a real operation through the real prompts. True when it queued."""
    supplied = iter(answers)
    for module in (app.ops_pages, app.ops_watermark, app.prompts):
        monkeypatch.setattr(module, "_input", lambda _p: next(supplied),
                            raising=False)
    decide = ask if ask is not None else (lambda *_a, **_k: True)
    for module in (app.prompts, app.ops_pages, app.ops_watermark):
        monkeypatch.setattr(module, "ask_yes_no", decide, raising=False)
    try:
        operation()
    except app.taskqueue._TaskQueued:
        return True
    except StopIteration:  # pragma: no cover - test wiring problem
        pytest.fail("the operation asked more questions than the script supplied")
    return False


def page_labels(path: Path):
    """The PAGEn markers of a produced PDF, in document order."""
    doc = pymupdf.open(str(path))
    try:
        return [page.get_text().strip() for page in doc]
    finally:
        doc.close()


def assert_source_is_free(path: Path) -> None:
    """A leaked handle makes this rename fail on Windows."""
    moved = path.with_name(path.stem + "_moved.pdf")
    path.rename(moved)
    moved.rename(path)


# --------------------------------------------------------------------------- #
# F-03 - Split must write from the document its runner reopened
# --------------------------------------------------------------------------- #

def test_split_writes_every_chunk_from_the_reopened_document(tmp_path, monkeypatch,
                                                             capsys):
    """The whole Split flow, executed the way the queue executes it."""
    src = numbered_pdf(tmp_path / "src.pdf", 6)
    out_dir = tmp_path / "chunks"
    assert drive(monkeypatch, app.operation_split_chunks,
                 [str(src), "2", "", "", str(out_dir)]), "Split did not queue"

    task = app.taskqueue._task_queue[-1]
    app.taskqueue._verify_sources(task)
    task.run()

    produced = sorted(out_dir.glob("*.pdf"))
    assert len(produced) == 3, f"expected 3 chunks, got {[p.name for p in produced]}"
    # Order and membership: chunk N holds exactly its own two source pages.
    assert [page_labels(p) for p in produced] == [
        ["PAGE1", "PAGE2"], ["PAGE3", "PAGE4"], ["PAGE5", "PAGE6"],
    ]
    # The reported paths are the ones that actually exist on disk.
    reported = capsys.readouterr().out
    assert "Created 3 file(s), 6 page(s)" in reported
    assert "closed" not in reported.lower(), (
        "the runner used a closed document instead of the one it reopened"
    )


def test_split_leaves_the_source_usable_after_the_run(tmp_path, monkeypatch):
    src = numbered_pdf(tmp_path / "src.pdf", 4)
    out_dir = tmp_path / "chunks"
    assert drive(monkeypatch, app.operation_split_chunks,
                 [str(src), "2", "", "", str(out_dir)])
    app.taskqueue._task_queue[-1].run()
    assert sorted(p.name for p in out_dir.glob("*.pdf")), "the run produced nothing"

    # Reopenable: the runner closed what it opened.
    doc = app.open_source_pdf(src)
    try:
        assert doc.page_count == 4
    finally:
        app.close_doc(doc)
    if os.name == "nt":
        assert_source_is_free(src)


def test_split_of_a_sub_range_writes_only_that_range(tmp_path, monkeypatch):
    """A second configuration shape, so the fix cannot be a one-case special."""
    src = numbered_pdf(tmp_path / "src.pdf", 6)
    out_dir = tmp_path / "chunks"
    assert drive(monkeypatch, app.operation_split_chunks,
                 [str(src), "2", "3", "6", str(out_dir)])
    app.taskqueue._task_queue[-1].run()

    produced = sorted(out_dir.glob("*.pdf"))
    assert [page_labels(p) for p in produced] == [
        ["PAGE3", "PAGE4"], ["PAGE5", "PAGE6"],
    ]


# --------------------------------------------------------------------------- #
# F-02 - Remove watermark must queue the source identity, not a raw path
# --------------------------------------------------------------------------- #

def test_watermark_removal_is_stopped_by_the_queue_source_gate(tmp_path, monkeypatch,
                                                               capsys):
    """Configure, edit the source in place, then let the queue run the task."""
    src = stamped_pdf(tmp_path / "wm.pdf", pages=3, pad=64)
    assert drive(monkeypatch, app.operation_remove_watermark,
                 [str(src), "", ""]), "the watermark operation did not queue"

    # The queue's own gate must be wired, not just the runner's reopen. Both
    # verify, so dropping `sources=[ref]` alone still passes the behavioural
    # assertions below - this is what pins the queue half of C-06.
    queued = app.taskqueue._task_queue[-1]
    assert [ref.path for ref in queued.sources] == [src], (
        "the task was queued without its source identity; the common queue "
        f"gate would verify nothing (sources={queued.sources!r})"
    )

    # Same size, same file id, restored timestamp - only the bytes differ.
    rewrite_tail_in_place(src, 32)

    before = {p.name for p in tmp_path.iterdir()}
    app.taskqueue._run_task_queue()          # the real execution path
    after = {p.name for p in tmp_path.iterdir()}

    assert after == before, (
        f"a changed source produced files: {sorted(after - before)}"
    )
    assert app.load_generated_outputs() == set(), \
        "a refused task must not enter the generated-output manifest"
    # Only the content hash can see this edit, so this also proves the cheap
    # metadata check was not what stopped the task.
    assert "was edited in place after this task was configured" in \
        capsys.readouterr().out, "the user was never told the source changed"


def test_watermark_removal_still_works_on_an_unchanged_source(tmp_path, monkeypatch):
    """The gate must not cost the happy path."""
    src = stamped_pdf(tmp_path / "wm.pdf", pages=3, pad=64)
    assert drive(monkeypatch, app.operation_remove_watermark, [str(src), "", ""])

    app.taskqueue._run_task_queue()

    out = tmp_path / "wm_no_watermark.pdf"
    assert out.exists(), "an unchanged source must still be processed"
    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 3
        # delete_image swaps the object for a tiny placeholder, so the check is
        # that the watermark-sized image is gone - not that no image remains.
        for page in doc:
            sizes = [(i["width"], i["height"]) for i in page.get_image_info()]
            assert (120, 90) not in sizes, f"the watermark is still present: {sizes}"
            assert "body text" in page.get_text(), "the page text must be preserved"
    finally:
        doc.close()
