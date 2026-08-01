#!/usr/bin/env python
"""Run the CI gates on this machine, with the same commands the workflows use.

Purpose: get real evidence without spending GitHub Actions minutes. Every gate
below runs the *identical* command its workflow step runs, so a pass here means
the same thing it would mean there - for the parts this machine can cover.

It deliberately reports what it could NOT check rather than quietly narrowing
the definition of "green": the cross-platform, multi-interpreter and
hosted-only gates need real CI, and this script says so in its summary instead
of implying full coverage.

Usage:
    .venv\\Scripts\\python.exe scripts\\local-gates.py            # everything runnable
    .venv\\Scripts\\python.exe scripts\\local-gates.py --quick     # skip the slow ones
    .venv\\Scripts\\python.exe scripts\\local-gates.py --list      # show the plan
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PYTHON.exists():
    VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)


#: Wall ceilings. Every observed gate finishes far inside these: the full suite
#: ~110s, the real conversion E2E ~96s, everything else in seconds. A gate that
#: outruns its ceiling is a defect to investigate, never a slow gate to wait on.
DEFAULT_TIMEOUT = 300
SLOW_TIMEOUT = 600

#: Workers for the conversion E2E. Bounded by the ~370 MB LibreOffice each one
#: can hold, not by core count - see the gate's own note.
E2E_WORKERS = min(4, os.cpu_count() or 1)


@dataclass
class Gate:
    """One CI step reproduced locally."""

    name: str
    workflow: str
    command: Sequence[str]
    slow: bool = False
    shell: bool = False
    available: Callable[[], Optional[str]] = lambda: None
    env: dict = field(default_factory=dict)
    #: Override the wall ceiling for this gate; None takes the slow/fast default.
    timeout: Optional[int] = None


def _needs(tool: str) -> Callable[[], Optional[str]]:
    def check() -> Optional[str]:
        if shutil.which(tool) is None:
            return f"{tool} is not installed on this machine"
        return None
    return check


def _needs_module(module: str) -> Callable[[], Optional[str]]:
    def check() -> Optional[str]:
        probe = subprocess.run([PYTHON, "-c", f"import {module}"],
                               capture_output=True, cwd=str(ROOT))
        if probe.returncode != 0:
            return f"the {module} module is not installed in .venv"
        return None
    return check


GATES: List[Gate] = [
    Gate("Test suite", "ci.yml / test",
         [PYTHON, "-m", "pytest", "-q"], slow=True),
    Gate("Repository clean after the suite", "ci.yml / test",
         ["git", "status", "--porcelain"]),
    Gate("Ruff", "ci.yml / lint",
         [PYTHON, "-m", "ruff", "check", "pdf_forge", "tests"]),
    Gate("Mypy", "ci.yml / lint",
         [PYTHON, "-m", "mypy"]),
    # CI lints on ubuntu, so a Windows-only attribute (ctypes.WinDLL) passes
    # here and fails there. --platform makes that reachable locally.
    Gate("Mypy (as analysed on Linux)", "ci.yml / lint",
         [PYTHON, "-m", "mypy", "--platform", "linux"]),
    # Coverage is produced by the ubuntu/3.12 leg of the test matrix, not by a
    # separate job.
    Gate("Coverage threshold", "ci.yml / test (coverage leg)",
         [PYTHON, "-m", "pytest", "--cov", "--cov-report=term-missing"],
         slow=True),
    Gate("Dependency vulnerability audit", "ci.yml / security",
         [PYTHON, "-m", "pip_audit", "--strict", "--progress-spinner", "off"],
         slow=True, available=_needs_module("pip_audit")),
    Gate("Secret scan", "ci.yml / security",
         ["gitleaks", "detect", "--no-banner", "--redact"],
         available=_needs("gitleaks")),
    Gate("PSScriptAnalyzer", "ci.yml / powershell",
         ["pwsh", "-NoProfile", "-Command",
          "$r = Invoke-ScriptAnalyzer -Path . -Recurse -Severity Error "
          "-ExcludeRule PSAvoidUsingWriteHost; "
          "if ($r) { $r | Format-Table -AutoSize; exit 1 }; "
          "Write-Host 'PSScriptAnalyzer: no errors.'"],
         slow=True, available=_needs("pwsh")),
    Gate("CLI: --version", "ci.yml / cli-smoke",
         [PYTHON, "-m", "pdf_forge", "--version"]),
    Gate("CLI: --version agrees with APP_VERSION", "ci.yml / cli-smoke",
         [PYTHON, "-c",
          "import subprocess,sys,pdf_forge;"
          "out=subprocess.run([sys.executable,'-m','pdf_forge','--version'],"
          "capture_output=True,text=True,check=True).stdout;"
          "assert pdf_forge.APP_VERSION in out,(pdf_forge.APP_VERSION,out);"
          "print('version reported:',pdf_forge.APP_VERSION)"]),
    Gate("Built wheel metadata reports the version", "ci.yml / cli-smoke",
         [PYTHON, "-m", "pytest", "tests/test_wheel_metadata.py", "-q"],
         slow=True),
    Gate("CLI: --diagnose", "ci.yml / cli-smoke",
         [PYTHON, "-m", "pdf_forge", "--diagnose"]),
    Gate("Office runtime reports ready", "office-e2e.yml",
         [PYTHON, "-c",
          "import pdf_forge as a;s=a.office_runtime.runtime_status();"
          "print('LibreOffice',s['libreoffice_version'],'ready',s['ready']);"
          "raise SystemExit(0 if s['ready'] else 1)"]),
    # Both files, because the workflow runs both: a local gate that covers less
    # than CI reports green for a narrower thing than it claims.
    #
    # Same tests as CI, but the worker count is capped instead of `-n auto`.
    # Every worker here can own a headless LibreOffice of ~370 MB, and `auto`
    # counts cores while knowing nothing about that: measured on this 16-core
    # machine it put 12 soffice.bin up at once (~4.4 GB) with 6.4 GB free, and
    # once left two of them wedged. CI never sees this because its runners have
    # 4 cores - so 4 is the concurrency that is actually evidenced to work.
    Gate("Real conversion end-to-end tests", "office-e2e.yml",
         [PYTHON, "-m", "pytest", "tests/test_office_e2e.py",
          "tests/test_office_links.py", "-q", "-n", str(E2E_WORKERS)],
         slow=True, env={"PDF_FORGE_E2E": "1"}),
]

# Gates this machine structurally cannot reproduce, stated so the summary is
# honest about the limits of a local run.
UNCOVERABLE = [
    ("Linux test matrix", "ci.yml / test",
     "this is a Windows machine; the ubuntu-latest jobs cannot run here "
     "(type checking IS covered - see the --platform linux gate)"),
    ("Python 3.10 / 3.12 / 3.13 matrix", "ci.yml / test",
     "only Python 3.11 is installed; version-specific breakage (for example a "
     "stdlib module newer than the 3.10 floor) is invisible locally"),
]


def _terminate_tree(process: subprocess.Popen) -> None:
    """Take down a timed-out gate *and everything it started*.

    A gate does not hang in the process this script spawned; it hangs in a
    grandchild - a headless LibreOffice, behind unoserver, behind a pytest-xdist
    worker. ``Popen.kill()`` reaches only the direct child and orphans that
    grandchild, which then sits on its profile directory and hundreds of
    megabytes. ``office_server._terminate`` is the project's existing tree-kill
    (``taskkill /T`` on Windows, ``killpg`` on POSIX); reuse it rather than write
    a second one that has not been through the same failures.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from pdf_forge.office_server import _terminate
        _terminate(process)
    except Exception:  # noqa: BLE001 - cleanup must never mask the timeout
        process.kill()
    finally:
        try:
            process.wait(timeout=30)
        except Exception:  # noqa: BLE001
            pass


def run_gate(gate: Gate) -> tuple:
    reason = gate.available()
    if reason:
        return "skipped", reason, 0.0
    env = dict(os.environ, **gate.env)
    limit = gate.timeout or (SLOW_TIMEOUT if gate.slow else DEFAULT_TIMEOUT)
    started = time.perf_counter()
    process = subprocess.Popen(list(gate.command), cwd=str(ROOT), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    try:
        stdout, stderr = process.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        # Bounded, visible failure. Without this the whole gate run waits
        # forever on one wedged child and reports nothing at all.
        _terminate_tree(process)
        stdout, stderr = process.communicate()
        elapsed = time.perf_counter() - started
        tail = "\n".join(line for line in ((stdout or "") + (stderr or "")).splitlines()
                         if line.strip())
        return ("failed",
                f"no result after {limit}s - terminated, with its process tree.\n"
                f"Last output before the stall:\n{tail[-1200:]}",
                elapsed)
    result = subprocess.CompletedProcess(gate.command, process.returncode,
                                         stdout, stderr)
    elapsed = time.perf_counter() - started
    output = (result.stdout or "") + (result.stderr or "")

    if gate.name == "Repository clean after the suite":
        # The command always succeeds; its OUTPUT is the verdict.
        if output.strip():
            return "failed", "the checkout was modified:\n" + output.strip(), elapsed
        return "passed", "", elapsed

    if result.returncode != 0:
        tail = "\n".join(line for line in output.splitlines() if line.strip())
        return "failed", tail[-1500:], elapsed
    return "passed", "", elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="skip the slow gates (suite, coverage, audit, E2E)")
    parser.add_argument("--list", action="store_true",
                        help="print the plan and exit")
    args = parser.parse_args()

    gates = [g for g in GATES if not (args.quick and g.slow)]
    if args.list:
        for gate in gates:
            print(f"  {gate.name:<42} {gate.workflow}")
        return 0

    print(f"Running {len(gates)} gate(s) with {PYTHON}\n")
    results = []
    for gate in gates:
        print(f"  {gate.name:<42} ... ", end="", flush=True)
        status, detail, elapsed = run_gate(gate)
        symbol = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[status]
        print(f"{symbol}  ({elapsed:5.1f}s)")
        if detail and status == "skipped":
            print(f"       {detail}")
        results.append((gate, status, detail))

    failed = [(g, d) for g, s, d in results if s == "failed"]
    skipped = [(g, d) for g, s, d in results if s == "skipped"]

    print("\n" + "=" * 72)
    passed = sum(1 for _, s, _ in results if s == "passed")
    print(f"passed {passed}   failed {len(failed)}   skipped {len(skipped)}")

    for gate, detail in failed:
        print(f"\n--- FAILED: {gate.name}  ({gate.workflow})")
        print(detail)

    print("\nNot reproducible on this machine - still needs real CI:")
    for name, workflow, why in UNCOVERABLE:
        print(f"  - {name} ({workflow}): {why}")
    for gate, why in skipped:
        print(f"  - {gate.name} ({gate.workflow}): {why}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
