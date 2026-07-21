# -*- coding: utf-8 -*-
"""Batch consent gate (PF-008) and honest unprotected reporting (PF-031).

Two defects, driven through the REAL interactive batch flows:

* #22 - a batch that writes derived PDFs from an owner-restricted source must
  ask for consent BEFORE queueing/writing, exactly like the single-file compress
  flow. Declining must leave no output; an unrestricted source must flow through
  without an extra prompt.
* #31 - a file whose write fails produced no output, so the closing batch report
  must not name it as "unprotected".

Each test targets behaviour that is wrong before its fix, so it fails against the
unfixed batch flow for the right reason. Tests use temporary directories and
generated files only; they never touch real user files.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
import pymupdf  # noqa: E402
from pdf_forge import batch_protection as bp  # noqa: E402
from helpers import make_encrypted, make_pdf  # noqa: E402

# Owner-restricted: opens freely (no user password) but forbids actions that an
# unrecoverable owner password enforces, so its policy cannot be reproduced.
RESTRICTED_PERMS = int(pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ACCESSIBILITY)


def _restricted_pdf(path: Path, pages: int = 4) -> Path:
    return make_encrypted(path, pages=pages, user_pw=None, owner_pw="owner",
                          permissions=RESTRICTED_PERMS)


def _script(monkeypatch, modules, answers, seen=None):
    """Feed one shared answer script to every module's ``_input``.

    Consumption order across the modules is the natural call order (folder ->
    selection/quality -> preflight decision). ``seen`` collects the rendered
    prompt text so a test can assert which prompts actually fired.
    """
    supplied = iter(answers)

    def feed(prompt):
        if seen is not None:
            seen.append(prompt.render() if hasattr(prompt, "render") else prompt)
        return next(supplied)

    for module in modules:
        monkeypatch.setattr(module, "_input", feed, raising=False)


# --------------------------------------------------------------------------- #
# #22 - delete-pages batch asks for consent before writing
# --------------------------------------------------------------------------- #

def test_delete_batch_restricted_asks_consent_before_writing(tmp_path, monkeypatch):
    src = _restricted_pdf(tmp_path / "r.pdf")
    seen = []
    # folder -> pages to delete -> restricted-files decision (2 = cancel).
    _script(monkeypatch, (app.prompts, app.ops_pages, bp),
            [str(tmp_path), "2", "2"], seen)

    queued = False
    try:
        app.operation_delete_pages_batch()
    except app.taskqueue._TaskQueued:
        queued = True
    except StopIteration:  # pragma: no cover - wiring problem
        pytest.fail("operation asked more questions than the script supplied")

    assert not queued, "a restricted batch must not queue when the user cancels"
    assert any("write them unprotected" in p for p in seen), \
        "consent must be requested before any output is queued or written (PF-008)"
    assert list(tmp_path.glob("*.pdf")) == [src], "no output may exist before consent"
    assert app.taskqueue._task_queue == []
    app.taskqueue._discard_queue()


def test_delete_batch_unrestricted_needs_no_consent(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "plain.pdf", 4)
    seen = []
    # folder -> pages to delete; no protection decision is expected.
    _script(monkeypatch, (app.prompts, app.ops_pages, bp),
            [str(tmp_path), "2"], seen)

    queued = False
    try:
        app.operation_delete_pages_batch()
    except app.taskqueue._TaskQueued:
        queued = True
    except StopIteration:
        pytest.fail("an unrestricted batch asked for a protection decision")

    assert queued, "an unrestricted batch must still reach the queue"
    assert not any("write them unprotected" in p for p in seen), \
        "no consent prompt may fire for unrestricted sources"
    assert src.exists()
    app.taskqueue._discard_queue()


# --------------------------------------------------------------------------- #
# #31 - a failed write is never reported as "unprotected"
# --------------------------------------------------------------------------- #

def test_delete_batch_failed_write_not_reported_unprotected(tmp_path, monkeypatch,
                                                            capsys):
    _restricted_pdf(tmp_path / "r.pdf")

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(app.ops_pages, "write_pages_to_pdf", boom)

    # folder -> pages to delete -> decision 3 (write unprotected) so the file
    # reaches the runner and the (failing) write is attempted.
    _script(monkeypatch, (app.prompts, app.ops_pages, bp),
            [str(tmp_path), "2", "3"])

    try:
        app.operation_delete_pages_batch()
    except app.taskqueue._TaskQueued:
        pass
    except StopIteration:
        pytest.fail("prompt script exhausted unexpectedly")

    if app.taskqueue._task_queue:
        app.taskqueue._task_queue[-1].run()

    out = capsys.readouterr().out.lower()
    assert "their outputs are unprotected" not in out, \
        "a file whose write failed produced no output to call 'unprotected' (PF-031)"
    assert not list(tmp_path.glob("*_deleted*.pdf")), "no output must have been written"
    app.taskqueue._discard_queue()


# --------------------------------------------------------------------------- #
# #22 / #31 - image-only PDF batch (ops_convert) has the same shape
# --------------------------------------------------------------------------- #

def test_image_pdf_batch_restricted_asks_consent_before_writing(tmp_path, monkeypatch):
    src = _restricted_pdf(tmp_path / "r.pdf", pages=2)
    seen = []
    # folder -> quality (3 = medium) -> restricted-files decision (2 = cancel).
    _script(monkeypatch, (app.prompts, app.ops_convert, bp),
            [str(tmp_path), "3", "2"], seen)

    queued = False
    try:
        app.operation_image_pdf_batch_folder()
    except app.taskqueue._TaskQueued:
        queued = True
    except StopIteration:
        pytest.fail("operation asked more questions than the script supplied")

    assert not queued, "a restricted image-PDF batch must not queue when cancelled"
    assert any("write them unprotected" in p for p in seen), \
        "consent must be requested before any output is queued or written (PF-008)"
    assert list(tmp_path.glob("*.pdf")) == [src], "no output may exist before consent"
    assert app.taskqueue._task_queue == []
    app.taskqueue._discard_queue()


def test_image_pdf_batch_unrestricted_needs_no_consent(tmp_path, monkeypatch):
    make_pdf(tmp_path / "plain.pdf", 2)
    seen = []
    _script(monkeypatch, (app.prompts, app.ops_convert, bp),
            [str(tmp_path), "3"], seen)

    queued = False
    try:
        app.operation_image_pdf_batch_folder()
    except app.taskqueue._TaskQueued:
        queued = True
    except StopIteration:
        pytest.fail("an unrestricted batch asked for a protection decision")

    assert queued, "an unrestricted batch must still reach the queue"
    assert not any("write them unprotected" in p for p in seen)
    app.taskqueue._discard_queue()


def test_image_pdf_batch_failed_render_not_reported_unprotected(tmp_path, monkeypatch,
                                                                capsys):
    _restricted_pdf(tmp_path / "r.pdf", pages=2)

    def boom(*_a, **_k):
        raise OSError("render failed")
    monkeypatch.setattr(app.ops_convert, "render_pdf_to_image_pdf", boom)

    # folder -> quality -> decision 3 (write unprotected) so the file reaches
    # the runner and the (failing) render is attempted.
    _script(monkeypatch, (app.prompts, app.ops_convert, bp),
            [str(tmp_path), "3", "3"])

    try:
        app.operation_image_pdf_batch_folder()
    except app.taskqueue._TaskQueued:
        pass
    except StopIteration:
        pytest.fail("prompt script exhausted unexpectedly")

    if app.taskqueue._task_queue:
        app.taskqueue._task_queue[-1].run()

    out = capsys.readouterr().out.lower()
    assert "their outputs are unprotected" not in out, \
        "a file whose render failed produced no output to call 'unprotected' (PF-031)"
    app.taskqueue._discard_queue()


# --------------------------------------------------------------------------- #
# N-01 - transient preflight failure must still fail closed at the runner.
#
# The batch preflight and each runner bind open_source_pdf / open_render_document
# in DIFFERENT modules, so patching only the preflight's binding to raise
# reproduces the exact scenario: preflight open fails once (the file is recorded
# "unreadable", so no consent prompt fires), the runner later opens the SAME
# owner-restricted file successfully, and it must be skipped/failed BEFORE any
# writer call - with no output, temp artifact, manifest entry, or "unprotected"
# report. Production is expected to already be correct; a failure here would
# expose a wiring gap.
# --------------------------------------------------------------------------- #

def _fail_preflight_open(monkeypatch):
    """Make the batch preflight's source-open fail transiently."""
    calls = {"pre": 0}

    def failing(src, *a, **k):
        calls["pre"] += 1
        raise app.PdfOpenError("transient preflight open failure")

    monkeypatch.setattr(bp, "open_source_pdf", failing)
    return calls


def test_delete_batch_transient_preflight_skips_restricted(tmp_path, monkeypatch,
                                                           capsys):
    src = _restricted_pdf(tmp_path / "r.pdf")
    pre = _fail_preflight_open(monkeypatch)

    opened = {"n": 0}
    real_open = app.ops_pages.open_source_pdf

    def open_spy(p, *a, **k):
        opened["n"] += 1
        return real_open(p, *a, **k)

    monkeypatch.setattr(app.ops_pages, "open_source_pdf", open_spy)
    wrote = {"n": 0}
    monkeypatch.setattr(app.ops_pages, "write_pages_to_pdf",
                        lambda *a, **k: wrote.__setitem__("n", wrote["n"] + 1))

    _script(monkeypatch, (app.prompts, app.ops_pages, bp), [str(tmp_path), "2"])
    try:
        app.operation_delete_pages_batch()
    except app.taskqueue._TaskQueued:
        pass
    if app.taskqueue._task_queue:
        app.taskqueue._task_queue[-1].run()

    out = capsys.readouterr().out.lower()
    assert pre["pre"] >= 1, "the preflight open must have been attempted"
    assert opened["n"] >= 1, "the runner must open the file itself"
    assert wrote["n"] == 0, "the writer must never be called for a skipped file"
    assert list(tmp_path.glob("*_deleted*.pdf")) == [], "no output may be written"
    assert list(tmp_path.glob(".pdfforge_*")) == [], "no temp artifact may survive"
    assert app.load_generated_outputs() == set(), "nothing may enter the manifest"
    assert "their outputs are unprotected" not in out
    assert list(tmp_path.glob("*.pdf")) == [src]
    app.taskqueue._discard_queue()


def test_image_pdf_batch_transient_preflight_skips_restricted(tmp_path, monkeypatch,
                                                             capsys):
    src = _restricted_pdf(tmp_path / "r.pdf", pages=2)
    pre = _fail_preflight_open(monkeypatch)

    opened = {"n": 0}
    real_open = app.ops_convert.open_render_document

    def open_spy(p, *a, **k):
        opened["n"] += 1
        return real_open(p, *a, **k)

    monkeypatch.setattr(app.ops_convert, "open_render_document", open_spy)
    wrote = {"n": 0}
    monkeypatch.setattr(app.ops_convert, "render_pdf_to_image_pdf",
                        lambda *a, **k: wrote.__setitem__("n", wrote["n"] + 1))

    _script(monkeypatch, (app.prompts, app.ops_convert, bp), [str(tmp_path), "3"])
    try:
        app.operation_image_pdf_batch_folder()
    except app.taskqueue._TaskQueued:
        pass
    if app.taskqueue._task_queue:
        app.taskqueue._task_queue[-1].run()

    out = capsys.readouterr().out.lower()
    assert pre["pre"] >= 1 and opened["n"] >= 1
    assert wrote["n"] == 0, "the renderer must never be called for a skipped file"
    assert list(tmp_path.glob(".pdfforge_*")) == []
    assert app.load_generated_outputs() == set()
    assert "their outputs are unprotected" not in out
    assert list(tmp_path.glob("*.pdf")) == [src], "no image-PDF output may be written"
    app.taskqueue._discard_queue()


def test_compress_batch_transient_preflight_fails_closed_on_restricted(
        tmp_path, monkeypatch, capsys):
    src = _restricted_pdf(tmp_path / "r.pdf")
    pre = _fail_preflight_open(monkeypatch)

    promoted = {"n": 0}
    monkeypatch.setattr(app.compress, "promote_atomically",
                        lambda *a, **k: promoted.__setitem__("n", promoted["n"] + 1))

    # folder -> compression level 6 (ultra). No consent prompt (unreadable).
    _script(monkeypatch, (app.prompts, app.ops_compress, bp), [str(tmp_path), "6"])
    try:
        app.operation_compress_pdf_batch()
    except app.taskqueue._TaskQueued:
        pass
    if app.taskqueue._task_queue:
        app.taskqueue._task_queue[-1].run()

    out = capsys.readouterr().out.lower()
    assert pre["pre"] >= 1
    assert promoted["n"] == 0, "nothing may be promoted for an owner-restricted file"
    # Compress fails closed (reports "failed", not "skipped"); both leave no output.
    assert "owner restrictions" in out
    assert list(tmp_path.glob(".pdfforge_*")) == [], "no temp artifact may survive"
    assert app.load_generated_outputs() == set()
    assert list(tmp_path.glob("*.pdf")) == [src], "no compressed output may be written"
    app.taskqueue._discard_queue()
