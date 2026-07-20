# -*- coding: utf-8 -*-
"""C-06: one source identity, used by every queued operation.

A queued task runs later than it was configured. Between those moments the
source can be edited, replaced, or swapped for a different file at the same
path. Size plus mtime plus inode does not notice an in-place rewrite of the
same length with a restored timestamp, so the identity carries a content hash -
and every operation that queues work must actually verify it before writing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from helpers import make_pdf  # noqa: E402


def _rewrite_in_place(path: Path, payload: bytes) -> None:
    """Replace the contents, same length, same inode, restored timestamp."""
    before = os.stat(str(path))
    original = path.read_bytes()
    assert len(payload) == len(original), "the test needs an equal-length payload"
    with open(str(path), "r+b") as handle:
        handle.seek(0)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(str(path), ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(str(path))
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    if before.st_ino:
        assert after.st_ino == before.st_ino, "the inode must be unchanged"


# --------------------------------------------------------------------------- #
# The fingerprint itself
# --------------------------------------------------------------------------- #

def test_same_size_in_place_rewrite_with_restored_timestamp_is_detected(tmp_path):
    source = make_pdf(tmp_path / "src.pdf", pages=3)
    doc = app.open_source_pdf(source)
    try:
        ref = app.capture_source(doc, source)
    finally:
        app.close_doc(doc)

    payload = bytearray(source.read_bytes())
    payload[-16:] = b"X" * 16          # same length, different bytes
    _rewrite_in_place(source, bytes(payload))

    with pytest.raises(app.SourceChangedError):
        ref.verify_unchanged()


def test_an_untouched_source_still_verifies(tmp_path):
    source = make_pdf(tmp_path / "src.pdf", pages=2)
    doc = app.open_source_pdf(source)
    try:
        ref = app.capture_source(doc, source)
    finally:
        app.close_doc(doc)
    ref.verify_unchanged()          # must not raise


def test_a_replaced_source_is_detected(tmp_path):
    source = make_pdf(tmp_path / "src.pdf", pages=3)
    doc = app.open_source_pdf(source)
    try:
        ref = app.capture_source(doc, source)
    finally:
        app.close_doc(doc)
    source.unlink()
    make_pdf(source, pages=3)
    with pytest.raises(app.SourceChangedError):
        ref.verify_unchanged()


def test_a_missing_source_is_detected(tmp_path):
    source = make_pdf(tmp_path / "src.pdf", pages=1)
    doc = app.open_source_pdf(source)
    try:
        ref = app.capture_source(doc, source)
    finally:
        app.close_doc(doc)
    source.unlink()
    with pytest.raises(app.SourceChangedError):
        ref.verify_unchanged()


def test_the_reference_never_reveals_the_password(tmp_path):
    source = make_pdf(tmp_path / "src.pdf", pages=1)
    doc = app.open_source_pdf(source)
    try:
        ref = app.capture_source(doc, source)
    finally:
        app.close_doc(doc)
    object.__setattr__(ref, "password", "hunter2") if hasattr(
        ref, "__dataclass_fields__") else setattr(ref, "password", "hunter2")
    assert "hunter2" not in repr(ref)


# --------------------------------------------------------------------------- #
# Non-PDF sources need the same identity
# --------------------------------------------------------------------------- #

def test_office_sources_can_be_fingerprinted(tmp_path):
    from test_office_validation import make_ooxml

    src = make_ooxml(tmp_path / "doc.docx", "word")
    ref = app.capture_file_source(src, family="word")
    ref.verify_unchanged()

    payload = bytearray(src.read_bytes())
    payload[-8:] = b"Z" * 8
    _rewrite_in_place(src, bytes(payload))
    with pytest.raises(app.SourceChangedError):
        ref.verify_unchanged()


# --------------------------------------------------------------------------- #
# Queued operations must verify before writing anything
# --------------------------------------------------------------------------- #

def test_watermark_removal_refuses_a_changed_source(tmp_path, monkeypatch):
    """A watermark job queued then edited must produce no output."""
    from helpers import repeated_image_pdf

    source = repeated_image_pdf(tmp_path / "wm.pdf", pages=3)
    out = tmp_path / "out.pdf"

    doc = app.open_source_pdf(source)
    try:
        candidates, _total = app.scan_watermark_candidates(doc)
        signatures = [c.signature for c in candidates]
        ref = app.capture_source(doc, source)
    finally:
        app.close_doc(doc)

    payload = bytearray(source.read_bytes())
    payload[-24:] = b"Q" * 24
    _rewrite_in_place(source, bytes(payload))

    with pytest.raises(app.SourceChangedError):
        reopened = ref.open()
        try:
            app.remove_watermark_images(reopened, signatures, out)
        finally:
            app.close_doc(reopened)

    assert not out.exists(), "no output may be produced from a changed source"


def test_office_conversion_refuses_a_changed_source(tmp_path, monkeypatch):
    """An Office job queued then edited must produce no output."""
    from pdf_forge import convert_backend as cb
    from pdf_forge import msoffice, ops_office
    from test_office_validation import make_ooxml

    src = make_ooxml(tmp_path / "doc.docx", "word")
    plan = ops_office._build_jobs([src])
    job = plan.accepted[0]
    job["out"] = tmp_path / "out.pdf"

    payload = bytearray(src.read_bytes())
    payload[-8:] = b"Z" * 8
    _rewrite_in_place(src, bytes(payload))

    reached = []
    monkeypatch.setattr(msoffice, "convert_to_pdf",
                        lambda *a, **k: reached.append(a))
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: False)
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)

    outcome = ops_office._convert_one(object(), job,
                                      cb.BackendChoice(cb.MSOFFICE, "Word"))
    assert outcome == "fail", "a changed source must fail the job"
    assert reached == [], "the converter must never see a changed source"
    assert not job["out"].exists()


def test_every_queued_job_carries_a_source_identity(tmp_path):
    """_build_jobs must attach an identity, not just a bare path."""
    from pdf_forge import ops_office
    from test_office_validation import make_ooxml

    src = make_ooxml(tmp_path / "doc.docx", "word")
    plan = ops_office._build_jobs([src])
    job = plan.accepted[0]
    assert job.get("ref") is not None, (
        "the job carries only a path; a source edited after configuration "
        "would be converted without notice"
    )
