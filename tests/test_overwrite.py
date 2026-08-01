# -*- coding: utf-8 -*-
"""OW-1: the opt-in "overwrite the existing file?" prompt.

The owner asked for an overwrite option whose answer defaults to **no**. These
tests pin both halves of that: the default answer must reproduce the historical
no-clobber behaviour byte for byte, and an explicit yes must actually reach
disk. Every test drives the real prompt through ``builtins.input`` (see
``tests/test_back_navigation.py::_feed``) rather than calling the writer layer
directly, because the guarantee being protected is a property of the prompt.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from helpers import make_pdf  # noqa: E402


def _feed(monkeypatch, answers):
    """Feed successive answers to every prompt that reads through _input."""
    supplied = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a: next(supplied))


def _choose(default_path, source):
    return app.prompts._choose_output_file(default_path, source)


def test_default_answer_keeps_the_unique_name(tmp_path, monkeypatch):
    """Pressing Enter at the new question must behave exactly like today.

    This is the guard on the requested default: the owner asked for the answer
    to default to no, so an unattended Enter can never destroy a file.
    """
    src = make_pdf(tmp_path / "src.pdf", 1)
    out = tmp_path / "out.pdf"
    original = b"USER DATA THAT MUST SURVIVE AN ENTER KEYPRESS"
    out.write_bytes(original)

    # output path = Enter (accept the default), overwrite? = Enter (default n).
    _feed(monkeypatch, ["", ""])
    chosen = _choose(out, src)

    assert chosen == tmp_path / "out_2.pdf", "Enter must keep the _2 behaviour"
    assert out.read_bytes() == original, "the existing file must be untouched"
    assert not app.overwrite_approved(out), "Enter must not record an approval"


def test_explicit_yes_returns_the_exact_path_and_approves(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "src.pdf", 1)
    out = tmp_path / "out.pdf"
    out.write_bytes(b"to be replaced")

    _feed(monkeypatch, ["", "y"])
    chosen = _choose(out, src)

    assert chosen == out, "an explicit yes must keep the exact name"
    assert app.overwrite_approved(out), "the approval must be recorded"


def test_no_question_when_the_destination_is_free(tmp_path, monkeypatch):
    """A free destination must not gain an extra question.

    Only one answer is supplied: if the prompt asked anything else, the feed
    would raise StopIteration and this test would fail.
    """
    src = make_pdf(tmp_path / "src.pdf", 1)
    out = tmp_path / "free.pdf"

    _feed(monkeypatch, [""])
    chosen = _choose(out, src)

    assert chosen == out, "a free path is returned unchanged"
    assert not app.overwrite_approved(out), "nothing was approved"


def test_source_file_is_still_refused_even_with_overwrite(tmp_path, monkeypatch):
    """The output may never be the source, approved or not.

    The source exists on disk, so a weakened guard would fall through to the
    overwrite question and consume the second answer as a y/n. It does not: the
    second answer is read as the re-prompted output path.
    """
    src = make_pdf(tmp_path / "src.pdf", 2)
    before = src.read_bytes()
    elsewhere = tmp_path / "elsewhere.pdf"

    _feed(monkeypatch, [str(src), str(elsewhere)])
    chosen = _choose(tmp_path / "default.pdf", src)

    assert chosen == elsewhere, "the source must be rejected and re-prompted"
    assert src.read_bytes() == before, "the source must never be targeted"
    assert not app.overwrite_approved(src), "the source can never be approved"


def test_second_queued_task_cannot_also_claim_an_approved_path(tmp_path,
                                                               monkeypatch):
    """Two queued tasks must never target one file, overwrite or not."""
    src = make_pdf(tmp_path / "src.pdf", 1)
    out = tmp_path / "out.pdf"
    out.write_bytes(b"original")

    _feed(monkeypatch, ["", "y"])
    first = _choose(out, src)
    assert first == out

    # A second operation asks for the same destination and also answers yes.
    _feed(monkeypatch, ["", "y"])
    second = _choose(out, src)

    assert second == tmp_path / "out_2.pdf", (
        f"the second task must fall back to a unique name; got {second.name}"
    )
    assert not app.overwrite_approved(second), "the fallback is not an overwrite"


def test_end_to_end_overwrite_replaces_the_file(tmp_path, monkeypatch):
    """Configure a real operation with y, run the queue, check the bytes.

    Proves the approval survives configuration -> queue -> execution and is
    honoured by the writer, which is the whole point of the registry.
    """
    src = make_pdf(tmp_path / "doc.pdf", 3)
    out = tmp_path / "picked.pdf"
    stale = b"%PDF-1.7 stale output the user wants gone\n"
    out.write_bytes(stale)

    # source, selection="1", output=<out>, overwrite?="y"
    _feed(monkeypatch, [str(src), "1", str(out), "y"])
    with pytest.raises(app.taskqueue._TaskQueued):
        app.ops_pages.operation_extract_pages()

    app.taskqueue._run_task_queue()

    assert out.exists(), "the approved destination must still be there"
    assert out.read_bytes() != stale, "the file was not replaced"
    assert not (tmp_path / "picked_2.pdf").exists(), "no sibling may be created"
    # The replacement is the real extracted page, not just different bytes.
    doc = app.open_source_pdf(out)
    try:
        assert doc.page_count == 1, "the output must be the extracted page"
    finally:
        app.close_doc(doc)
