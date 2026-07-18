# -*- coding: utf-8 -*-
"""Regression tests for runtime candidate resolution and version trust.

Covers PF-016 (a broken project-local runtime must not mask a valid explicit
override) and PF-037 (the version comes from the binary; a marker is only a
cache hint, so a forged one cannot make the runtime report ready).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf_forge import office_runtime as ort  # noqa: E402


def fake_runtime(base: Path, with_python: bool = True, marker: str = None) -> Path:
    """A directory that looks like a provisioned LibreOffice runtime."""
    program = base / "program"
    program.mkdir(parents=True, exist_ok=True)
    soffice = program / ("soffice.exe" if sys.platform == "win32" else "soffice")
    soffice.write_text("", encoding="utf-8")
    if with_python:
        (program / ("python.exe" if sys.platform == "win32" else "python")).write_text(
            "", encoding="utf-8"
        )
    if marker is not None:
        import json

        (base / ".provisioned.json").write_text(
            json.dumps({"version": marker}), encoding="utf-8"
        )
    return base


# --------------------------------------------------------------------------- #
# PF-016 - candidate resolution
# --------------------------------------------------------------------------- #

def test_incomplete_local_runtime_does_not_mask_a_valid_override(tmp_path, monkeypatch):
    broken = fake_runtime(tmp_path / "local", with_python=False)   # no bundled Python
    good = fake_runtime(tmp_path / "override", with_python=True)

    monkeypatch.setattr(ort, "libreoffice_dir", lambda: broken)
    monkeypatch.setenv("PDF_FORGE_SOFFICE", str(good))

    chosen = ort.select_runtime(verify_version=False)
    assert chosen is not None, "a complete override must be selected"
    assert chosen.source == "PDF_FORGE_SOFFICE"
    assert str(good) in str(chosen.soffice)


def test_complete_local_runtime_is_preferred_over_an_override(tmp_path, monkeypatch):
    local = fake_runtime(tmp_path / "local")
    other = fake_runtime(tmp_path / "override")
    monkeypatch.setattr(ort, "libreoffice_dir", lambda: local)
    monkeypatch.setenv("PDF_FORGE_SOFFICE", str(other))

    chosen = ort.select_runtime(verify_version=False)
    assert chosen.source == "project-local"


def test_both_invalid_reports_every_rejection_reason(tmp_path, monkeypatch):
    broken = fake_runtime(tmp_path / "local", with_python=False)
    monkeypatch.setattr(ort, "libreoffice_dir", lambda: broken)
    monkeypatch.setenv("PDF_FORGE_SOFFICE", str(tmp_path / "nothing-here"))

    assert ort.select_runtime(verify_version=False) is None
    status = ort.runtime_status(verify_version=False)
    assert status["ready"] is False
    reasons = {r["source"]: r["reason"] for r in status["rejected"]}
    assert "project-local" in reasons and "Python" in reasons["project-local"]
    assert "PDF_FORGE_SOFFICE" in reasons


def test_missing_soffice_is_explained(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(ort, "libreoffice_dir", lambda: empty)
    monkeypatch.delenv("PDF_FORGE_SOFFICE", raising=False)
    status = ort.runtime_status(verify_version=False)
    assert status["ready"] is False
    assert any("soffice" in r["reason"] for r in status["rejected"])


# --------------------------------------------------------------------------- #
# PF-037 - the binary is the authority, not the marker
# --------------------------------------------------------------------------- #

def test_forged_marker_cannot_make_a_runtime_ready(tmp_path, monkeypatch):
    """A marker claiming a version means nothing if the binary cannot answer."""
    forged = fake_runtime(tmp_path / "forged", marker="99.9.9")
    monkeypatch.setattr(ort, "libreoffice_dir", lambda: forged)
    monkeypatch.delenv("PDF_FORGE_SOFFICE", raising=False)

    assert ort.marker_version(forged) == "99.9.9", "marker is readable"
    # The fake soffice is not executable, so the probe cannot return a version.
    status = ort.runtime_status(verify_version=True)
    assert status["ready"] is False, "a forged marker must not report ready"
    assert any("version" in r["reason"] for r in status["rejected"])


def test_probe_failure_is_reported_not_guessed(tmp_path, monkeypatch):
    runtime = fake_runtime(tmp_path / "rt", marker="25.8.7")
    monkeypatch.setattr(ort, "libreoffice_dir", lambda: runtime)
    monkeypatch.delenv("PDF_FORGE_SOFFICE", raising=False)
    monkeypatch.setattr(ort, "probe_soffice_version", lambda *_a, **_k: None)
    status = ort.runtime_status(verify_version=True)
    assert status["libreoffice_version"] is None
    assert status["ready"] is False


def test_probe_timeout_does_not_raise(tmp_path, monkeypatch):
    import subprocess

    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="soffice", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert ort.probe_soffice_version(tmp_path / "soffice.exe") is None


@pytest.mark.skipif(not ort.runtime_status(verify_version=False)["soffice"],
                    reason="no provisioned LibreOffice on this machine")
def test_real_runtime_reports_a_probed_version():
    """With a real runtime present, the version must come from the binary."""
    status = ort.runtime_status(verify_version=True)
    assert status["ready"] is True
    assert status["libreoffice_version"], "the binary must report a version"
    # The probe is at least as specific as the marker.
    marker = ort.marker_version()
    if marker:
        assert status["libreoffice_version"].startswith(marker.split(".")[0])
