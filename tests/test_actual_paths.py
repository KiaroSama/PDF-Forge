# -*- coding: utf-8 -*-
"""F-01: a writer must report the path it actually wrote.

Queue-time reservation only protects one process. When the configured
destination appears between configuration and execution, no-clobber promotion
allocates a ``_2`` sibling - so the path the operation was configured against is
no longer the path on disk. A writer that returns only a count, or a caller that
re-reads the configured destination, hands the user the name of a file it never
wrote (and, for the extraction tool, a created-file list pointing at someone
else's file).

Each test recreates that exact race: the destination is free while the operation
is configured, an unrelated external file is created at it, and only then does
the operation run. The external file must survive untouched, and the writer's
return value, the created-file list, the success message and the completion log
must all name the suffixed path.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
import pymupdf  # noqa: E402
from helpers import file_hash, make_pdf, rgba_png  # noqa: E402
from pdf_forge import core, render  # noqa: E402


# --------------------------------------------------------------------------- #
# N-05 - a path can be reserved for only one queued output object; a file and a
# directory can never share it, so reserving either kind blocks the other.
# --------------------------------------------------------------------------- #

def test_file_reservation_blocks_the_same_path_as_a_directory(tmp_path):
    core.clear_reservations()
    try:
        p = tmp_path / "result.pdf"
        f = core.reserve_unique_file(p)
        d = core.reserve_unique_dir(p)
        assert core.normalized_path_key(f) != core.normalized_path_key(d), (
            "a file and a directory cannot occupy one path; the second "
            "reservation must be suffixed"
        )
    finally:
        core.clear_reservations()


def test_directory_reservation_blocks_the_same_path_as_a_file(tmp_path):
    core.clear_reservations()
    try:
        p = tmp_path / "result.pdf"
        d = core.reserve_unique_dir(p)
        f = core.reserve_unique_file(p)
        assert core.normalized_path_key(d) != core.normalized_path_key(f)
    finally:
        core.clear_reservations()

# Content of the unrelated file that appears at the configured destination.
_EXTERNAL = b"%PDF-1.7\nsomeone else's file that must survive\n"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

def _occupy(path: Path) -> str:
    """Create an unrelated external file at ``path``; returns its hash."""
    path.write_bytes(_EXTERNAL)
    return file_hash(path)


def _suffixed(path: Path) -> Path:
    """The sibling no-clobber promotion allocates when ``path`` is taken."""
    return path.parent / f"{path.stem}_2{path.suffix}"


def _configure(operation):
    """Run an operation's configuration phase; return its queued runner.

    Configuration ends by queueing the task, so the runner executes later -
    which is precisely the window this defect lives in.
    """
    app.taskqueue._task_queue.clear()
    with pytest.raises(app.taskqueue._TaskQueued):
        operation()
    assert app.taskqueue._task_queue, "the operation queued no task"
    return app.taskqueue._task_queue[-1].run


def _spy(monkeypatch, module, name):
    """Wrap a writer so the test can see what it returned to its caller."""
    original = getattr(module, name)
    returned = []

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        returned.append(result)
        return result

    monkeypatch.setattr(module, name, wrapper)
    return returned


def _assert_reported(configured: Path, external_hash: str, capsys, caplog,
                     log_marker: str) -> Path:
    """Every user-visible channel must name the file that was really written."""
    suffixed = _suffixed(configured)
    assert file_hash(configured) == external_hash, \
        "the external file at the configured destination was modified"
    assert suffixed.exists(), "no suffixed output was produced"

    out = capsys.readouterr().out
    assert str(suffixed) in out, f"stdout does not name the written file:\n{out}"
    assert str(configured) not in out, \
        f"stdout names a file this run did not write:\n{out}"

    said = [r.getMessage() for r in caplog.records if log_marker in r.getMessage()]
    assert said, f"no completion log record matching '{log_marker}'"
    assert all(str(suffixed) in m for m in said), \
        f"the completion log does not name the written file: {said}"
    assert not any(str(configured) in m for m in said), \
        f"the completion log names a file this run did not write: {said}"
    return suffixed


@pytest.fixture(autouse=True)
def _capture_app_log(caplog):
    caplog.set_level(logging.INFO, logger="pdf_forge")


# --------------------------------------------------------------------------- #
# Compression
# --------------------------------------------------------------------------- #

def test_compression_reports_the_file_it_actually_wrote(
        tmp_path, monkeypatch, capsys, caplog):
    src = make_pdf(tmp_path / "doc.pdf", 3)
    configured = tmp_path / "doc_compressed.pdf"

    monkeypatch.setattr(app.ops_compress, "prompt_source_pdf", lambda: src)
    monkeypatch.setattr(app.ops_compress, "_prompt_compression_level",
                        lambda: ("ultra", None, None))
    monkeypatch.setattr(app.ops_compress, "_choose_output_file",
                        lambda default, source: default)
    returned = _spy(monkeypatch, app.ops_compress, "compress_pdf")

    run = _configure(app.operation_compress_pdf)
    external_hash = _occupy(configured)
    capsys.readouterr()          # drop the configuration summary
    caplog.clear()
    run()

    suffixed = _assert_reported(configured, external_hash, capsys, caplog,
                                "Compressed ")
    assert returned[-1].path == suffixed
    assert returned[-1].stats["pages"] == 3
    # The reported size must come from the file that was written, not from the
    # external file sitting at the configured name.
    assert returned[-1].stats["new_size"] == suffixed.stat().st_size


# --------------------------------------------------------------------------- #
# Image-only PDF
# --------------------------------------------------------------------------- #

def test_image_only_pdf_reports_the_file_it_actually_wrote(
        tmp_path, monkeypatch, capsys, caplog):
    src = make_pdf(tmp_path / "doc.pdf", 2)
    configured = app.default_image_pdf_output(src)

    monkeypatch.setattr(app.ops_convert, "prompt_source_pdf", lambda: src)
    monkeypatch.setattr(app.ops_convert, "prompt_image_quality", lambda: 40)
    monkeypatch.setattr(app.ops_convert, "_choose_output_file",
                        lambda default, source: default)
    returned = _spy(monkeypatch, app.ops_convert, "render_pdf_to_image_pdf")

    run = _configure(app.operation_pdf_to_image_pdf)
    external_hash = _occupy(configured)
    capsys.readouterr()
    caplog.clear()
    run()

    suffixed = _assert_reported(configured, external_hash, capsys, caplog,
                                "Image-only PDF complete")
    assert returned[-1].path == suffixed
    assert returned[-1].count == 2


# --------------------------------------------------------------------------- #
# Transparent (soft-masked) embedded image
# --------------------------------------------------------------------------- #

def _soft_masked_pdf(path: Path) -> Path:
    """A PDF holding one image whose transparency lives in a separate /SMask."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.insert_image(pymupdf.Rect(10, 10, 160, 160), stream=rgba_png((60, 60)))
    doc.save(str(path))
    doc.close()
    return path


def test_soft_masked_extraction_lists_the_file_it_actually_wrote(
        tmp_path, monkeypatch):
    src = _soft_masked_pdf(tmp_path / "alpha.pdf")
    out_dir = tmp_path / "images"
    out_dir.mkdir()
    configured = out_dir / "p1_1.png"

    # Extraction picks the name and promotes it inside one call, so the race
    # window is collapsed here: the uniqueness check is neutralised and the
    # destination is already taken, which is what promotion faces when another
    # process wins the name a moment after it was chosen.
    monkeypatch.setattr(render, "unique_file_path", lambda path: path)
    external_hash = _occupy(configured)

    alpha_calls = _spy(monkeypatch, render, "_atomic_pixmap_save")

    doc = app.open_source_pdf(src)
    try:
        created = app.extract_embedded_images(doc, out_dir, jpeg_quality=None)
    finally:
        app.close_doc(doc)

    # Prove the alpha branch was taken; the ordinary branches never reach
    # _atomic_pixmap_save, so an empty list means this test proved nothing.
    assert alpha_calls, "the soft-mask branch was not exercised"

    suffixed = _suffixed(configured)
    assert file_hash(configured) == external_hash, \
        "the external file at the configured destination was modified"
    assert suffixed.exists(), "no suffixed output was produced"
    assert created == [suffixed], \
        f"the created-file list names a file this run did not write: {created}"
    assert alpha_calls[-1] == suffixed


# --------------------------------------------------------------------------- #
# Unlock
# --------------------------------------------------------------------------- #

def _owner_restricted_pdf(path: Path, pages: int = 2) -> Path:
    """Opens without a password, but forbids editing and copying."""
    blocked = (app.permission_bits()["editing content"]
               | app.permission_bits()["copying text/images"])
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
             owner_pw="owner", permissions=int(app.all_permissions() & ~blocked))
    doc.close()
    return path


def test_unlock_reports_the_file_it_actually_wrote(
        tmp_path, monkeypatch, capsys, caplog):
    src = _owner_restricted_pdf(tmp_path / "locked.pdf", pages=2)
    configured = tmp_path / "locked_unlocked.pdf"

    monkeypatch.setattr(app.ops_unlock, "prompt_source_pdf", lambda: src)
    monkeypatch.setattr(app.ops_unlock, "_choose_output_file",
                        lambda default, source: default)
    returned = _spy(monkeypatch, app.ops_unlock, "unlock_pdf_doc")

    run = _configure(app.operation_unlock_pdf)
    external_hash = _occupy(configured)
    capsys.readouterr()
    caplog.clear()
    run()

    suffixed = _assert_reported(configured, external_hash, capsys, caplog,
                                "Unlock complete")
    assert returned[-1].path == suffixed
    assert returned[-1].count == 2


# --------------------------------------------------------------------------- #
# Protection
# --------------------------------------------------------------------------- #

def test_open_password_protection_reports_the_file_it_actually_wrote(
        tmp_path, monkeypatch, capsys, caplog):
    src = make_pdf(tmp_path / "doc.pdf", 2)
    configured = tmp_path / "doc_protected.pdf"

    monkeypatch.setattr(app.ops_encrypt, "prompt_source_pdf", lambda: src)
    monkeypatch.setattr(app.ops_encrypt, "prompt_new_password",
                        lambda purpose: "openme")
    monkeypatch.setattr(app.ops_encrypt, "_choose_output_file",
                        lambda default, source: default)
    returned = _spy(monkeypatch, app.ops_encrypt, "save_encrypted_pdf")

    run = _configure(app.operation_protect_open_password)
    external_hash = _occupy(configured)
    capsys.readouterr()
    caplog.clear()
    run()

    suffixed = _assert_reported(configured, external_hash, capsys, caplog,
                                "Protect (open password) complete")
    assert returned[-1].path == suffixed
    assert returned[-1].count == 2
    # The protected file is the suffixed one, not the external bystander.
    check = pymupdf.open(str(suffixed))
    try:
        assert check.needs_pass
    finally:
        check.close()


def test_owner_restriction_reports_the_file_it_actually_wrote(
        tmp_path, monkeypatch, capsys, caplog):
    src = make_pdf(tmp_path / "doc.pdf", 3)
    configured = tmp_path / "doc_restricted.pdf"
    blocked = app.permission_bits()["editing content"]

    monkeypatch.setattr(app.ops_encrypt, "prompt_source_pdf", lambda: src)
    monkeypatch.setattr(
        app.ops_encrypt, "_prompt_blocked_actions",
        lambda: (int(app.all_permissions() & ~blocked), ["editing content"]),
    )
    monkeypatch.setattr(app.ops_encrypt, "prompt_new_password",
                        lambda purpose: "owner")
    monkeypatch.setattr(app.ops_encrypt, "_choose_output_file",
                        lambda default, source: default)
    returned = _spy(monkeypatch, app.ops_encrypt, "save_encrypted_pdf")

    run = _configure(app.operation_protect_restrict)
    external_hash = _occupy(configured)
    capsys.readouterr()
    caplog.clear()
    run()

    suffixed = _assert_reported(configured, external_hash, capsys, caplog,
                                "Protect (restrict) complete")
    assert returned[-1].path == suffixed
    assert returned[-1].count == 3
    check = pymupdf.open(str(suffixed))
    try:
        assert "editing content" in app.denied_permissions(check)
    finally:
        check.close()
