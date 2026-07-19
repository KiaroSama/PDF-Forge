# -*- coding: utf-8 -*-
"""Convert-to-PDF runtime: discovery, provisioning safety, output validation,
resilience, and the conversion timeout.

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
    monkeypatch.setattr(app.office_discovery, "load_runtime_meta", lambda: {
        "version": "test",
        "windows": {"url": "https://example.invalid/x.msi", "sha256": "0" * 64},
    })
    empty = tmp_path / "no-runtime"
    empty.mkdir()
    monkeypatch.setattr(app.office_discovery, "libreoffice_dir", lambda: empty)
    monkeypatch.setattr(app.office_discovery, "runtime_root", lambda: tmp_path)

    def fake_download(url, dest):
        Path(dest).write_bytes(b"corrupted payload")

    with pytest.raises(app.office_runtime.OfficeRuntimeError) as excinfo:
        app.office_runtime.provision_runtime(download=fake_download)
    assert "checksum" in str(excinfo.value).lower()


def test_provisioning_refuses_when_no_checksum_is_pinned(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows-only administrative-extraction path")
    monkeypatch.setattr(app.office_discovery, "load_runtime_meta", lambda: {
        "version": "test",
        "windows": {"url": "https://example.invalid/x.msi"},
    })
    empty = tmp_path / "no-runtime3"
    empty.mkdir()
    monkeypatch.setattr(app.office_discovery, "libreoffice_dir", lambda: empty)
    monkeypatch.setattr(app.office_discovery, "runtime_root", lambda: tmp_path)
    with pytest.raises(app.office_runtime.OfficeRuntimeError) as excinfo:
        app.office_runtime.provision_runtime(
            download=lambda u, d: Path(d).write_bytes(b"x")
        )
    assert "checksum" in str(excinfo.value).lower()


def test_clean_runtime_only_touches_the_project_local_copy(tmp_path, monkeypatch):
    fake_runtime = tmp_path / "libreoffice"
    fake_runtime.mkdir()
    (fake_runtime / "marker.txt").write_text("x")
    monkeypatch.setattr(app.office_discovery, "libreoffice_dir", lambda: fake_runtime)
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



def _package_sources() -> str:
    """Every module in the package, concatenated.

    Scanned as a whole rather than one named file so these guarantees survive a
    refactor that moves the code between modules - and so a violation is caught
    wherever it is introduced.
    """
    package = Path(app.__file__).resolve().parent
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(package.glob("*.py")))


def test_convert_password_is_not_placed_on_a_command_line():
    """B4/B8: the password goes through the in-memory API, never argv/env."""
    source = _package_sources()
    assert '"--password"' not in source and "'--password'" not in source
    assert 'kwargs["password"] = password' in source


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
