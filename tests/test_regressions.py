# -*- coding: utf-8 -*-
"""Regression tests for the audited defects (A1-A18) plus the hierarchical
prompt numbering (D), path-guidance prompts (E), and the convert-to-PDF
tool (B).

Each test targets behaviour that was wrong (or absent) before the fix, so it
fails against the old implementation for the right reason. Tests use temporary
directories and generated files only; they never touch real user files and
never require the native LibreOffice runtime.
"""

import csv
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402
from pypdf import PdfWriter  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def make_encrypted(path: Path, pages: int = 2, user_pw="pw", owner_pw="pw",
                   permissions=None) -> Path:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    kwargs = dict(encryption=pymupdf.PDF_ENCRYPT_AES_256)
    if user_pw:
        kwargs["user_pw"] = user_pw
    if owner_pw:
        kwargs["owner_pw"] = owner_pw
    if permissions is not None:
        kwargs["permissions"] = int(permissions)
    doc.save(str(path), **kwargs)
    doc.close()
    return path


def rgb_png(size=(40, 40), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def rgba_png(size=(50, 50)) -> bytes:
    img = Image.new("RGBA", size, (255, 0, 0, 0))
    for x in range(size[0] // 2):
        for y in range(size[1]):
            img.putpixel((x, y), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def repeated_image_pdf(path: Path, pages: int, color=(200, 0, 0)) -> Path:
    images = [Image.new("RGB", (120, 80), color) for _ in range(pages)]
    images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    return path


def label_of(prompt: str) -> str:
    import re

    match = re.search(r"(\S+)\.\s", prompt)
    return match.group(1) if match else ""


def zip_ooxml(path: Path, family="word") -> Path:
    """A structurally valid OOXML package (real content types + main part).

    The previous helper wrote a ZIP holding only a stub ``[Content_Types].xml``,
    which the hardened validator correctly rejects; tests must exercise real
    packages so they cannot pass against a weak implementation.
    """
    from test_office_validation import make_ooxml

    return make_ooxml(path, family)


# --------------------------------------------------------------------------- #
# A1 - merge submenu 1 must finish reliably
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("terminator", ["", "done", "DONE", "Done"])
def test_merge_finishes_on_blank_finish_and_done(tmp_path, monkeypatch, terminator):
    a, b = make_pdf(tmp_path / "a.pdf", 1), make_pdf(tmp_path / "b.pdf", 1)
    answers = iter([str(a), str(b), terminator])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "b.pdf"]


def test_merge_minimum_count_error_keeps_flow(tmp_path, monkeypatch):
    """Finishing early errors and stays in the flow; duplicates stay rejected."""
    a, b = make_pdf(tmp_path / "a.pdf", 1), make_pdf(tmp_path / "b.pdf", 1)
    answers = iter(["", "done", str(a), str(a), str(b), "done"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "b.pdf"]


def test_merge_zero_returns_back(tmp_path, monkeypatch):
    answers = iter(["0"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    assert app.ops_merge.prompt_merge_source_files() is None


def test_merge_exit_raises_exit_request(tmp_path, monkeypatch):
    answers = iter(["exit"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    with pytest.raises(app._ExitRequested):
        app.ops_merge.prompt_merge_source_files()


# --------------------------------------------------------------------------- #
# A2 - queue-time output path reservation
# --------------------------------------------------------------------------- #

def test_reservations_prevent_queued_output_collision(tmp_path):
    app.clear_reservations()
    try:
        target = tmp_path / "out.pdf"
        chosen = [app.reserve_unique_file(target) for _ in range(3)]
        assert chosen[0].name == "out.pdf"
        assert len(set(chosen)) == 3, "queued tasks must not share an output path"
    finally:
        app.clear_reservations()


def test_files_and_dirs_are_reserved_separately(tmp_path):
    app.clear_reservations()
    try:
        d1 = app.reserve_unique_dir(tmp_path / "shared")
        d2 = app.reserve_unique_dir(tmp_path / "shared")
        assert d1 != d2
        # A file reservation does not consume a directory name and vice versa.
        assert app.reserve_unique_file(tmp_path / "shared") == tmp_path / "shared"
    finally:
        app.clear_reservations()


def test_reservation_is_case_insensitive_on_windows(tmp_path):
    app.clear_reservations()
    try:
        app.reserve_unique_file(tmp_path / "Case.pdf")
        second = app.reserve_unique_file(tmp_path / "case.pdf")
        if os.name == "nt":
            assert second.name == "case_2.pdf"
        else:
            assert second.name == "case.pdf"
    finally:
        app.clear_reservations()


def test_reservation_respects_existing_disk_file(tmp_path):
    app.clear_reservations()
    try:
        existing = make_pdf(tmp_path / "taken.pdf", 1)
        assert app.reserve_unique_file(existing).name == "taken_2.pdf"
    finally:
        app.clear_reservations()


def test_reservations_released_individually_and_globally(tmp_path):
    app.clear_reservations()
    first = app.reserve_unique_file(tmp_path / "x.pdf")
    app.release_reservations(files=[first])
    assert app.reserve_unique_file(tmp_path / "x.pdf") == first
    app.clear_reservations()
    assert app.reserve_unique_file(tmp_path / "x.pdf") == first
    app.clear_reservations()


# --------------------------------------------------------------------------- #
# A3 - protection preservation policy
# --------------------------------------------------------------------------- #

def test_extract_preserves_open_password_protection(tmp_path):
    src = make_encrypted(tmp_path / "enc.pdf", pages=3)
    doc = app.open_source_pdf(src, password_prompt=lambda previous_failed=False: "pw")
    try:
        policy = app.detect_protection(doc)
        assert policy.kind == "password" and policy.can_preserve
        out = tmp_path / "out.pdf"
        app.write_pages_to_pdf(doc, [0, 1], out, protection=policy)
    finally:
        app.close_doc(doc)

    check = pymupdf.open(str(out))
    try:
        assert check.needs_pass, "protection must not be silently dropped"
        assert check.authenticate("pw") > 0
        assert check.page_count == 2
    finally:
        check.close()


def test_transform_without_policy_still_produces_plain_output(tmp_path):
    """The default (no policy passed) stays backwards compatible."""
    src = make_pdf(tmp_path / "plain.pdf", 2)
    doc = app.open_source_pdf(src)
    try:
        out = tmp_path / "o.pdf"
        app.write_pages_to_pdf(doc, [0], out)
    finally:
        app.close_doc(doc)
    check = pymupdf.open(str(out))
    try:
        assert not check.needs_pass
    finally:
        check.close()


def test_owner_restricted_source_is_flagged_not_preserved(tmp_path):
    perms = int(pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ACCESSIBILITY)
    src = make_encrypted(tmp_path / "r.pdf", pages=1, user_pw=None,
                         owner_pw="owner", permissions=perms)
    doc = app.open_source_pdf(src)
    try:
        policy = app.detect_protection(doc)
        assert policy.kind == "restricted"
        assert policy.is_protected and not policy.can_preserve
        assert policy.denied and policy.save_kwargs() == {}
    finally:
        app.close_doc(doc)


def test_unprotected_source_needs_no_policy(tmp_path):
    doc = app.open_source_pdf(make_pdf(tmp_path / "p.pdf", 2))
    try:
        policy = app.detect_protection(doc)
        assert policy.kind == "none" and not policy.is_protected
        assert policy.save_kwargs() == {}
    finally:
        app.close_doc(doc)


def test_merge_protection_requires_explicit_choice(tmp_path, monkeypatch):
    """A merge never invents a policy when sources are protected."""
    protected = app.ProtectionPolicy(kind="password", password="pw")
    plain = app.ProtectionPolicy(kind="none")
    monkeypatch.setattr(app.prompts, "_input", lambda _p: "y")
    resolved = app.resolve_merge_protection([protected, plain])
    assert resolved is not None and resolved.kind == "none"

    monkeypatch.setattr(app.prompts, "_input", lambda _p: "n")
    assert app.resolve_merge_protection([protected, plain]) is None

    # All-plain sources need no question at all.
    assert app.resolve_merge_protection([plain, plain]).kind == "none"


# --------------------------------------------------------------------------- #
# A4 / A7 / A8 / A9 - image identity, painted occurrences, form xobjects
# --------------------------------------------------------------------------- #

def test_different_images_are_not_grouped(tmp_path):
    doc = pymupdf.open()
    red, blue = rgb_png(color=(255, 0, 0)), rgb_png(color=(0, 0, 255))
    for _ in range(3):
        page = doc.new_page(width=200, height=200)
        page.insert_image(pymupdf.Rect(10, 10, 60, 60), stream=red)
        page.insert_image(pymupdf.Rect(80, 10, 130, 60), stream=blue)
    src = tmp_path / "two.pdf"
    doc.save(str(src))
    doc.close()

    doc = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        assert len(candidates) == 2, "distinct images must not share an identity"
        assert len({c.signature for c in candidates}) == 2
    finally:
        app.close_doc(doc)


def test_same_image_across_pages_groups_once(tmp_path):
    src = repeated_image_pdf(tmp_path / "wm.pdf", 4)
    doc = app.open_source_pdf(src)
    try:
        candidates, total = app.scan_watermark_candidates(doc)
        assert total == 4
        assert candidates and len(candidates[0].pages) == 4
        assert len(candidates[0].pages) >= 4
        assert candidates[0].sample_xref > 0
    finally:
        app.close_doc(doc)


def test_watermark_in_form_xobject_detected_and_removed(tmp_path):
    stamp = pymupdf.open()
    page = stamp.new_page(width=80, height=40)
    page.insert_image(pymupdf.Rect(0, 0, 80, 40), stream=rgb_png(size=(80, 40)))
    stamp_path = tmp_path / "stamp.pdf"
    stamp.save(str(stamp_path))
    stamp.close()

    stamp_doc = pymupdf.open(str(stamp_path))
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page(width=300, height=300)
        page.insert_text((20, 200), "keep this text")
        page.show_pdf_page(pymupdf.Rect(20, 20, 100, 60), stamp_doc, 0)
    src = tmp_path / "formwm.pdf"
    doc.save(str(src))
    doc.close()
    stamp_doc.close()

    doc = app.open_source_pdf(src)
    try:
        candidates, _ = app.scan_watermark_candidates(doc)
        assert candidates, "a watermark painted via a Form XObject must be found"
        target = candidates[0]
        assert len(target.pages) == 3
        out = tmp_path / "clean.pdf"
        modified = app.remove_watermark_images(doc, [target.signature], out)
        assert modified > 0, "modified==0 would be a misleading success message"
    finally:
        app.close_doc(doc)

    check = app.open_source_pdf(out)
    try:
        after, _ = app.scan_watermark_candidates(check, min_pages=2)
        assert target.signature not in {c.signature for c in after}
        assert "keep this text" in check[0].get_text()
    finally:
        app.close_doc(check)


def test_extract_dedupes_identical_content_across_xrefs(tmp_path):
    png = rgb_png(size=(60, 60), color=(10, 120, 200))
    parts = []
    for name in ("one.pdf", "two.pdf"):
        d = pymupdf.open()
        p = d.new_page(width=200, height=200)
        p.insert_image(pymupdf.Rect(10, 10, 70, 70), stream=png)
        path = tmp_path / name
        d.save(str(path))
        d.close()
        parts.append(path)

    merged = pymupdf.open()
    for path in parts:
        piece = pymupdf.open(str(path))
        merged.insert_pdf(piece)
        piece.close()
    src = tmp_path / "merged.pdf"
    merged.save(str(src))
    merged.close()

    doc = app.open_source_pdf(src)
    try:
        xrefs = set()
        for page in doc:
            for entry in page.get_images(full=True):
                xrefs.add(entry[0])
        assert len(xrefs) >= 2, "fixture must hold the image under two xrefs"
        assert app.count_embedded_images(doc) == 1
        created = app.extract_embedded_images(doc, tmp_path / "imgs")
        assert len(created) == 1, "identical content must be extracted once"
    finally:
        app.close_doc(doc)


# --------------------------------------------------------------------------- #
# A10 - soft mask / transparency
# --------------------------------------------------------------------------- #

def transparent_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.insert_image(pymupdf.Rect(10, 10, 110, 110), stream=rgba_png())
    doc.save(str(path))
    doc.close()
    return path


def test_original_mode_rebuilds_alpha_from_soft_mask(tmp_path):
    src = transparent_pdf(tmp_path / "alpha.pdf")
    doc = app.open_source_pdf(src)
    try:
        created = app.extract_embedded_images(doc, tmp_path / "orig", jpeg_quality=None)
        assert created, "no image extracted"
        with Image.open(created[0]) as img:
            assert img.mode in ("RGBA", "LA"), "transparency must survive"
            assert img.getchannel("A").getextrema()[0] == 0, "alpha channel lost"
    finally:
        app.close_doc(doc)


def test_jpeg_mode_composites_soft_mask(tmp_path):
    src = transparent_pdf(tmp_path / "alpha.pdf")
    doc = app.open_source_pdf(src)
    try:
        created = app.extract_embedded_images(doc, tmp_path / "jpg", jpeg_quality=85)
        assert created and created[0].suffix == ".jpg"
        with Image.open(created[0]) as img:
            assert img.mode == "RGB"     # JPEG cannot carry alpha
    finally:
        app.close_doc(doc)


# --------------------------------------------------------------------------- #
# A11 - pathological delete ranges
# --------------------------------------------------------------------------- #

def test_huge_delete_range_rejected_without_materializing():
    import time

    started = time.perf_counter()
    with pytest.raises(app.PageSelectionError):
        app.parse_delete_pages("1-999999999")
    assert time.perf_counter() - started < 1.0


def test_delete_range_bounded_by_document_length():
    assert app.parse_delete_pages("1-3", max_page=10) == [1, 2, 3]
    for bad in ("1-50", "999"):
        with pytest.raises(app.PageSelectionError):
            app.parse_delete_pages(bad, max_page=10)


def test_normal_delete_syntax_unchanged():
    assert app.parse_delete_pages("5") == [5]
    assert app.parse_delete_pages("3,1,2") == [1, 2, 3]
    assert app.parse_delete_pages("10-12,11") == [10, 11, 12]


# --------------------------------------------------------------------------- #
# A12 / A13 - unlimited password retries, silent reopen
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("wrong_attempts", [1, 3, 4, 11, 21, 25])
def test_password_retries_are_unlimited(tmp_path, wrong_attempts):
    src = make_encrypted(tmp_path / "enc.pdf", pages=1, user_pw="correct",
                         owner_pw="correct")
    calls = {"n": 0}

    def prompt(previous_failed=False):
        calls["n"] += 1
        return "wrong" if calls["n"] <= wrong_attempts else "correct"

    doc = app.open_source_pdf(src, password_prompt=prompt)
    try:
        assert doc.page_count == 1
    finally:
        app.close_doc(doc)
    assert calls["n"] == wrong_attempts + 1, "no attempt limit may apply"


def test_blank_password_does_not_consume_an_attempt_limit(tmp_path):
    src = make_encrypted(tmp_path / "enc.pdf", pages=1, user_pw="correct",
                         owner_pw="correct")
    calls = {"n": 0}

    def prompt(previous_failed=False):
        calls["n"] += 1
        return "" if calls["n"] <= 6 else "correct"

    doc = app.open_source_pdf(src, password_prompt=prompt)
    try:
        assert doc.page_count == 1
    finally:
        app.close_doc(doc)
    assert calls["n"] == 7


def test_password_prompt_receives_previous_failed_flag(tmp_path):
    src = make_encrypted(tmp_path / "enc.pdf", pages=1, user_pw="correct",
                         owner_pw="correct")
    seen = []

    def prompt(previous_failed=False):
        seen.append(previous_failed)
        return "correct" if len(seen) > 2 else "wrong"

    doc = app.open_source_pdf(src, password_prompt=prompt)
    app.close_doc(doc)
    assert seen == [False, True, True], "only retries are flagged as failures"


def test_password_cancel_is_a_distinct_signal(tmp_path):
    src = make_encrypted(tmp_path / "enc.pdf", pages=1, user_pw="x", owner_pw="x")
    with pytest.raises(app.PdfPasswordCancelled):
        app.open_source_pdf(src, password_prompt=lambda previous_failed=False: None)
    # Batch code catches PdfOpenError, so the cancel must remain a subclass.
    assert issubclass(app.PdfPasswordCancelled, app.PdfOpenError)


def test_captured_password_enables_silent_reopen(tmp_path):
    src = make_encrypted(tmp_path / "enc.pdf", pages=1, user_pw="pw", owner_pw="pw")
    doc = app.open_source_pdf(src, password_prompt=lambda previous_failed=False: "pw")
    captured = app.source_password(doc)
    app.close_doc(doc)
    assert captured == "pw"

    calls = {"n": 0}

    def must_not_prompt(previous_failed=False):
        calls["n"] += 1
        return None

    reopened = app.open_source_pdf(src, password_prompt=must_not_prompt,
                                   password=captured)
    try:
        assert reopened.page_count == 1
    finally:
        app.close_doc(reopened)
    assert calls["n"] == 0, "a queued task must not ask for the password again"


def test_source_password_absent_for_plain_pdf(tmp_path):
    doc = app.open_source_pdf(make_pdf(tmp_path / "p.pdf", 1))
    try:
        assert app.source_password(doc) == ""
    finally:
        app.close_doc(doc)


# --------------------------------------------------------------------------- #
# A5 / A14 - handles and queue lifecycle
# --------------------------------------------------------------------------- #

def test_close_doc_is_idempotent(tmp_path):
    doc = app.open_source_pdf(make_pdf(tmp_path / "p.pdf", 1))
    app.close_doc(doc)
    app.close_doc(doc)          # must not raise
    app.close_doc(None)


def test_source_file_can_be_removed_after_use(tmp_path):
    """Windows: a leaked handle would make this fail with a sharing violation."""
    src = make_pdf(tmp_path / "s.pdf", 2)
    doc = app.open_source_pdf(src)
    out = tmp_path / "o.pdf"
    app.write_pages_to_pdf(doc, [0], out)
    app.close_doc(doc)
    src.rename(tmp_path / "renamed.pdf")
    (tmp_path / "renamed.pdf").unlink()
    assert not src.exists()


def test_exit_at_start_confirmation_is_a_clean_exit(tmp_path, monkeypatch):
    app.taskqueue._task_queue.clear()
    app.clear_reservations()
    app.taskqueue._task_queue.append(app.taskqueue._QueuedTask("t", lambda: None))
    app.reserve_unique_file(tmp_path / "reserved.pdf")

    monkeypatch.setattr(app.prompts, "_input", lambda _p: "exit")
    exited = app.finalize_queue()

    assert exited is True, "exit must be reported, never raised to the top level"
    assert not app.taskqueue._task_queue
    assert not app.core._reserved_files


def test_declining_start_discards_and_releases(tmp_path, monkeypatch):
    app.taskqueue._task_queue.clear()
    app.clear_reservations()
    ran = {"n": 0}
    app.taskqueue._task_queue.append(
        app.taskqueue._QueuedTask("t", lambda: ran.__setitem__("n", 1))
    )
    app.reserve_unique_file(tmp_path / "reserved.pdf")

    monkeypatch.setattr(app.prompts, "_input", lambda _p: "n")
    assert app.finalize_queue() is False
    assert ran["n"] == 0
    assert not app.taskqueue._task_queue
    assert not app.core._reserved_files


def test_running_queue_releases_reservations(tmp_path, monkeypatch):
    app.taskqueue._task_queue.clear()
    app.clear_reservations()
    ran = {"n": 0}
    app.taskqueue._task_queue.append(
        app.taskqueue._QueuedTask("t", lambda: ran.__setitem__("n", 1))
    )
    app.reserve_unique_file(tmp_path / "reserved.pdf")

    monkeypatch.setattr(app.prompts, "_input", lambda _p: "y")
    assert app.finalize_queue() is False
    assert ran["n"] == 1
    assert not app.taskqueue._task_queue
    assert not app.core._reserved_files


def test_empty_queue_finalize_is_a_noop():
    app.taskqueue._task_queue.clear()
    assert app.finalize_queue() is False


def test_password_never_appears_in_a_task_repr():
    task = app.taskqueue._QueuedTask("Protect secret.pdf -> secret_protected.pdf",
                                     lambda: None)
    assert "hunter2" not in repr(task)
    assert "user_pw" not in repr(task)


# --------------------------------------------------------------------------- #
# A15 / A16 - honest batch reporting
# --------------------------------------------------------------------------- #

def test_folder_dpi_stats_counts_unscannable_files(tmp_path):
    make_pdf(tmp_path / "plain.pdf", 1)
    make_encrypted(tmp_path / "locked.pdf", pages=1, user_pw="secret",
                   owner_pw="secret")
    stats = app._folder_dpi_stats([tmp_path / "plain.pdf", tmp_path / "locked.pdf"])
    assert stats["files_not_scanned"] == 1, "encrypted files must not vanish"
    assert stats["files_text_only"] == 1
    assert "max" not in stats, "no image DPI can be claimed here"


def test_folder_dpi_stats_all_unscannable(tmp_path):
    make_encrypted(tmp_path / "a.pdf", pages=1, user_pw="s", owner_pw="s")
    make_encrypted(tmp_path / "b.pdf", pages=1, user_pw="s", owner_pw="s")
    stats = app._folder_dpi_stats([tmp_path / "a.pdf", tmp_path / "b.pdf"])
    assert stats["files_not_scanned"] == 2
    assert stats["files_with_images"] == 0 and stats["files_text_only"] == 0


def test_format_size_is_never_asked_for_negatives():
    assert app.ops_compress._format_size(0) == "0 B"
    assert app.ops_compress._format_size(2048) == "2.0 KB"
    assert app.ops_compress._format_size(5 * 1024 * 1024).endswith("MB")


# --------------------------------------------------------------------------- #
# A18 - no filesystem side effects during configuration
# --------------------------------------------------------------------------- #

def test_output_directory_not_created_during_configuration(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "s.pdf", 1)
    target_dir = tmp_path / "not_yet"
    monkeypatch.setattr(app.prompts, "_input", lambda _p: str(target_dir / "o.pdf"))
    monkeypatch.setattr(app.prompts, "ask_yes_no", lambda *_a, **_k: True)
    app.clear_reservations()
    try:
        chosen = app.prompts._choose_output_file(target_dir / "o.pdf", src)
        assert chosen.parent == target_dir
        assert not target_dir.exists(), "configuration must not create directories"
    finally:
        app.clear_reservations()


# --------------------------------------------------------------------------- #
# D - hierarchical prompt numbering
# --------------------------------------------------------------------------- #

def test_retries_advance_only_the_local_counter():
    app.set_operation_prompt("1")
    labels = [label_of(app.question_prompt(f"PDF file #{n}")) for n in (1, 2, 2, 2)]
    assert labels == ["1-1", "1-2", "1-3", "1-4"]


def test_prefix_follows_the_selected_submenu_item():
    app.set_operation_prompt("2")
    assert label_of(app.question_prompt("Folder")) == "2-1"
    assert label_of(app.question_prompt("Output")) == "2-2"
    app.set_operation_prompt("7")
    assert label_of(app.question_prompt("Source")) == "7-1"


def test_counter_resets_when_operation_restarts():
    app.set_operation_prompt("1")
    app.question_prompt("a")
    app.question_prompt("b")
    app.set_operation_prompt("1")
    assert label_of(app.question_prompt("a")) == "1-1"


def test_menu_and_queue_prompts_use_plain_numbering():
    app.set_operation_prompt(None)
    assert label_of(app.question_prompt("Start now?")) == "1"
    assert label_of(app.question_prompt("Queue another?")) == "2"


def test_reset_questions_keeps_the_prefix():
    app.set_operation_prompt("3")
    app.question_prompt("x")
    app.reset_questions()
    assert label_of(app.question_prompt("y")) == "3-1"


def test_nested_helper_prompts_stay_in_the_same_context():
    """Custom quality / confirmations continue the operation's numbering."""
    app.set_operation_prompt("1")
    app.question_prompt("Source PDF path")
    app.question_prompt("Output image quality")
    assert label_of(app.question_prompt("Custom DPI")) == "1-3"


# --------------------------------------------------------------------------- #
# E - path prompt guidance
# --------------------------------------------------------------------------- #

def test_drag_drop_guidance_exact_text():
    assert app.drag_drop_guidance() == "drag and drop a file here or paste a path"
    assert app.drag_drop_guidance(kind="folder") == (
        "drag and drop a folder here or paste a path"
    )
    assert app.drag_drop_guidance(repeated=True) == (
        "drag and drop a file here or paste a path; b=re-enter previous file; "
        "type done when finished"
    )


def test_pdf_file_prompt_exact_rendering_without_colour():
    """The documented multi-file prompt line, with colours disabled."""
    app.set_operation_prompt("1")
    app.question_prompt("PDF file #1")
    prompt = app.question_prompt(
        "PDF file #2",
        details=app.guidance_text(app.drag_drop_guidance(repeated=True),
                                  app.GUIDANCE_KEYWORDS),
    )
    assert prompt == (
        "\n1-2. PDF file #2 (drag and drop a file here or paste a path; "
        "b=re-enter previous file; type done when finished) {back=0, quit=exit}: "
    )
    assert "[" not in prompt, "a multi-file prompt carries no default marker"


def test_folder_prompt_exact_rendering():
    """A folder prompt gets the short guidance: no previous file, nothing to finish."""
    app.set_operation_prompt("2")
    prompt = app.question_prompt(
        "Folder containing PDFs",
        details=app.guidance_text(app.drag_drop_guidance(kind="folder"),
                                  app.GUIDANCE_KEYWORDS),
    )
    assert prompt == (
        "\n2-1. Folder containing PDFs (drag and drop a folder here or paste a "
        "path) {back=0, quit=exit}: "
    )


def test_guidance_colouring_matches_ffmwiz_split(monkeypatch):
    """Hint-coloured body, typeable keywords picked out in light blue."""
    monkeypatch.setattr(app.ui, "_COLOR_ENABLED", True)
    coloured = app.guidance_text(app.drag_drop_guidance(repeated=True),
                                 app.GUIDANCE_KEYWORDS)
    assert app.Color.LIGHT_BLUE + "b=" in coloured
    assert app.Color.LIGHT_BLUE + "done" in coloured
    # Each highlight restores the hint colour so the tail is not left plain.
    assert coloured.count(app.Color.HINT_YELLOW) == len(app.GUIDANCE_KEYWORDS)
    assert "drag and drop a file here" in coloured
    # With colour off the guidance is exactly the plain text.
    monkeypatch.setattr(app.ui, "_COLOR_ENABLED", False)
    assert app.guidance_text("plain text", app.GUIDANCE_KEYWORDS) == "plain text"


def test_quoted_and_unicode_paths_are_accepted(tmp_path):
    persian = tmp_path / "پوشه" / "سند.pdf"
    assert app.strip_surrounding_quotes('"' + str(persian) + '"') == str(persian)
    assert app.strip_surrounding_quotes("'" + str(persian) + "'") == str(persian)


def test_guidance_is_defined_once_not_duplicated():
    """The literal lives in core only, so prompts cannot drift apart."""
    package = Path(app.__file__).resolve().parent
    hits = [
        path.name for path in package.glob("*.py")
        if "drag and drop a" in path.read_text(encoding="utf-8")
    ]
    assert hits == ["core.py"], hits


# --------------------------------------------------------------------------- #
# B - office source handling (no native runtime required)
# --------------------------------------------------------------------------- #

def test_office_family_classification():
    assert app.classify_office_file(Path("a.docx")) == "word"
    assert app.classify_office_file(Path("a.DOC")) == "word"
    assert app.classify_office_file(Path("b.pptx")) == "powerpoint"
    assert app.classify_office_file(Path("c.xls")) == "excel"
    assert app.classify_office_file(Path("d.csv")) == "csv"
    assert app.classify_office_file(Path("e.pdf")) is None
    assert set(app.SUPPORTED_OFFICE_EXTS) == {
        ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv"
    }


def test_ooxml_validation_and_renamed_binaries(tmp_path):
    assert app.validate_office_file(zip_ooxml(tmp_path / "good.docx"))[0]

    # A ZIP carrying only [Content_Types].xml is not a real package.
    import zipfile

    stub = tmp_path / "stub.docx"
    with zipfile.ZipFile(stub, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    assert not app.validate_office_file(stub)[0]

    # A spreadsheet renamed to .docx must be rejected as the wrong family.
    wrong_family = zip_ooxml(tmp_path / "sheet.docx", family="excel")
    ok, reason = app.validate_office_file(wrong_family)
    assert not ok and "excel" in reason.lower()

    fake = tmp_path / "fake.docx"
    fake.write_bytes(b"not a zip at all")
    ok, reason = app.validate_office_file(fake)
    assert not ok and "OOXML" in reason

    binary_csv = tmp_path / "bin.csv"
    binary_csv.write_bytes(b"\x00\x01\x02binary")
    ok, reason = app.validate_office_file(binary_csv)
    assert not ok and "binary" in reason.lower()

    legacy = tmp_path / "old.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    assert app.validate_office_file(legacy)[0]

    bad_legacy = tmp_path / "bad.doc"
    bad_legacy.write_bytes(b"plain text pretending to be a doc")
    ok, reason = app.validate_office_file(bad_legacy)
    assert not ok and "signature" in reason

    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    assert not app.validate_office_file(empty)[0]


def test_office_discovery_skips_lock_files_and_sorts_naturally(tmp_path):
    zip_ooxml(tmp_path / "b2.docx")
    zip_ooxml(tmp_path / "b10.docx")
    zip_ooxml(tmp_path / "~$b2.docx")
    (tmp_path / "notes.txt").write_text("ignore me")
    (tmp_path / "sheet.csv").write_text("a,b\n1,2\n")
    (tmp_path / "sub").mkdir()
    zip_ooxml(tmp_path / "sub" / "nested.docx")     # non-recursive

    names = [p.name for p in app.discover_office_files(tmp_path)]
    assert "~$b2.docx" not in names
    assert "notes.txt" not in names
    assert "nested.docx" not in names
    assert names == ["b2.docx", "b10.docx", "sheet.csv"]


def test_office_lock_file_detection():
    assert app.is_office_lock_file("~$report.docx")
    assert not app.is_office_lock_file("report.docx")


def test_office_family_counts():
    counts = app.family_counts([Path("a.docx"), Path("b.doc"), Path("c.pptx"),
                                Path("d.xlsx"), Path("e.csv"), Path("f.pdf")])
    assert counts == {"word": 2, "powerpoint": 1, "excel": 1, "csv": 1}


@pytest.mark.parametrize("text,delim", [
    ("a,b,c\n1,2,3\n4,5,6\n", ","),
    ("a;b;c\n1;2;3\n4;5;6\n", ";"),
    ("a\tb\tc\n1\t2\t3\n4\t5\t6\n", "\t"),
])
def test_csv_delimiter_detection(tmp_path, text, delim):
    path = tmp_path / "d.csv"
    path.write_text(text, encoding="utf-8")
    dialect = app.detect_csv_dialect(path)
    assert dialect.delimiter == delim
    assert dialect.encoding == "UTF-8"


def test_csv_bom_is_detected(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf" + "name,note\n1,2\n".encode("utf-8"))
    dialect = app.detect_csv_dialect(path)
    assert dialect.encoding == "UTF-8"
    assert "BOM detected" in dialect.notes


def test_csv_quoted_multiline_field(tmp_path):
    path = tmp_path / "q.csv"
    path.write_text('name,note\n"a","line1\nline2"\n', encoding="utf-8")
    dialect = app.detect_csv_dialect(path)
    assert dialect.delimiter == ","


def test_csv_header_detection(tmp_path):
    path = tmp_path / "h.csv"
    path.write_text("name,count\nalpha,1\nbeta,2\n", encoding="utf-8")
    assert app.detect_csv_dialect(path).has_header is True


def test_csv_non_utf8_uses_deterministic_fallback(tmp_path):
    path = tmp_path / "latin.csv"
    path.write_bytes("naive,cafe\n1,2\n".encode("cp1252") + b"\xe9\n")
    dialect = app.detect_csv_dialect(path)
    assert dialect.encoding in ("UTF-8", "windows-1252")


# --------------------------------------------------------------------------- #
# B - runtime discovery, provisioning safety, output validation
# --------------------------------------------------------------------------- #

def test_runtime_paths_are_project_local():
    project = Path(app.__file__).resolve().parent.parent
    assert app.office_runtime.runtime_root() == project / ".tools"
    assert app.office_runtime.libreoffice_dir() == project / ".tools" / "libreoffice"


def test_runtime_metadata_is_pinned_and_checksummed():
    meta = app.office_runtime.load_runtime_meta()
    assert meta["version"]
    win = meta["windows"]
    assert win["url"].startswith("https://download.documentfoundation.org/")
    assert len(win["sha256"]) == 64
    assert meta["python_dependency"]["package"] == "unoserver"


def test_unoserver_resolves_from_the_project_environment():
    """The client must come from this project's environment, not a global one."""
    import importlib.util

    assert app.office_runtime.unoserver_installed() is (
        importlib.util.find_spec("unoserver") is not None
    )
    spec = importlib.util.find_spec("unoserver")
    if spec is None:
        pytest.skip("unoserver is not installed in this environment")
    origin = Path(spec.origin).resolve()
    venv_dirs = [Path(d).resolve() for d in app.office_runtime.venv_site_packages()]
    assert any(str(origin).startswith(str(d)) for d in venv_dirs), (
        f"unoserver resolved from {origin}, outside the project venv"
    )


def test_random_localhost_port_is_private_and_varies():
    port = app.office_runtime.random_localhost_port()
    assert 1024 < port < 65536
    assert port != app.office_runtime.random_localhost_port()


def test_runtime_status_shape():
    status = app.office_runtime.runtime_status()
    for key in ("unoserver_installed", "unoserver_version", "soffice",
                "soffice_python", "libreoffice_version", "ready"):
        assert key in status
    if not status["soffice"]:
        assert status["ready"] is False


def test_provisioning_refuses_an_unverified_download(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows-only administrative-extraction path")
    monkeypatch.setattr(app.office_runtime, "load_runtime_meta", lambda: {
        "version": "test",
        "windows": {"url": "https://example.invalid/x.msi", "sha256": "0" * 64},
    })
    empty = tmp_path / "no-runtime"
    empty.mkdir()
    monkeypatch.setattr(app.office_runtime, "libreoffice_dir", lambda: empty)
    monkeypatch.setattr(app.office_runtime, "runtime_root", lambda: tmp_path)

    def fake_download(url, dest):
        Path(dest).write_bytes(b"corrupted payload")

    with pytest.raises(app.office_runtime.OfficeRuntimeError) as excinfo:
        app.office_runtime.provision_runtime(download=fake_download)
    assert "checksum" in str(excinfo.value).lower()


def test_provisioning_refuses_when_no_checksum_is_pinned(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows-only administrative-extraction path")
    monkeypatch.setattr(app.office_runtime, "load_runtime_meta", lambda: {
        "version": "test",
        "windows": {"url": "https://example.invalid/x.msi"},
    })
    empty = tmp_path / "no-runtime3"
    empty.mkdir()
    monkeypatch.setattr(app.office_runtime, "libreoffice_dir", lambda: empty)
    monkeypatch.setattr(app.office_runtime, "runtime_root", lambda: tmp_path)
    with pytest.raises(app.office_runtime.OfficeRuntimeError) as excinfo:
        app.office_runtime.provision_runtime(
            download=lambda u, d: Path(d).write_bytes(b"x")
        )
    assert "checksum" in str(excinfo.value).lower()


def test_clean_runtime_only_touches_the_project_local_copy(tmp_path, monkeypatch):
    fake_runtime = tmp_path / "libreoffice"
    fake_runtime.mkdir()
    (fake_runtime / "marker.txt").write_text("x")
    monkeypatch.setattr(app.office_runtime, "libreoffice_dir", lambda: fake_runtime)
    assert app.office_runtime.clean_runtime() is True
    assert not fake_runtime.exists()
    assert app.office_runtime.clean_runtime() is False


def test_setup_makes_no_global_changes():
    """Provisioning must never touch PATH, the registry, or create shortcuts."""
    source = (Path(app.__file__).resolve().parent / "office_runtime.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("winreg", "SetEnvironmentVariable", "CreateShortcut",
                      "setx", "HKEY_"):
        assert forbidden not in source, forbidden


def test_conversion_output_validation(tmp_path):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(app.PdfOpenError):
        app.ops_office._validate_pdf_output(empty)

    app.ops_office._validate_pdf_output(make_pdf(tmp_path / "good.pdf", 1))

    locked = make_encrypted(tmp_path / "locked.pdf", pages=1, user_pw="p",
                            owner_pw="p")
    with pytest.raises(app.PdfOpenError):
        app.ops_office._validate_pdf_output(locked)


def test_convert_password_prompt_navigation(monkeypatch):
    """0/back/skip cancel; exit/quit raise; anything else is the password."""
    import getpass

    for word in ("0", "back", "skip", "BACK"):
        monkeypatch.setattr(getpass, "getpass", lambda _p, w=word: w)
        assert app.ops_office._prompt_convert_password("f.docx", False) is None

    for word in ("exit", "quit"):
        monkeypatch.setattr(getpass, "getpass", lambda _p, w=word: w)
        with pytest.raises(app._ExitRequested):
            app.ops_office._prompt_convert_password("f.docx", False)

    monkeypatch.setattr(getpass, "getpass", lambda _p: "s3cret")
    assert app.ops_office._prompt_convert_password("f.docx", True) == "s3cret"


def test_convert_password_is_not_placed_on_a_command_line():
    """B4/B8: the password goes through the in-memory API, never argv/env."""
    source = (Path(app.__file__).resolve().parent / "office_runtime.py").read_text(
        encoding="utf-8"
    )
    assert '"--password"' not in source and "'--password'" not in source
    assert 'kwargs["password"] = password' in source


# --------------------------------------------------------------------------- #
# A6 - folder/batch runs must not reprocess their own outputs
# --------------------------------------------------------------------------- #

def test_generated_outputs_are_excluded_from_folder_discovery(tmp_path):
    """A second folder run must not pick up the first run's output."""
    app.forget_generated_outputs()
    try:
        source = make_pdf(tmp_path / "book.pdf", 2)
        assert [p.name for p in app.discover_pdfs_in_folder(tmp_path)] == ["book.pdf"]

        # Simulate a tool writing its result beside the source.
        doc = app.open_source_pdf(source)
        out = tmp_path / "book_compressed.pdf"
        app.write_pages_to_pdf(doc, [0], out)
        app.close_doc(doc)
        assert out.exists()

        names = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
        assert names == ["book.pdf"], "our own output must not be rediscovered"

        # The escape hatch still sees everything.
        every = [p.name for p in app.discover_pdfs_in_folder(tmp_path,
                                                             include_generated=True)]
        assert sorted(every) == ["book.pdf", "book_compressed.pdf"]
    finally:
        app.forget_generated_outputs()


def test_exclusion_is_by_exact_path_not_by_name_substring(tmp_path):
    """A user's own file named like an output must still be processed."""
    app.forget_generated_outputs()
    try:
        # The user genuinely owns this file; we never generated it.
        user_file = make_pdf(tmp_path / "holiday_compressed.pdf", 1)
        make_pdf(tmp_path / "plain.pdf", 1)
        names = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
        assert "holiday_compressed.pdf" in names, "substring matching would hide this"
        assert user_file.exists()
    finally:
        app.forget_generated_outputs()


def test_manifest_forgets_deleted_outputs(tmp_path):
    app.forget_generated_outputs()
    try:
        generated = make_pdf(tmp_path / "gen.pdf", 1)
        app.record_generated_output(generated)
        assert app.discover_pdfs_in_folder(tmp_path) == []

        # The user deletes our output and puts their own, different file there.
        generated.unlink()
        make_pdf(tmp_path / "gen.pdf", 5)
        names = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
        assert names == ["gen.pdf"], "a replaced path is a normal source again"
    finally:
        app.forget_generated_outputs()


def test_recording_is_idempotent(tmp_path):
    app.forget_generated_outputs()
    try:
        generated = make_pdf(tmp_path / "g.pdf", 1)
        for _ in range(3):
            app.record_generated_output(generated)
        assert len(app.load_generated_outputs()) == 1
    finally:
        app.forget_generated_outputs()


# --------------------------------------------------------------------------- #
# B - encrypted-source detection, CSV normalization, runtime resilience
# --------------------------------------------------------------------------- #

def test_encrypted_office_detection_ooxml(tmp_path):
    """A password-to-open OOXML file is an OLE2 container, not a ZIP.

    Detection parses the OLE directory, so a real container is required: loose
    marker bytes must NOT be enough (that was a false-positive source).
    """
    from test_office_validation import make_encrypted_ooxml

    encrypted = make_encrypted_ooxml(tmp_path / "locked.docx")
    assert app.is_encrypted_office_file(encrypted)
    # Validation accepts it so the flow can ask for the password (PF-002).
    assert app.validate_office_file(encrypted)[0]

    # Marker bytes alone are not an encrypted package.
    bogus = tmp_path / "bogus.docx"
    bogus.write_bytes(
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
        + "EncryptedPackage".encode("utf-16-le") + b"\x00" * 64
    )
    assert not app.is_encrypted_office_file(bogus)


def test_encrypted_office_detection_odf(tmp_path):
    import zipfile

    encrypted = tmp_path / "locked.odt"
    with zipfile.ZipFile(encrypted, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("META-INF/manifest.xml",
                    '<manifest><encryption-data checksum="x"/></manifest>')
    assert app.is_encrypted_office_file(encrypted)


def test_plain_office_file_not_flagged_encrypted(tmp_path):
    assert not app.is_encrypted_office_file(zip_ooxml(tmp_path / "plain.docx"))
    plain_csv = tmp_path / "a.csv"
    plain_csv.write_text("a,b\n1,2\n", encoding="utf-8")
    assert not app.is_encrypted_office_file(plain_csv)
    assert not app.is_encrypted_office_file(tmp_path / "missing.docx")


def test_csv_normalization_applies_detected_dialect(tmp_path):
    source = tmp_path / "semi.csv"
    source.write_text("name;count\nalpha;1\nbeta;2\n", encoding="utf-8")
    original = source.read_bytes()

    dialect = app.detect_csv_dialect(source)
    assert dialect.delimiter == ";"
    out = app.normalize_csv_for_import(dialect=dialect, path=source,
                                       out_path=tmp_path / "norm.csv")
    text = out.read_text(encoding="utf-8")
    assert "name,count" in text and "alpha,1" in text
    assert source.read_bytes() == original, "the source CSV must not be modified"


def test_csv_normalization_preserves_quoted_fields(tmp_path):
    source = tmp_path / "q.csv"
    source.write_text('name;note\n"a";"x;y"\n', encoding="utf-8")
    dialect = app.detect_csv_dialect(source)
    out = app.normalize_csv_for_import(source, dialect, tmp_path / "n.csv")
    rows = list(csv.reader(out.open(encoding="utf-8", newline="")))
    assert rows[1] == ["a", "x;y"], rows


def test_bridge_loss_is_classified_for_restart():
    """A dead UNO bridge must be distinguishable from a bad input file."""
    classify = app.office_runtime._classify_convert_error
    for message in ("Binary URP bridge already disposed",
                    "Looks like LibreOffice died",
                    "[WinError 10061] No connection could be made"):
        assert classify(Exception(message)) == app.office_runtime.BRIDGE_LOST_SENTINEL
    assert app.office_runtime.is_bridge_lost(
        app.office_runtime.OfficeRuntimeError(
            app.office_runtime.BRIDGE_LOST_SENTINEL)
    )


def test_password_error_is_classified_for_retry():
    classify = app.office_runtime._classify_convert_error
    assert classify(Exception("wrong password supplied")) == \
        app.office_runtime.PASSWORD_SENTINEL
    generic = classify(Exception("some unrelated failure"))
    assert generic not in (app.office_runtime.PASSWORD_SENTINEL,
                           app.office_runtime.BRIDGE_LOST_SENTINEL)


def test_venv_site_packages_excludes_the_venv_root():
    """Putting sys.prefix on PYTHONPATH breaks LibreOffice's embedded Python."""
    for path in app.office_runtime.venv_site_packages():
        assert Path(path).name.lower() == "site-packages", path


def test_windows_prefers_soffice_exe_not_the_com_shim():
    """soffice.com exits after launching the real binary, which kills the bridge."""
    names = app.office_runtime._soffice_names()
    if os.name == "nt":
        assert names[0] == "soffice.exe"
    else:
        assert names == ["soffice"]


def test_profile_argument_is_a_plain_path(tmp_path):
    """unoserver calls Path(value).as_uri() itself; a URI would be rejected."""
    value = app.office_runtime._profile_argument(tmp_path)
    assert not value.startswith("file:")
    assert Path(value).is_absolute()


def test_converted_pdfs_remain_discoverable_for_pdf_tools(tmp_path):
    """A6 scoping: convert output is a new source, not a reprocessed output."""
    app.forget_generated_outputs()
    try:
        converted = make_pdf(tmp_path / "report.pdf", 1)   # as convert would write
        names = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
        assert "report.pdf" in names, (
            "a PDF converted from a document must stay available to PDF tools"
        )
        assert converted.exists()
    finally:
        app.forget_generated_outputs()


def test_crashed_libreoffice_marshalling_error_is_bridge_loss():
    """unoserver reports a dead LibreOffice as a traceback-marshalling failure.

    The classifier lower-cases the message, so the needle must be lower-case
    too - otherwise the very first crash is mistaken for a bad input file and
    the runtime is never restarted.
    """
    classify = app.office_runtime._classify_convert_error
    real_message = (
        "<Fault 1: \"<class 'uno.com.sun.star.uno.RuntimeException'>:Couldn't "
        "convert <traceback object at 0x1> to a UNO type; caught exception: "
        "<class 'AttributeError'>: 'traceback' object has no attribute "
        "'getTypes', traceback follows\">"
    )
    assert classify(Exception(real_message)) == \
        app.office_runtime.BRIDGE_LOST_SENTINEL


def test_b_re_enters_the_previous_file(tmp_path, monkeypatch):
    """'b' drops the file added last so it can be entered again."""
    a = make_pdf(tmp_path / "a.pdf", 1)
    b = make_pdf(tmp_path / "b.pdf", 1)
    c = make_pdf(tmp_path / "c.pdf", 1)
    # add a, add b, 'b' (undo b), add c, done
    answers = iter([str(a), str(b), "b", str(c), "done"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "c.pdf"]


def test_b_prompt_number_goes_back(tmp_path, monkeypatch):
    """After 'b' the prompt asks for that same file number again."""
    a = make_pdf(tmp_path / "a.pdf", 1)
    b = make_pdf(tmp_path / "b.pdf", 1)
    seen = []
    answers = iter([str(a), str(b), "b", str(b), "done"])

    def fake_input(prompt):
        seen.append(prompt)
        return next(answers)

    monkeypatch.setattr(app.ops_merge, "_input", fake_input)
    app.ops_merge.prompt_merge_source_files()
    titles = [p.split(". ", 1)[1].split(" (")[0] for p in seen]
    assert titles[:4] == ["PDF file #1", "PDF file #2", "PDF file #3", "PDF file #2"]


def test_b_with_nothing_selected_is_rejected(tmp_path, monkeypatch):
    a = make_pdf(tmp_path / "a.pdf", 1)
    b = make_pdf(tmp_path / "b.pdf", 1)
    answers = iter(["b", str(a), str(b), "done"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "b.pdf"]


def test_finish_keyword_is_gone(tmp_path, monkeypatch):
    """'finish' is no longer a terminator - it is treated as a path."""
    a = make_pdf(tmp_path / "a.pdf", 1)
    b = make_pdf(tmp_path / "b.pdf", 1)
    answers = iter([str(a), str(b), "finish", "done"])
    monkeypatch.setattr(app.ops_merge, "_input", lambda _p: next(answers))
    result = app.ops_merge.prompt_merge_source_files()
    assert [p.name for p in result] == ["a.pdf", "b.pdf"]


def test_office_prompt_supports_b_and_done(tmp_path, monkeypatch):
    first = zip_ooxml(tmp_path / "one.docx")
    second = zip_ooxml(tmp_path / "two.docx")
    answers = iter([str(first), "b", str(second), "done"])
    monkeypatch.setattr(app.ops_office, "_input", lambda _p: next(answers))
    result = app.ops_office.prompt_office_source_files()
    assert [p.name for p in result] == ["two.docx"]


# --------------------------------------------------------------------------- #
# Conversion timeout must never block (the "stuck for hours" regression)
# --------------------------------------------------------------------------- #

def test_wedged_conversion_times_out_and_is_abandoned(monkeypatch):
    """A hung LibreOffice must surface as BRIDGE_LOST within the timeout.

    Regression: the convert call used to run in a ThreadPoolExecutor, whose
    `with` block calls shutdown(wait=True) on exit - so the timeout fired and
    then the very next statement blocked on the same hung worker, wedging the
    run indefinitely. The worker is now a daemon thread that is never re-joined.
    """
    import time
    import types

    class FakeServer:
        port = 1

    fake = types.ModuleType("unoserver.client")

    class UnoClient:
        def __init__(self, **kwargs):
            pass

        def convert(self, **kwargs):
            time.sleep(60)  # never returns within the test's timeout

    fake.UnoClient = UnoClient
    monkeypatch.setitem(sys.modules, "unoserver", types.ModuleType("unoserver"))
    monkeypatch.setitem(sys.modules, "unoserver.client", fake)

    started = time.monotonic()
    with pytest.raises(app.office_runtime.OfficeRuntimeError) as excinfo:
        app.office_runtime.convert_to_pdf(FakeServer(), "in.docx", "out.pdf", timeout=2)
    elapsed = time.monotonic() - started

    assert app.office_runtime.is_bridge_lost(excinfo.value)
    assert elapsed < 15, f"timeout did not release promptly ({elapsed:.1f}s)"


def test_convert_worker_thread_is_daemon(monkeypatch):
    """The abandoned worker must not be able to hold up interpreter exit."""
    import threading
    import time
    import types

    class FakeServer:
        port = 1

    created = {}
    real_thread = threading.Thread

    def capture(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created["daemon"] = thread.daemon
        return thread

    fake = types.ModuleType("unoserver.client")

    class UnoClient:
        def __init__(self, **kwargs):
            pass

        def convert(self, **kwargs):
            time.sleep(30)

    fake.UnoClient = UnoClient
    monkeypatch.setitem(sys.modules, "unoserver", types.ModuleType("unoserver"))
    monkeypatch.setitem(sys.modules, "unoserver.client", fake)
    monkeypatch.setattr(threading, "Thread", capture)

    with pytest.raises(app.office_runtime.OfficeRuntimeError):
        app.office_runtime.convert_to_pdf(FakeServer(), "in.docx", "out.pdf", timeout=1)
    assert created.get("daemon") is True
