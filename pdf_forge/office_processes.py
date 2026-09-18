# -*- coding: utf-8 -*-
"""The LibreOffice processes that are using *our* conversion profile.

One responsibility, two halves: reading them (:func:`soffice_processes`, the
evidence :mod:`.office_diagnostics` reports on) and acting on them
(:func:`kill_profile_owners`, the reaper that keeps an abandoned soffice.bin
from sitting on hundreds of megabytes). Both are scoped by the profile path,
which is what makes acting on the result safe: that directory is a fresh
``mkdtemp`` owned by this process, so a LibreOffice the user has open cannot
match it.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ['profile_glob', 'soffice_processes', 'SofficeSample',
           'kill_profile_owners']


#: Bound on the helper that lists processes. Generous next to its ~0.3 s cost,
#: but finite: this runs on a failure path that has already spent the whole
#: start timeout, and a diagnostic that hangs would turn a reported stall into
#: an unreported one - the exact defect B-01 is about.
_QUERY_TIMEOUT = 20

#: Win32 FILETIME unit: CPU times come back in 100-nanosecond ticks.
_TICKS_PER_SECOND = 10_000_000


def _powershell() -> str:
    """Resolve powershell.exe without trusting PATH.

    Imported lazily from :mod:`.office_server`, which imports this module: at
    call time the cycle is already resolved, at module scope it would not be.
    """
    from .office_server import _system32

    return _system32("powershell")


@dataclass(frozen=True)
class SofficeSample:
    """One LibreOffice process owned by our profile, at one instant."""

    pid: int
    cpu_seconds: float
    rss_bytes: int


def profile_glob(profile_dir: Path) -> str:
    """The PowerShell ``-like`` pattern that matches soffice using *our* profile.

    Matching the URI form is not cosmetic and must not be "simplified" back to
    the native path. We pass ``--user-installation`` a plain path, but unoserver
    converts it with ``Path(...).as_uri()`` before launching soffice, so the URI
    is what is actually on the command line. A glob built from the native path
    contains backslashes, which PowerShell's ``-like`` treats as literal escape
    characters, so it can never match. Measured against live processes: the
    native form matched 0, this form matched 2.

    Scoping to the profile path is also what makes any action taken on the
    result safe - that directory is a fresh ``mkdtemp`` owned by this process,
    so a LibreOffice the user has open cannot match it.
    """
    return f"*{profile_dir.resolve().as_uri()}*"


def soffice_processes(profile_dir: Path) -> list[SofficeSample]:
    """Sample the LibreOffice processes currently using *our* profile.

    Returns an empty list on any failure, and on POSIX, where the measured
    fault and the shipped runtime are both Windows.
    """
    if os.name != 'nt':
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='soffice.bin' or Name='soffice.exe'\" | "
        "Where-Object { $_.CommandLine -like $env:PDFFORGE_PROFILE_GLOB } | "
        "ForEach-Object { "
        "'{0} {1} {2}' -f $_.ProcessId, "
        "($_.KernelModeTime + $_.UserModeTime), $_.WorkingSetSize }"
    )
    try:
        completed = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive",
             "-Command", script],
            capture_output=True, timeout=_QUERY_TIMEOUT,
            # Through the environment, so a profile path containing
            # backslashes, spaces or quotes cannot be reinterpreted as
            # PowerShell syntax.
            env=dict(os.environ, PDFFORGE_PROFILE_GLOB=profile_glob(profile_dir)),
        )
    except (OSError, subprocess.SubprocessError):
        return []

    samples: list[SofficeSample] = []
    for line in completed.stdout.decode('utf-8', 'replace').splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ticks, rss = (int(p) for p in parts)
        except ValueError:
            continue
        samples.append(SofficeSample(pid, ticks / _TICKS_PER_SECOND, rss))
    return samples


def kill_profile_owners(profile_dir: Path) -> None:
    """Kill LibreOffice processes still using *our* profile, and only ours.

    ``_terminate`` takes down the whole process tree, but only while the
    unoserver parent is alive to define it. Once that parent has exited, its
    soffice.bin child is orphaned: nothing reaps it, it recreates the
    user-installation directory moments after ``stop()`` deleted it, and it sits
    on hundreds of megabytes. Measured: two survivors after one suite run, and a
    later server refusing to start.

    Matching on the profile path is what makes this safe. That directory is a
    fresh ``mkdtemp`` owned by this process, so a LibreOffice the user has open
    cannot match it - the promise of "task-owned processes, nothing else" holds.
    """
    if os.name != "nt":
        # POSIX orphans are not addressed here; the measured failure and the
        # runtime this ships against are Windows.
        return
    script = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='soffice.bin' or Name='soffice.exe'\" | "
        "Where-Object { $_.CommandLine -like $env:PDFFORGE_PROFILE_GLOB } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive",
             "-Command", script],
            capture_output=True, timeout=30,
            # profile_glob explains why the URI form is required and why the
            # pattern travels through the environment rather than the command.
            env=dict(os.environ,
                     PDFFORGE_PROFILE_GLOB=profile_glob(profile_dir)),
        )
    except (OSError, subprocess.SubprocessError):
        pass
