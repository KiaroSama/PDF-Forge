# -*- coding: utf-8 -*-
"""Protection regressions: the preservation policy, unlimited password
retries, and silent reopen.

Split out of the former single test_regressions module. Each test targets
behaviour that was wrong (or absent) before its fix, so it fails against the
old implementation for the right reason. Tests use temporary directories and
generated files only; they never touch real user files and never require the
native LibreOffice runtime.
"""

import csv  # noqa: F401
import io  # noqa: F401
import os  # noqa: F401
import sys
from pathlib import Path

import pytest  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402,F401
import pymupdf  # noqa: E402,F401
from PIL import Image  # noqa: E402,F401
from helpers import (  # noqa: E402,F401
    label_of, make_encrypted, make_pdf, repeated_image_pdf, rgb_png, rgba_png,
    zip_ooxml,
)
from pypdf import PdfWriter  # noqa: E402,F401


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
