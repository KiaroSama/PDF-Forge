"""Where the LibreOffice runtime lives, and whether it can actually be used.

The base layer of the conversion runtime: project paths, the pinned metadata,
the shared timeouts, and the discovery logic that decides which ``soffice``
(and which bundled Python) a conversion should run against.

:mod:`pdf_forge.office_server` and :mod:`pdf_forge.office_provision` build on
this module; :mod:`pdf_forge.office_runtime` re-exports the whole public API so
callers keep using a single ``office_runtime`` namespace.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .constants import *  # noqa: F401,F403



class OfficeRuntimeError(RuntimeError):
    """Raised when the local conversion runtime is missing or misbehaves."""


# Env override to point at an already-installed LibreOffice instead of the
# project-local runtime (the project-local runtime is still preferred by
# default; this only takes effect when explicitly set).
_SOFFICE_ENV = "PDF_FORGE_SOFFICE"

# Bounded timeouts (seconds). A timeout is a failure signal, never a password
# attempt limit.
SERVER_START_TIMEOUT = 90
# Base allowance for a small document. Real presentations with embedded media
# run to hundreds of megabytes and legitimately need longer, so the effective
# timeout scales with the input size (see conversion_timeout_for).
CONVERT_TIMEOUT = 180
# Extra seconds granted per megabyte of input.
CONVERT_SECONDS_PER_MB = 4
# Ceiling, so a pathological input still cannot block a queue forever.
CONVERT_TIMEOUT_MAX = 3600


def conversion_timeout_for(path) -> int:
    """Timeout allowance for converting ``path``, scaled by its size.

    A fixed 180s was fine for small documents but silently failed a 120 MB
    PowerPoint that converts correctly given time. The value is still bounded:
    a timeout remains a failure signal, never an attempt limit.
    """
    try:
        megabytes = Path(path).stat().st_size / (1024 * 1024)
    except OSError:
        return CONVERT_TIMEOUT
    allowance = CONVERT_TIMEOUT + int(megabytes * CONVERT_SECONDS_PER_MB)
    return max(CONVERT_TIMEOUT, min(allowance, CONVERT_TIMEOUT_MAX))
# Provisioning unpacks a ~360 MB package; generous, but never unbounded.
EXTRACT_TIMEOUT = 1800
# Abort sooner when Windows Installer writes nothing at all (a silent stall).
NO_PROGRESS_TIMEOUT = 180


def runtime_root() -> Path:
    """The git-ignored ``.tools`` directory at the project root."""
    return Path(__file__).resolve().parent.parent / ".tools"


def libreoffice_dir() -> Path:
    """The project-local LibreOffice runtime directory."""
    return runtime_root() / "libreoffice"


def _meta_path() -> Path:
    """Tracked runtime metadata (pinned version, source URL, checksum)."""
    return Path(__file__).resolve().parent.parent / "office_runtime_meta.json"


def load_runtime_meta() -> dict:
    """Load the pinned LibreOffice provisioning metadata (tracked JSON)."""
    try:
        return json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OfficeRuntimeError(
            f"Could not read runtime metadata ({_meta_path().name}): {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def _soffice_names() -> List[str]:
    """Executable names to look for, best first.

    On Windows prefer ``soffice.exe``, NOT the ``soffice.com`` console shim:
    the shim only launches the real binary and then exits, so unoserver - which
    watches the PID it spawned - concludes "LibreOffice died" and disposes the
    UNO bridge in the middle of the first export.
    """
    if os.name == "nt":
        return ["soffice.exe", "soffice.com"]
    return ["soffice"]


def _search_soffice_under(base: Path) -> Optional[Path]:
    """Find a soffice binary somewhere under ``base`` (program/ subdir first)."""
    if not base.exists():
        return None
    for name in _soffice_names():
        direct = base / "program" / name
        if direct.exists():
            return direct
    # Administrative extraction can nest under LibreOffice/program.
    for name in _soffice_names():
        for candidate in base.rglob(name):
            if candidate.is_file():
                return candidate
    return None


def find_soffice() -> Optional[Path]:
    """Locate the soffice executable to use.

    Order: project-local runtime first (preferred), then an explicit override
    via ``PDF_FORGE_SOFFICE``. A system-wide LibreOffice is used only when that
    env var points at it - never auto-registered or modified.
    """
    # Version probing is skipped here: this is the cheap path used while a
    # server is being started, and completeness (soffice + bundled Python) is
    # what decides whether a candidate can work at all.
    chosen = select_runtime(verify_version=False)
    return chosen.soffice if chosen else None


def find_soffice_python() -> Optional[Path]:
    """Locate LibreOffice's bundled Python (runs the unoserver server).

    The bundled interpreter is the only one that can ``import uno`` reliably on
    Windows, so the server runs under it while loading the venv's unoserver via
    ``PYTHONPATH``.
    """
    soffice = find_soffice()
    if soffice is None:
        return None
    program = soffice.parent
    for name in ("python.exe", "python3", "python"):
        candidate = program / name
        if candidate.exists():
            return candidate
    return None


def venv_site_packages() -> List[str]:
    """Return the *site-packages* directories of the running interpreter.

    Only real ``site-packages`` folders are returned. ``site.getsitepackages()``
    also yields ``sys.prefix`` itself in a virtualenv, and putting that on
    ``PYTHONPATH`` breaks LibreOffice's embedded Python (it starts resolving its
    own stdlib against the venv and the UNO bridge dies on first use). The
    server only needs the directory that actually contains ``unoserver``.
    """
    import site

    candidates: List[str] = []
    try:
        candidates.extend(site.getsitepackages())
    except AttributeError:  # pragma: no cover - virtualenv edge
        pass
    candidates.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    candidates.append(os.path.join(sys.prefix, "lib", "site-packages"))

    seen, out = set(), []
    for path in candidates:
        if not path or path in seen or not os.path.isdir(path):
            continue
        if os.path.basename(path.rstrip("\\/")).lower() != "site-packages":
            continue  # never the venv root itself
        seen.add(path)
        out.append(path)
    return out


def unoserver_installed() -> bool:
    """True when the venv has the unoserver package importable."""
    import importlib.util

    return importlib.util.find_spec("unoserver") is not None


def unoserver_version() -> Optional[str]:
    """Installed unoserver version string, or ``None`` when not installed."""
    try:
        from importlib.metadata import version

        return version("unoserver")
    except Exception:  # noqa: BLE001 - not installed / metadata missing
        return None


def marker_version(base: Optional[Path] = None) -> Optional[str]:
    """Version recorded by provisioning. A cache hint only - never authority."""
    root = base if base is not None else libreoffice_dir()
    try:
        return json.loads(
            (root / ".provisioned.json").read_text(encoding="utf-8")
        ).get("version")
    except (OSError, ValueError):
        return None


def probe_soffice_version(soffice: Path, timeout: int = 60) -> Optional[str]:
    """Ask the actual binary for its version, or ``None`` if it cannot answer.

    A provisioning marker can be stale, hand-edited, or left behind by a
    half-removed runtime, so the binary is the authority (PF-037).

    On Windows the probe must use ``soffice.com``: ``soffice.exe`` is a GUI
    binary that writes nothing to a captured stdout, so probing it would report
    "no version" for a perfectly good runtime. (The *server* still launches
    soffice.exe - the .com shim exits immediately and unoserver would conclude
    LibreOffice had died.)
    """
    probe = soffice
    if os.name == "nt" and soffice.suffix.lower() == ".exe":
        console = soffice.with_suffix(".com")
        if console.exists():
            probe = console
    try:
        result = subprocess.run(
            [str(probe), "--version"], capture_output=True, text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr or "").strip()
    match = re.search(r"(\d+\.\d+(?:\.\d+)*)", text)
    return match.group(1) if match else None


@dataclass
class RuntimeCandidate:
    """A complete, self-consistent LibreOffice runtime (or an explained reject).

    Resolution works on *candidates*, not individual paths, so a broken
    project-local runtime can no longer mask a valid explicit override (PF-016).
    """

    source: str                       # "project-local" | "PDF_FORGE_SOFFICE"
    soffice: Optional[Path] = None
    python: Optional[Path] = None
    version: Optional[str] = None
    complete: bool = False
    reason: str = ""


def _describe_candidate(source: str, base: Optional[Path],
                        soffice: Optional[Path], verify_version: bool) -> RuntimeCandidate:
    """Build a candidate and say precisely why it is or is not usable."""
    if soffice is None:
        return RuntimeCandidate(source=source, reason="no soffice executable found")
    python = None
    program = soffice.parent
    for name in ("python.exe", "python3", "python"):
        if (program / name).exists():
            python = program / name
            break
    if python is None:
        return RuntimeCandidate(
            source=source, soffice=soffice,
            reason="LibreOffice's bundled Python is missing (incomplete runtime)",
        )
    version = probe_soffice_version(soffice) if verify_version else marker_version(base)
    if verify_version and version is None:
        return RuntimeCandidate(
            source=source, soffice=soffice, python=python,
            reason="the binary did not report a version (corrupt or blocked)",
        )
    return RuntimeCandidate(source=source, soffice=soffice, python=python,
                            version=version, complete=True)


def resolve_runtime_candidates(verify_version: bool = True):
    """Every candidate runtime, best first, each marked complete or rejected."""
    candidates = []
    local_root = libreoffice_dir()
    candidates.append(_describe_candidate(
        "project-local", local_root, _search_soffice_under(local_root), verify_version
    ))
    override = os.environ.get(_SOFFICE_ENV)
    if override:
        path = Path(override)
        found = path if path.is_file() else _search_soffice_under(path)
        base = path if path.is_dir() else (found.parent.parent if found else None)
        candidates.append(
            _describe_candidate(_SOFFICE_ENV, base, found, verify_version)
        )
    return candidates


def select_runtime(verify_version: bool = True) -> Optional[RuntimeCandidate]:
    """The first *complete* candidate: project-local preferred, override next."""
    for candidate in resolve_runtime_candidates(verify_version):
        if candidate.complete:
            return candidate
    return None


def libreoffice_version() -> Optional[str]:
    """Version of the runtime actually in use (probed from the binary)."""
    candidate = select_runtime()
    return candidate.version if candidate else None


def runtime_status(verify_version: bool = True) -> dict:
    """Summarize the conversion runtime, based on a complete verified candidate.

    ``ready`` is true only when a candidate is complete *and* its binary
    answered a version probe, so a forged marker or a half-removed runtime can
    never report ready (PF-037). Rejected candidates are reported with the
    reason they were skipped (PF-016).
    """
    candidates = resolve_runtime_candidates(verify_version)
    chosen = next((c for c in candidates if c.complete), None)
    return {
        "unoserver_installed": unoserver_installed(),
        "unoserver_version": unoserver_version(),
        "soffice": str(chosen.soffice) if chosen else None,
        "soffice_python": str(chosen.python) if chosen else None,
        "libreoffice_version": chosen.version if chosen else None,
        "runtime_source": chosen.source if chosen else None,
        "rejected": [
            {"source": c.source, "reason": c.reason,
             "soffice": str(c.soffice) if c.soffice else None}
            for c in candidates if not c.complete
        ],
        "ready": bool(chosen) and unoserver_installed(),
    }


def random_localhost_port() -> int:
    """Reserve a free localhost TCP port (bind to 0, read it back, release)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


