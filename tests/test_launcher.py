# -*- coding: utf-8 -*-
"""Regression tests for the PowerShell launcher bootstrap.

Covers PF-033 (dependency freshness must use a content hash, not requirements
mtime) and PF-034 (an existing virtual environment is revalidated on every
launch rather than trusted because python.exe exists).
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "Run.ps1"

pwsh = shutil.which("pwsh") or shutil.which("powershell")
requires_pwsh = pytest.mark.skipif(pwsh is None, reason="PowerShell not available")


def launcher_text() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def extract_function(name: str) -> str:
    text = launcher_text()
    match = re.search(rf"function {name} \{{.*?\n\}}", text, re.S)
    assert match, f"{name} not found in Run.ps1"
    return match.group(0)


def run_ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run([pwsh, "-NoProfile", "-Command", script],
                          capture_output=True, text=True, timeout=180)


# --------------------------------------------------------------------------- #
# PF-033 - content-hash dependency stamp
# --------------------------------------------------------------------------- #

def test_freshness_no_longer_depends_on_mtime_alone():
    text = launcher_text()
    assert "Get-DependencyHash" in text, "freshness must be content-based"
    assert "LastWriteTimeUtc" not in text, (
        "requirements mtime must no longer decide whether to reinstall"
    )


@requires_pwsh
def test_hash_changes_with_content_even_when_mtime_is_preserved(tmp_path):
    """The reported failure: edit requirements, restore the timestamp."""
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("pymupdf==1.0.0\n", encoding="utf-8")

    script = extract_function("Get-DependencyHash") + f"""
$f = '{reqs}'
$before = Get-DependencyHash -Files @($f) -PythonPath '{sys.executable}'
$stamp = (Get-Item $f).LastWriteTimeUtc
Set-Content -LiteralPath $f -Value 'pymupdf==2.0.0' -Encoding UTF8
(Get-Item $f).LastWriteTimeUtc = $stamp      # preserve the timestamp
$after = Get-DependencyHash -Files @($f) -PythonPath '{sys.executable}'
Write-Output "$($before -eq $after)"
"""
    result = run_ps(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", (
        "changed contents with an unchanged mtime must change the hash"
    )


@requires_pwsh
def test_hash_includes_the_interpreter_identity(tmp_path):
    """A Python upgrade must force a reinstall."""
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("pymupdf==1.0.0\n", encoding="utf-8")
    script = extract_function("Get-DependencyHash") + f"""
$h = Get-DependencyHash -Files @('{reqs}') -PythonPath '{sys.executable}'
$h2 = Get-DependencyHash -Files @('{reqs}') -PythonPath 'definitely-not-python'
Write-Output "$($h -eq $h2)"
"""
    result = run_ps(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_stamp_is_written_atomically():
    text = launcher_text()
    assert "$StampFile.tmp" in text and "Move-Item" in text, (
        "the stamp must be written to a temp file and moved into place"
    )


def test_installed_packages_are_verified_after_install():
    text = launcher_text()
    assert "Test-DependenciesImportable" in text
    assert "import pymupdf" in text, "an install must be proven by importing"


# --------------------------------------------------------------------------- #
# PF-034 - the existing virtualenv is revalidated
# --------------------------------------------------------------------------- #

def test_launcher_revalidates_an_existing_venv():
    text = launcher_text()
    assert "Test-VenvHealthy" in text
    # It must not simply trust that python.exe is present.
    assert "sys.version_info" in text, "the interpreter version must be checked"


@requires_pwsh
def test_health_check_accepts_a_working_interpreter():
    script = extract_function("Test-VenvHealthy") + f"""
Write-Output (Test-VenvHealthy '{sys.executable}')
"""
    result = run_ps(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


@requires_pwsh
def test_health_check_rejects_a_missing_or_broken_interpreter(tmp_path):
    broken = tmp_path / "python.exe"
    broken.write_text("not an executable", encoding="utf-8")
    script = extract_function("Test-VenvHealthy") + f"""
$missing = Test-VenvHealthy '{tmp_path / "nope.exe"}'
$broken = Test-VenvHealthy '{broken}'
Write-Output "$missing|$broken"
"""
    result = run_ps(script)
    assert result.returncode == 0, result.stderr
    missing, broken_result = result.stdout.strip().split("|")
    assert missing == "False", "a missing interpreter must fail the check"
    assert broken_result == "False", "an unusable interpreter must fail the check"


@requires_pwsh
def test_launcher_parses():
    script = (
        "$errors=$null; "
        f"$null=[System.Management.Automation.Language.Parser]::ParseFile('{LAUNCHER}',"
        "[ref]$null,[ref]$errors); "
        "if($errors){$errors|ForEach-Object{$_.Message}; exit 1}else{'ok'}"
    )
    result = run_ps(script)
    assert result.returncode == 0, result.stdout + result.stderr
