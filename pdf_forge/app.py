from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .logsetup import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .watermark import *  # noqa: F401,F403
from .ops_watermark import *  # noqa: F401,F403
from . import office_runtime as _office_runtime
from .menus import *  # noqa: F401,F403

__all__ = ['main', 'print_diagnostics']


def print_diagnostics() -> int:
    """Print exactly which code and runtime this process is executing.

    Answers "is the launcher running the checkout I think it is?" - the failure
    mode where an installed command still points at an old copy. Every launch
    method (``python -m pdf_forge``, ``Run.ps1``, the installed ``pdf-forge``
    command) should print the same paths and commit.
    """
    import subprocess

    from . import ops_merge

    root = Path(__file__).resolve().parent.parent
    commit = "(not a git checkout)"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            commit = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    print_heading(f"\n{APP_NAME} diagnostics")
    print_kv("Version", APP_VERSION, Color.LIME)
    print_kv("Repository root", root, Color.AQUA)
    print_kv("Commit", commit, Color.GOLD)
    print_kv("Package file", Path(__file__).resolve(), Color.CYAN)
    print_kv("ops_merge file", Path(ops_merge.__file__).resolve(), Color.CYAN)
    print_kv("Python", sys.version.split()[0], Color.MAGENTA)
    print_kv("Executable", sys.executable, Color.MAGENTA)
    print_kv("Working directory", os.getcwd(), Color.GRAY)

    status = _office_runtime.runtime_status()
    status.update({
        "runtime_root": str(_office_runtime.runtime_root()),
        "libreoffice_dir": str(_office_runtime.libreoffice_dir()),
        "venv_site_packages": _office_runtime.venv_site_packages(),
        "python": sys.executable,
    })
    print_heading("\nConvert-to-PDF runtime")
    print_kv("unoserver", status["unoserver_version"] or "not installed", Color.LIME)
    print_kv("LibreOffice", status["libreoffice_version"] or "not provisioned",
             Color.LIME)
    print_kv("soffice", status["soffice"] or "(none)", Color.AQUA)
    print_kv("Runtime dir", status["libreoffice_dir"], Color.GRAY)
    print_kv("Ready", "yes" if status["ready"] else "no",
             Color.GREEN if status["ready"] else Color.RED)
    logger.info("Diagnostics: %s", status)
    return 0


def _setup_office_runtime(force: bool = False) -> int:
    """Provision the project-local LibreOffice runtime (idempotent)."""
    print_heading(f"\n{APP_NAME} - convert-to-PDF runtime setup")
    meta = _office_runtime.load_runtime_meta()
    print_kv("LibreOffice version", meta["version"], Color.LIME)
    print_kv("Source", meta["windows"]["url"], Color.AQUA)
    print_kv("Target", _office_runtime.libreoffice_dir(), Color.GRAY)
    print_note(
        "This downloads the official LibreOffice package, verifies its pinned "
        "checksum, and extracts it into the project folder only. Nothing is "
        "installed system-wide: no PATH, registry, shortcut, or service change."
    )
    try:
        result = _office_runtime.provision_runtime(
            progress=lambda message: print_info(f"  {message}"), force=force
        )
    except _office_runtime.OfficeRuntimeError as exc:
        print_error(f"Setup failed: {exc}")
        logger.error("Office runtime setup failed: %s", exc)
        return 1
    print_success(f"Runtime {result['status']}: {result.get('soffice')}")
    return 0

def main(argv: Optional[Sequence[str]] = None) -> int:
    """Application entry point."""
    enable_ansi_colors()
    # __file__ is inside the pdf_forge/ package; the project root (where logs/
    # and temp/ live) is its parent.
    script_dir = Path(__file__).resolve().parent.parent
    log_path = setup_logging(script_dir)

    logger.info("=== %s v%s starting ===", APP_NAME, APP_VERSION)
    logger.info("Python %s on %s", sys.version.split()[0], sys.platform)
    logger.info("Executable: %s", sys.executable)
    logger.info("Operating system: %s", os.name)
    logger.info("Script directory: %s", script_dir)
    logger.info("Working directory: %s", os.getcwd())
    if log_path is not None:
        logger.info("Log file: %s", log_path)
    else:
        logger.warning("Persistent file logging is unavailable; using console fallback.")

    # Non-interactive maintenance modes.
    args = list(argv or [])
    if "--diagnose" in args or "--version" in args:
        return print_diagnostics()
    if "--setup-office" in args:
        return _setup_office_runtime(force="--force" in args)
    if "--clean-office" in args:
        removed = _office_runtime.clean_runtime()
        print_success(
            "Removed the project-local LibreOffice runtime."
            if removed else "No project-local LibreOffice runtime to remove."
        )
        return 0

    # Clear the project-local temp folder (e.g. leftover preview images).
    cleanup_temp_dir()

    print_banner(APP_NAME)
    if log_path is not None:
        print_note(f"Logging to: {log_path}")

    # Verify the PDF backend early for a friendly message. PyMuPDF drives every
    # operation (page tools, merge, render, compress, watermark removal).
    try:
        _import_pymupdf()
    except RuntimeError as exc:
        print_error(str(exc))
        logger.critical("PDF backend import failed: %s", exc)
        return 2

    try:
        exit_code = main_menu()
    except KeyboardInterrupt:
        print_warning("\nInterrupted. Exiting.")
        logger.warning("Application interrupted at top level.")
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 - log any unexpected top-level error
        print_error(f"Unexpected error: {exc}")
        logger.exception("Unhandled top-level exception.")
        exit_code = 1
    finally:
        logger.info("=== %s shutting down (exit code %s) ===", APP_NAME, exit_code)
        logging.shutdown()

    return exit_code
