# -*- coding: utf-8 -*-
"""C-07..C-12 and C-16: the Office conversion path.

Staging uniqueness, output protection that actually protects, an explicit
manifest policy, per-family backend routing, family revalidation after
decryption, and macro/update hardening that cannot fail open.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from pdf_forge import convert_backend as cb  # noqa: E402
from pdf_forge import msoffice, office_server, ops_office  # noqa: E402
from test_office_validation import make_ooxml  # noqa: E402

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows/COM only")


def _pdf_bytes(pages: int = 1) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def _job(tmp_path, family="word", name="doc.docx"):
    src = make_ooxml(tmp_path / name, family)
    return {"src": src, "family": family, "out": tmp_path / "doc.pdf",
            "csv_dialect": None}


class _FakeSession:
    """Stands in for a backend session; writes a plain PDF wherever asked."""

    def __init__(self, pages: int = 1):
        self.pages = pages
        self.converted = []

    def convert(self, src, out, family, password=None, encrypted=False):
        self.converted.append(Path(src))
        Path(out).write_bytes(_pdf_bytes(self.pages))


# --------------------------------------------------------------------------- #
# C-08 - the selected protection must apply to the FINAL output
# --------------------------------------------------------------------------- #

def _convert_with_protection(tmp_path, monkeypatch, choice, source_password="pw"):
    """Drive the public per-file conversion flow with a protection choice."""
    session = _FakeSession()
    monkeypatch.setattr(msoffice, "convert_to_pdf",
                        lambda _s, src, out, fam, password=None, encrypted=False:
                        session.convert(src, out, fam))
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: True)
    monkeypatch.setattr(ops_office, "_prompt_convert_password",
                        lambda *_a: source_password)
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: choice)

    job = _job(tmp_path)
    outcome = ops_office._convert_one(session, job,
                                      cb.BackendChoice(cb.MSOFFICE, "Word"))
    return outcome, job["out"]


def _needs_password(path: Path, password: str) -> bool:
    import pymupdf

    doc = pymupdf.open(str(path))
    try:
        if not doc.needs_pass:
            return False
        return bool(doc.authenticate(password))
    finally:
        doc.close()


def test_same_password_protects_the_final_output(tmp_path, monkeypatch):
    """The user chose 'same password'; the promoted PDF must require it."""
    outcome, out = _convert_with_protection(tmp_path, monkeypatch, ("same", None))
    assert outcome == "ok"
    assert out.exists(), "the conversion must produce the configured output"
    assert _needs_password(out, "pw"), (
        "the final output is unprotected - the selected password was applied to "
        "a discarded sibling instead"
    )


def test_different_password_protects_the_final_output(tmp_path, monkeypatch):
    outcome, out = _convert_with_protection(
        tmp_path, monkeypatch, ("different", "brandnew")
    )
    assert outcome == "ok"
    assert _needs_password(out, "brandnew")
    import pymupdf

    doc = pymupdf.open(str(out))
    try:
        assert not doc.authenticate("pw"), "the old password must be rejected"
    finally:
        doc.close()


def test_no_protection_selection_leaves_the_output_open(tmp_path, monkeypatch):
    outcome, out = _convert_with_protection(tmp_path, monkeypatch, ("none", None))
    assert outcome == "ok"
    import pymupdf

    doc = pymupdf.open(str(out))
    try:
        assert not doc.needs_pass
    finally:
        doc.close()


def test_protection_leaves_no_stray_artifact(tmp_path, monkeypatch):
    """No .protect.tmp, no .convert_2.tmp, no encrypted orphan."""
    outcome, out = _convert_with_protection(tmp_path, monkeypatch, ("same", None))
    assert outcome == "ok"
    strays = sorted(p.name for p in tmp_path.iterdir()
                    if p.suffix == ".tmp" or ".tmp" in p.name or "_2" in p.stem)
    assert strays == [], f"stray artifacts left behind: {strays}"


# --------------------------------------------------------------------------- #
# C-07 - staging names must be unique across processes
# --------------------------------------------------------------------------- #

def test_conversion_staging_is_not_a_deterministic_sibling(tmp_path, monkeypatch):
    """Two processes converting the same source must not share a staging path."""
    seen = []
    session = _FakeSession()

    def record(_s, src, out, fam, password=None, encrypted=False):
        seen.append(Path(out))
        session.convert(src, out, fam)

    monkeypatch.setattr(msoffice, "convert_to_pdf", record)
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: False)
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)

    for index in range(2):
        job = _job(tmp_path, name=f"doc{index}.docx")
        job["out"] = tmp_path / "same.pdf"
        ops_office._convert_one(session, job,
                                cb.BackendChoice(cb.MSOFFICE, "Word"))

    assert len(seen) == 2
    assert seen[0] != seen[1], (
        f"both conversions staged through the same path: {seen[0]}"
    )
    for staging in seen:
        assert staging.suffix != ".tmp" or staging.parent != tmp_path, (
            "staging must not be a deterministic sibling of the output"
        )


# --------------------------------------------------------------------------- #
# C-09 - manifest policy is explicit, not accidental
# --------------------------------------------------------------------------- #

def test_converted_pdf_stays_discoverable_to_folder_tools(tmp_path, monkeypatch):
    """The documented policy: a converted PDF is a fresh source, not our output."""
    from pdf_forge import safeio

    monkeypatch.setattr(safeio, "state_dir", lambda: tmp_path / "state")
    session = _FakeSession()
    monkeypatch.setattr(msoffice, "convert_to_pdf",
                        lambda _s, src, out, fam, password=None, encrypted=False:
                        session.convert(src, out, fam))
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: False)
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)

    job = _job(tmp_path)
    assert ops_office._convert_one(
        session, job, cb.BackendChoice(cb.MSOFFICE, "Word")) == "ok"

    recorded = safeio.load_generated_outputs()
    assert recorded == set(), (
        "a converted PDF must stay discoverable; the code recorded it anyway"
    )


def test_a_failed_conversion_records_nothing(tmp_path, monkeypatch):
    from pdf_forge import safeio

    monkeypatch.setattr(safeio, "state_dir", lambda: tmp_path / "state")

    def explode(*_a, **_k):
        raise msoffice.MsOfficeError("conversion failed")

    monkeypatch.setattr(msoffice, "convert_to_pdf", explode)
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: False)
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)

    job = _job(tmp_path)
    assert ops_office._convert_one(
        object(), job, cb.BackendChoice(cb.MSOFFICE, "Word")) == "fail"
    assert safeio.load_generated_outputs() == set()
    assert not job["out"].exists()


# --------------------------------------------------------------------------- #
# C-10 - a family whose COM application is missing must not be routed to it
# --------------------------------------------------------------------------- #

def test_partial_office_does_not_claim_families_it_cannot_convert():
    """Word-only Office must not be selected for an Excel job."""
    with mock.patch.object(msoffice, "detect_office",
                           return_value={"apps": ["word"],
                                         "families": ["word"]}):
        choice = cb.detect_backend()
    assert hasattr(choice, "families"), (
        "the backend choice must carry which families it can actually convert"
    )
    assert "word" in choice.families
    assert "excel" not in choice.families
    assert "powerpoint" not in choice.families


def test_partial_office_falls_back_to_libreoffice_per_family(monkeypatch):
    """Excel job + Word-only Office + ready LibreOffice -> LibreOffice, not fail."""
    monkeypatch.setattr(msoffice, "detect_office",
                        lambda: {"apps": ["word"], "families": ["word"]})
    monkeypatch.setattr(app.office_runtime, "runtime_status",
                        lambda *a, **k: {"ready": True,
                                         "libreoffice_version": "25.8"})
    plan = cb.plan_batch(["word", "excel"])
    assert plan["word"].kind == cb.MSOFFICE
    assert plan["excel"].kind == cb.LIBREOFFICE


def test_partial_office_without_libreoffice_reports_the_unsupported_family(
        monkeypatch):
    monkeypatch.setattr(msoffice, "detect_office",
                        lambda: {"apps": ["excel"], "families": ["csv", "excel"]})
    monkeypatch.setattr(app.office_runtime, "runtime_status",
                        lambda *a, **k: {"ready": False})
    plan = cb.plan_batch(["excel", "csv", "word"])
    assert plan["excel"].kind == cb.MSOFFICE
    assert plan["csv"].kind == cb.MSOFFICE
    assert not plan["word"], "a family with no usable backend must be reported"


def test_full_office_serves_every_family(monkeypatch):
    monkeypatch.setattr(
        msoffice, "detect_office",
        lambda: {"apps": ["word", "excel", "powerpoint"],
                 "families": ["csv", "excel", "powerpoint", "word"]},
    )
    plan = cb.plan_batch(["word", "excel", "powerpoint", "csv"])
    assert {c.kind for c in plan.values()} == {cb.MSOFFICE}


# --------------------------------------------------------------------------- #
# C-11 - the decrypted package must match the family it claimed
# --------------------------------------------------------------------------- #

def _encrypted_looking(path: Path, payload: bytes) -> Path:
    """A stand-in whose 'decryption' yields the given package bytes."""
    path.write_bytes(payload)
    return path


def test_decrypted_family_mismatch_is_rejected(tmp_path, monkeypatch):
    """An encrypted XLSX renamed .docx must not reach Word."""
    real_xlsx = make_ooxml(tmp_path / "real.xlsx", "excel")
    payload = real_xlsx.read_bytes()
    claimed = _encrypted_looking(tmp_path / "claimed.docx", b"encrypted-blob")

    def fake_decrypt(_path, _password, temp_dir):
        target = Path(temp_dir) / "decrypted.docx"
        target.write_bytes(payload)
        return target

    monkeypatch.setattr(ops_office, "decrypt_to_temp", fake_decrypt)
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: True)
    monkeypatch.setattr(ops_office, "_prompt_convert_password", lambda *_a: "pw")
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)

    reached = []
    monkeypatch.setattr(app.office_runtime, "convert_to_pdf",
                        lambda _s, src, out, **_k: reached.append(Path(src)))

    job = {"src": claimed, "family": "word", "out": tmp_path / "out.pdf",
           "csv_dialect": None}
    outcome = ops_office._convert_one(object(), job,
                                      cb.BackendChoice(cb.LIBREOFFICE, "25.8"))

    assert outcome == "fail", "a family mismatch must fail the job"
    assert reached == [], "the converter must never see a mismatched package"
    assert not job["out"].exists()


def test_decrypted_matching_family_is_accepted(tmp_path, monkeypatch):
    real_docx = make_ooxml(tmp_path / "real.docx", "word")
    payload = real_docx.read_bytes()
    claimed = _encrypted_looking(tmp_path / "claimed.docx", b"encrypted-blob")

    def fake_decrypt(_path, _password, temp_dir):
        target = Path(temp_dir) / "decrypted.docx"
        target.write_bytes(payload)
        return target

    monkeypatch.setattr(ops_office, "decrypt_to_temp", fake_decrypt)
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: True)
    monkeypatch.setattr(ops_office, "_prompt_convert_password", lambda *_a: "pw")
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)
    monkeypatch.setattr(app.office_runtime, "convert_to_pdf",
                        lambda _s, src, out, **_k:
                        Path(out).write_bytes(_pdf_bytes()))

    job = {"src": claimed, "family": "word", "out": tmp_path / "out.pdf",
           "csv_dialect": None}
    assert ops_office._convert_one(
        object(), job, cb.BackendChoice(cb.LIBREOFFICE, "25.8")) == "ok"


def test_a_wrong_password_still_reprompts_and_is_not_a_family_error(
        tmp_path, monkeypatch):
    from pdf_forge import office_decrypt

    attempts = iter(["wrong", "right"])
    payload = make_ooxml(tmp_path / "real.docx", "word").read_bytes()

    def fake_decrypt(_path, password, temp_dir):
        if password != "right":
            raise office_decrypt.DecryptPasswordError("wrong password")
        target = Path(temp_dir) / "decrypted.docx"
        target.write_bytes(payload)
        return target

    monkeypatch.setattr(ops_office, "decrypt_to_temp", fake_decrypt)
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: True)
    monkeypatch.setattr(ops_office, "_prompt_convert_password",
                        lambda *_a: next(attempts))
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)
    monkeypatch.setattr(app.office_runtime, "convert_to_pdf",
                        lambda _s, src, out, **_k:
                        Path(out).write_bytes(_pdf_bytes()))

    job = {"src": _encrypted_looking(tmp_path / "c.docx", b"blob"),
           "family": "word", "out": tmp_path / "out.pdf", "csv_dialect": None}
    assert ops_office._convert_one(
        object(), job, cb.BackendChoice(cb.LIBREOFFICE, "25.8")) == "ok"
    assert next(attempts, "used") == "used", "the wrong password must re-prompt"


def test_a_malformed_decrypted_package_is_rejected(tmp_path, monkeypatch):
    def fake_decrypt(_path, _password, temp_dir):
        target = Path(temp_dir) / "decrypted.docx"
        target.write_bytes(b"not a zip at all")
        return target

    monkeypatch.setattr(ops_office, "decrypt_to_temp", fake_decrypt)
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: True)
    monkeypatch.setattr(ops_office, "_prompt_convert_password", lambda *_a: "pw")
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)
    reached = []
    monkeypatch.setattr(app.office_runtime, "convert_to_pdf",
                        lambda _s, src, out, **_k: reached.append(src))

    job = {"src": _encrypted_looking(tmp_path / "c.docx", b"blob"),
           "family": "word", "out": tmp_path / "out.pdf", "csv_dialect": None}
    assert ops_office._convert_one(
        object(), job, cb.BackendChoice(cb.LIBREOFFICE, "25.8")) == "fail"
    assert reached == []


# --------------------------------------------------------------------------- #
# C-12 - macro / external-update safety must not fail open
# --------------------------------------------------------------------------- #

def test_cli_fallback_hardens_its_profile():
    """The bridge-loss retry must not run under a fresh, unhardened profile."""
    source = Path(office_server.__file__).read_text(encoding="utf-8")
    body_start = source.index("def convert_via_soffice_cli(")
    body_end = source.index("\ndef ", body_start + 10)
    body = source[body_start:body_end]
    assert "_harden_profile" in body, (
        "convert_via_soffice_cli creates its own profile and never hardens it, "
        "so a bridge-loss retry opens the document with default macro security"
    )


def test_com_hardening_failure_aborts_before_opening_a_document():
    """A failed AutomationSecurity assignment must not be swallowed."""
    session = msoffice.MsOfficeSession()

    class RefusesHardening:
        Visible = False
        DisplayAlerts = False

        def __setattr__(self, name, value):
            if name == "AutomationSecurity":
                raise RuntimeError("policy forbids changing automation security")
            object.__setattr__(self, name, value)

        def Quit(self):
            pass

    with mock.patch.object(msoffice, "_com") as com:
        com.return_value.DispatchEx.return_value = RefusesHardening()
        with pytest.raises(msoffice.MsOfficeError):
            session._app("word")


def test_word_and_powerpoint_suppress_external_updates():
    """The promise 'external updates disabled' must be implemented, not stated."""
    source = Path(msoffice.__file__).read_text(encoding="utf-8")
    word = source[source.index("def _convert_word("):source.index("def _convert_excel(")]
    powerpoint = source[source.index("def _convert_powerpoint("):]
    assert "UpdateLinks" in word or "UpdateLinksAtOpen" in word, (
        "Word opens documents with no link/field update suppression"
    )
    # Not a bare "Update" substring: that is satisfied by the very call this
    # must forbid. The refresh action is an argument-less method that performs
    # the fetch rather than suppressing it, and the old assertion passed over it
    # for exactly that reason. The behavioural check lives in
    # test_confirmed_defects.py; this one keeps the call from coming back.
    #
    # Comments are stripped first - otherwise the guard trips on prose that
    # merely names the call it forbids.
    code = "\n".join(line.split("#", 1)[0] for line in powerpoint.splitlines())
    assert "UpdateLinks()" not in code, (
        "Presentation.UpdateLinks() performs the external fetch it is supposed "
        "to prevent; set LinkFormat.AutoUpdate on the shapes instead"
    )
    assert "AutoUpdate" in code, (
        "PowerPoint opens presentations with no link update suppression"
    )


# --------------------------------------------------------------------------- #
# C-16 - CSV normalization failure must not silently convert the raw source
# --------------------------------------------------------------------------- #

def test_csv_normalization_failure_does_not_reach_the_backend(tmp_path,
                                                              monkeypatch):
    """Converting the un-normalized source would ignore the detected dialect."""
    src = tmp_path / "data.csv"
    src.write_bytes("a;b\n1;2\n".encode("utf-8"))

    def explode(*_a, **_k):
        raise OSError("cannot write the normalized copy")

    monkeypatch.setattr(ops_office, "normalize_csv_for_import", explode)
    reached = []
    monkeypatch.setattr(msoffice, "convert_to_pdf",
                        lambda *a, **k: reached.append(a))

    dialect = app.office.detect_csv_dialect(src)
    job = {"src": src, "family": "csv", "out": tmp_path / "out.pdf",
           "csv_dialect": dialect}
    outcome = ops_office._convert_one(object(), job,
                                      cb.BackendChoice(cb.MSOFFICE, "Excel"))

    assert outcome == "fail", "the file must be failed, not silently converted"
    assert reached == [], "the backend must not receive the un-normalized source"
    assert not job["out"].exists()


@windows_only
def test_excel_bom_copy_is_streamed(tmp_path):
    """C-16b: the BOM copy must not hold the whole CSV in memory."""
    import tracemalloc

    src = tmp_path / "big.csv"
    with src.open("w", encoding="utf-8", newline="") as handle:
        for index in range(200_000):
            handle.write(f"{index},value-{index},سلام\n")
    size = src.stat().st_size
    assert size > 5 * 1024 * 1024, f"fixture too small: {size}"

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    tracemalloc.start()
    try:
        result = msoffice._csv_with_bom(src, scratch)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.read_bytes()[:3] == b"\xef\xbb\xbf"
    assert peak < 8 * 1024 * 1024, (
        f"peak {peak / 1e6:.1f} MB for a {size / 1e6:.1f} MB file - not streamed"
    )


def test_bom_copy_preserves_content_exactly(tmp_path):
    src = tmp_path / "fa.csv"
    payload = "نام,توضیح\nسلام,دنیا 😀\n".encode("utf-8")
    src.write_bytes(payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = msoffice._csv_with_bom(src, scratch)
    assert result.read_bytes() == b"\xef\xbb\xbf" + payload
    assert src.read_bytes() == payload, "the source must never be modified"
