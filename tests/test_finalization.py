# -*- coding: utf-8 -*-
"""C-01/C-02/C-03: the output finalization contract.

A writer must report the path it actually produced, prove the output correct
*before* claiming a user-visible name, and leave nothing behind when it fails.
"""
from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from helpers import file_hash, make_pdf  # noqa: E402
from pdf_forge import safeio  # noqa: E402


# --------------------------------------------------------------------------- #
# C-03 - a failed promotion must not leave the claimed placeholder behind
# --------------------------------------------------------------------------- #

def test_failed_promotion_removes_the_placeholder_it_claimed(tmp_path, monkeypatch):
    """os.replace can fail on Windows (sharing violation, AV lock, cross-device).

    claim_unique_path() has already created an empty file at the final name by
    then. Leaving it behind hands the user a 0-byte file wearing the name of
    their expected output, and pushes every later run onto a _2 suffix.
    """
    staging = tmp_path / "staging.tmp"
    staging.write_bytes(b"%PDF-1.7\npayload\n")
    final = tmp_path / "out.pdf"

    def boom(*_args, **_kwargs):
        raise OSError(32, "The process cannot access the file")

    monkeypatch.setattr(safeio.os, "replace", boom)

    with pytest.raises(OSError):
        safeio.promote_atomically(staging, final)

    assert not final.exists(), "the claimed placeholder must be removed"
    assert not staging.exists(), "the staging file must be consumed"
    assert list(tmp_path.iterdir()) == [], f"stray files: {list(tmp_path.iterdir())}"


def test_failed_promotion_never_deletes_a_pre_existing_file(tmp_path, monkeypatch):
    """Only the placeholder this operation created may be removed."""
    external = tmp_path / "out.pdf"
    external.write_bytes(b"%PDF-1.7\nsomeone else's file\n")
    before = file_hash(external)

    staging = tmp_path / "staging.tmp"
    staging.write_bytes(b"%PDF-1.7\nours\n")

    def boom(*_args, **_kwargs):
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(safeio.os, "replace", boom)

    with pytest.raises(OSError):
        safeio.promote_atomically(staging, external)

    assert external.exists(), "a pre-existing external file must survive"
    assert file_hash(external) == before, "and must be byte-for-byte unchanged"
    # The placeholder this run claimed was out_2.pdf; it must be gone too.
    assert not (tmp_path / "out_2.pdf").exists()
    assert not staging.exists()


def test_failed_promotion_records_nothing(tmp_path, monkeypatch):
    """A failed output must never enter the generated-output manifest."""
    monkeypatch.setattr(safeio, "state_dir", lambda: tmp_path / "state")

    staging = tmp_path / "staging.tmp"
    staging.write_bytes(b"%PDF-1.7\n")
    final = tmp_path / "out.pdf"

    monkeypatch.setattr(safeio.os, "replace",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        safeio.promote_atomically(staging, final)

    assert safeio.load_generated_outputs() == set()


# --------------------------------------------------------------------------- #
# C-01 - the actual promoted path is what the caller must see
# --------------------------------------------------------------------------- #

def test_promotion_returns_the_suffixed_path_it_used(tmp_path):
    external = tmp_path / "out.pdf"
    external.write_bytes(b"external")
    staging = tmp_path / "staging.tmp"
    staging.write_bytes(b"%PDF-1.7\n")

    actual = safeio.promote_atomically(staging, external, record=False)

    assert actual == tmp_path / "out_2.pdf"
    assert external.read_bytes() == b"external", "the external file is untouched"


def test_page_writer_reports_the_path_it_actually_wrote(tmp_path):
    """C-01: extract/split/delete must report the suffixed path, not the one
    that was configured before an external file appeared."""
    source = make_pdf(tmp_path / "src.pdf", pages=4)
    requested = tmp_path / "out.pdf"
    requested.write_bytes(b"an unrelated file that appeared after configuration")
    external_before = file_hash(requested)

    doc = app.open_source_pdf(source)
    try:
        result = app.write_pages_to_pdf(doc, [0, 1], requested)
    finally:
        app.close_doc(doc)

    actual = getattr(result, "path", None)
    assert actual is not None, "the writer must report the path it produced"
    assert actual == tmp_path / "out_2.pdf"
    assert actual.exists()
    assert file_hash(requested) == external_before, "external file untouched"

    check = app.open_source_pdf(actual)
    try:
        assert check.page_count == 2
    finally:
        app.close_doc(check)


# --------------------------------------------------------------------------- #
# C-02 - deep validation must happen before the final name is claimed
# --------------------------------------------------------------------------- #

def test_wrong_page_order_leaves_no_final_output(tmp_path, monkeypatch):
    """A correct page COUNT with the wrong pages must not reach the user."""
    source = make_pdf(tmp_path / "src.pdf", pages=5)
    out = tmp_path / "out.pdf"

    import pdf_forge.pdf_io as pdf_io

    def wrong_order(out_path, source_doc, pages_zero_based, password=None):
        raise pdf_io.PdfOpenError("Output validation failed: page 1 does not match")

    monkeypatch.setattr(pdf_io, "validate_page_selection_output", wrong_order)

    doc = app.open_source_pdf(source)
    try:
        with pytest.raises(pdf_io.PdfOpenError):
            app.write_pages_to_pdf(doc, [0, 1], out)
    finally:
        app.close_doc(doc)

    assert not out.exists(), "a semantically wrong output must not be promoted"
    strays = [p.name for p in tmp_path.iterdir() if p.name != "src.pdf"]
    assert strays == [], f"staging/claim artifacts left behind: {strays}"


def test_failed_protection_postcondition_leaves_no_final_output(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"

    import pdf_forge.pdf_io as pdf_io

    def wrong_protection(out_path, policy):
        raise pdf_io.PdfOpenError("Output validation failed: protection was lost")

    monkeypatch.setattr(pdf_io, "validate_protection_postcondition", wrong_protection)

    doc = app.open_source_pdf(source)
    try:
        with pytest.raises(pdf_io.PdfOpenError):
            app.write_pages_to_pdf(doc, [0], out)
    finally:
        app.close_doc(doc)

    assert not out.exists()
    strays = [p.name for p in tmp_path.iterdir() if p.name != "src.pdf"]
    assert strays == [], f"staging/claim artifacts left behind: {strays}"


def test_a_rejected_output_is_absent_from_the_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(safeio, "state_dir", lambda: tmp_path / "state")
    source = make_pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"

    import pdf_forge.pdf_io as pdf_io
    monkeypatch.setattr(
        pdf_io, "validate_page_selection_output",
        lambda *_a, **_k: (_ for _ in ()).throw(pdf_io.PdfOpenError("bad")),
    )

    doc = app.open_source_pdf(source)
    try:
        with pytest.raises(pdf_io.PdfOpenError):
            app.write_pages_to_pdf(doc, [0], out)
    finally:
        app.close_doc(doc)

    assert safeio.load_generated_outputs() == set()


def test_deep_validation_runs_on_staging_not_on_the_final_name(tmp_path, monkeypatch):
    """The validator must never be pointed at a file this run did not write.

    With an external file already sitting on the configured name, validating
    'out.pdf' would inspect a stranger's document and could pass or fail on
    foreign content.
    """
    source = make_pdf(tmp_path / "src.pdf", pages=3)
    requested = tmp_path / "out.pdf"
    requested.write_bytes(b"not a pdf at all")

    seen = {}
    import pdf_forge.pdf_io as pdf_io
    real = pdf_io.validate_page_selection_output

    def spy(out_path, source_doc, pages_zero_based, password=None):
        # Read inside the spy: the staging file is consumed by promotion, so it
        # no longer exists once the writer returns.
        seen["validated"] = Path(out_path)
        seen["magic"] = Path(out_path).read_bytes()[:5]
        return real(out_path, source_doc, pages_zero_based, password=password)

    monkeypatch.setattr(pdf_io, "validate_page_selection_output", spy)

    doc = app.open_source_pdf(source)
    try:
        result = app.write_pages_to_pdf(doc, [0, 2], requested)
    finally:
        app.close_doc(doc)

    assert seen["validated"] != requested, (
        "validation ran against the external file, not the file just written"
    )
    assert seen["magic"] == b"%PDF-", "validated a real output, not the stranger"
    assert result.path == tmp_path / "out_2.pdf"
    assert requested.read_bytes() == b"not a pdf at all"


def test_no_temporary_or_claim_file_survives_a_successful_write(tmp_path):
    source = make_pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"

    doc = app.open_source_pdf(source)
    try:
        result = app.write_pages_to_pdf(doc, [0, 1], out)
    finally:
        app.close_doc(doc)

    assert result.path == out
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["out.pdf", "src.pdf"], f"unexpected files: {left}"
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())


def test_output_result_carries_the_page_count(tmp_path):
    source = make_pdf(tmp_path / "src.pdf", pages=6)
    doc = app.open_source_pdf(source)
    try:
        result = app.write_pages_to_pdf(doc, [0, 2, 4], tmp_path / "out.pdf")
    finally:
        app.close_doc(doc)
    assert result.count == 3
    assert isinstance(result.path, Path)


def test_output_result_is_immutable(tmp_path):
    from pdf_forge.safeio import OutputResult

    result = OutputResult(path=tmp_path / "x.pdf", count=1)
    # frozen=True: a writer cannot hand back a result a caller then edits.
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.path = tmp_path / "y.pdf"  # type: ignore[misc]


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-violation semantics")
def test_placeholder_is_removed_even_when_unlink_of_staging_fails(tmp_path,
                                                                  monkeypatch):
    """Cleanup of one artifact must not abandon cleanup of the other."""
    staging = tmp_path / "staging.tmp"
    staging.write_bytes(b"%PDF-1.7\n")
    final = tmp_path / "out.pdf"

    monkeypatch.setattr(safeio.os, "replace",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
    original_unlink = Path.unlink

    def stubborn(self, *args, **kwargs):
        if self.name == "staging.tmp":
            raise OSError(32, "locked")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stubborn)

    with pytest.raises(OSError):
        safeio.promote_atomically(staging, final)

    assert not final.exists(), "the placeholder must still be cleaned up"
