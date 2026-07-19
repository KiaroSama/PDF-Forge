# -*- coding: utf-8 -*-
"""Regression tests for runtime candidate resolution and version trust.

Covers PF-016 (a broken project-local runtime must not mask a valid explicit
override) and PF-037 (the version comes from the binary; a marker is only a
cache hint, so a forged one cannot make the runtime report ready).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf_forge import office_runtime as ort  # noqa: E402
# The implementation modules behind the office_runtime facade. Internals are
# patched and inspected where they are DEFINED: patching the facade would not
# affect calls made inside these modules.
from pdf_forge import office_discovery as ort_discovery  # noqa: E402
from pdf_forge import office_provision as ort_provision  # noqa: E402
from pdf_forge import office_server as ort_server  # noqa: E402


def _package_sources() -> str:
    """Every module in the package, concatenated.

    Scanned as a whole rather than one named file so these guarantees survive
    a refactor that moves the code between modules.
    """
    package = Path(ort.__file__).resolve().parent
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(package.glob("*.py")))


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

    monkeypatch.setattr(ort_discovery, "libreoffice_dir", lambda: broken)
    monkeypatch.setenv("PDF_FORGE_SOFFICE", str(good))

    chosen = ort.select_runtime(verify_version=False)
    assert chosen is not None, "a complete override must be selected"
    assert chosen.source == "PDF_FORGE_SOFFICE"
    assert str(good) in str(chosen.soffice)


def test_complete_local_runtime_is_preferred_over_an_override(tmp_path, monkeypatch):
    local = fake_runtime(tmp_path / "local")
    other = fake_runtime(tmp_path / "override")
    monkeypatch.setattr(ort_discovery, "libreoffice_dir", lambda: local)
    monkeypatch.setenv("PDF_FORGE_SOFFICE", str(other))

    chosen = ort.select_runtime(verify_version=False)
    assert chosen.source == "project-local"


def test_both_invalid_reports_every_rejection_reason(tmp_path, monkeypatch):
    broken = fake_runtime(tmp_path / "local", with_python=False)
    monkeypatch.setattr(ort_discovery, "libreoffice_dir", lambda: broken)
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
    monkeypatch.setattr(ort_discovery, "libreoffice_dir", lambda: empty)
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
    monkeypatch.setattr(ort_discovery, "libreoffice_dir", lambda: forged)
    monkeypatch.delenv("PDF_FORGE_SOFFICE", raising=False)

    assert ort.marker_version(forged) == "99.9.9", "marker is readable"
    # The fake soffice is not executable, so the probe cannot return a version.
    status = ort.runtime_status(verify_version=True)
    assert status["ready"] is False, "a forged marker must not report ready"
    assert any("version" in r["reason"] for r in status["rejected"])


def test_probe_failure_is_reported_not_guessed(tmp_path, monkeypatch):
    runtime = fake_runtime(tmp_path / "rt", marker="25.8.7")
    monkeypatch.setattr(ort_discovery, "libreoffice_dir", lambda: runtime)
    monkeypatch.delenv("PDF_FORGE_SOFFICE", raising=False)
    monkeypatch.setattr(ort_discovery, "probe_soffice_version", lambda *_a, **_k: None)
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


# --------------------------------------------------------------------------- #
# PF-015 - a partial extraction is never accepted as installed
# --------------------------------------------------------------------------- #

def test_soffice_only_directory_is_not_accepted(tmp_path):
    """The reported case: only a discovered soffice left in an incomplete tree."""
    partial = fake_runtime(tmp_path / "partial", with_python=False)
    result = ort.verify_runtime_directory(partial)
    assert result.complete is False
    assert "Python" in result.reason


def test_interrupted_extraction_is_not_accepted(tmp_path):
    empty = tmp_path / "interrupted"
    empty.mkdir()
    result = ort.verify_runtime_directory(empty)
    assert result.complete is False
    assert "soffice" in result.reason


def test_marker_alone_does_not_make_a_runtime_complete(tmp_path):
    forged = fake_runtime(tmp_path / "forged", marker="25.8.7")
    # The fake soffice cannot report a version, so the tuple is incomplete.
    result = ort.verify_runtime_directory(forged)
    assert result.complete is False
    assert "version" in result.reason


@pytest.mark.skipif(os.name != "nt",
                    reason="Windows-only administrative-extraction path")
def test_provisioning_rebuilds_an_incomplete_runtime(tmp_path, monkeypatch):
    """An existing-but-broken runtime must be rebuilt, not reported as present."""
    broken = fake_runtime(tmp_path / "rt", with_python=False)
    monkeypatch.setattr(ort_discovery, "libreoffice_dir", lambda: broken)
    monkeypatch.setattr(ort_discovery, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(ort_discovery, "load_runtime_meta", lambda: {
        "version": "25.8.7",
        "windows": {"url": "https://example.invalid/x.msi", "sha256": "00"},
    })
    calls = {"download": 0}

    def fake_download(_url, dest):
        calls["download"] += 1
        Path(dest).write_bytes(b"msi")

    # Checksum verification is skipped so the test stops at extraction.
    with pytest.raises(ort.OfficeRuntimeError):
        ort.provision_runtime(download=fake_download, verify_checksum=False)
    assert calls["download"] == 1, "a broken runtime must trigger a rebuild"


def test_real_runtime_verifies_completely():
    """The provisioned runtime on this machine must pass the full tuple check."""
    if not ort.runtime_status(verify_version=False)["soffice"]:
        pytest.skip("no provisioned LibreOffice on this machine")
    result = ort.verify_runtime_directory(ort.libreoffice_dir())
    assert result.complete, result.reason
    assert result.python and result.version


# --------------------------------------------------------------------------- #
# PF-017 - Windows Installer service state is never changed
# --------------------------------------------------------------------------- #

def test_provisioning_never_starts_a_service():
    source = _package_sources()
    assert '"net", "start"' not in source and "net start msiserver\"" not in source, (
        "provisioning must not change Windows service state"
    )


def _fake_sc(monkeypatch, stdout):
    """Stand in for ``sc`` and record any attempt to change service state."""
    import subprocess as sp

    class Result:
        pass

    Result.stdout, Result.stderr, Result.returncode = stdout, "", 0
    touched = {"state": False}

    def fake_run(cmd, *a, **k):
        if cmd[:1] == ["net"] or (len(cmd) > 1 and cmd[1] in {"start", "config"}):
            touched["state"] = True
        return Result()

    monkeypatch.setattr(sp, "run", fake_run)
    return touched


def test_a_stopped_demand_start_service_is_not_a_blocker(monkeypatch):
    """msiserver is demand-start: stopped is its normal idle state.

    Regression - treating "stopped" as fatal rejected the common case and broke
    provisioning on a clean machine, where the SCM starts the service for
    msiexec on demand.
    """
    touched = _fake_sc(monkeypatch, (
        "SERVICE_NAME: msiserver\n"
        "        START_TYPE  : 3   DEMAND_START\n"
        "        STATE       : 1   STOPPED"
    ))
    ort_provision._ensure_installer_service()   # must not raise
    assert touched["state"] is False, "the service must not be started"


def test_running_service_passes(monkeypatch):
    _fake_sc(monkeypatch, "SERVICE_NAME: msiserver\n        STATE : 4  RUNNING")
    ort_provision._ensure_installer_service()   # must not raise


def test_disabled_service_is_reported_not_enabled(monkeypatch):
    touched = _fake_sc(monkeypatch, (
        "SERVICE_NAME: msiserver\n        START_TYPE  : 4   DISABLED"
    ))
    with pytest.raises(ort.OfficeRuntimeError) as excinfo:
        ort_provision._ensure_installer_service()
    assert "does not change Windows service state" in str(excinfo.value)
    assert touched["state"] is False, "the service must not be re-enabled"


# --------------------------------------------------------------------------- #
# PF-023 - macro and external-update safety is enforced, not just claimed
# --------------------------------------------------------------------------- #

def test_conversion_profile_is_hardened_by_default(tmp_path, monkeypatch):
    """Every conversion profile must carry the lockdown unless explicitly off."""
    monkeypatch.delenv("PDF_FORGE_HARDEN_PROFILE", raising=False)
    profile = tmp_path / "prof"
    profile.mkdir()
    ort_server._harden_profile(profile)
    xcu = (profile / "user" / "registrymodifications.xcu").read_text(encoding="utf-8")

    # Macros must not run.
    assert "MacroSecurityLevel" in xcu
    assert "DisableMacrosExecution" in xcu
    # External links / data updates must not fire during conversion.
    assert xcu.count("/Content/Update") >= 2, "Writer and Calc link updates"
    # No dialog may block a headless conversion.
    assert "FirstStartWizardCompleted" in xcu
    assert "RecoveryInfo" in xcu


def test_hardening_is_applied_unless_explicitly_disabled():
    source = _package_sources()
    assert 'PDF_FORGE_HARDEN_PROFILE") != "0"' in source, (
        "hardening must be the default, not opt-in"
    )


def test_hardened_profile_is_isolated_and_removed(tmp_path, monkeypatch):
    """The lockdown must never touch the user's own LibreOffice configuration."""
    profile = tmp_path / "prof"
    profile.mkdir()
    ort_server._harden_profile(profile)
    written = list(profile.rglob("registrymodifications.xcu"))
    assert len(written) == 1
    # It lives inside the per-run profile, which teardown deletes.
    assert profile in written[0].parents
