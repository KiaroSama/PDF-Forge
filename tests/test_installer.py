# -*- coding: utf-8 -*-
"""Regression tests for the PowerShell command installer.

Covers PF-042 (no unconditional -ExecutionPolicy Bypass in the installed
command) and PF-041 (stale PDF Forge PATH entries from a previous checkout
location are removed, unrelated entries are not).
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "Install-pdf-forgeCommand.ps1"

pwsh = shutil.which("pwsh") or shutil.which("powershell")
requires_pwsh = pytest.mark.skipif(pwsh is None, reason="PowerShell not available")


def installer_text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# PF-042 - the installed command must not bypass execution policy
# --------------------------------------------------------------------------- #

def test_installed_command_has_no_unconditional_bypass():
    text = installer_text()
    # The launcher invocations are the lines that actually start PowerShell.
    invocations = [
        line for line in text.splitlines()
        if "-File" in line and ("pwsh" in line or "powershell.exe" in line)
    ]
    assert invocations, "expected launcher invocation lines"
    for line in invocations:
        assert "-ExecutionPolicy Bypass" not in line, (
            f"the installed command still bypasses execution policy: {line.strip()}"
        )


def test_installer_explains_how_to_allow_scripts():
    """Declining to bypass is only acceptable with actionable guidance."""
    text = installer_text()
    assert "Set-ExecutionPolicy" in text
    assert "RemoteSigned" in text


def test_arguments_are_forwarded():
    assert "@args" in installer_text(), "the command must forward its arguments"


def test_launcher_path_is_single_quote_escaped():
    """A path containing a quote must not break the generated function."""
    text = installer_text()
    assert "$Launcher.Replace(\"'\", \"''\")" in text


# --------------------------------------------------------------------------- #
# PF-041 - stale PATH entries from an older checkout location
# --------------------------------------------------------------------------- #

def test_path_cleanup_is_not_limited_to_the_current_checkout():
    text = installer_text()
    assert "Test-IsPdfForgeBin" in text, (
        "PATH cleanup must be able to recognise a PDF Forge bin at any location"
    )
    # Ownership is verified against project marker files, not just the name.
    assert "pdf_forge\__init__.py" in text or "pdf_forge\\__init__.py" in text
    assert "Run.ps1" in text


@requires_pwsh
def test_ownership_check_accepts_only_real_pdf_forge_bins(tmp_path):
    """Drive the real Test-IsPdfForgeBin function from the installer."""
    old_checkout = tmp_path / "OldLocation"
    (old_checkout / "pdf_forge").mkdir(parents=True)
    (old_checkout / "pdf_forge" / "__init__.py").write_text("", encoding="utf-8")
    (old_checkout / "Run.ps1").write_text("", encoding="utf-8")
    (old_checkout / "bin").mkdir()

    unrelated = tmp_path / "SomeOtherTool"
    (unrelated / "bin").mkdir(parents=True)

    # Extract the function from the installer and exercise it in isolation.
    text = installer_text()
    match = re.search(r"function Test-IsPdfForgeBin \{.*?\n\}", text, re.S)
    assert match, "ownership helper not found"
    script = match.group(0) + f"""
$ours = Test-IsPdfForgeBin '{old_checkout / "bin"}'
$theirs = Test-IsPdfForgeBin '{unrelated / "bin"}'
$notbin = Test-IsPdfForgeBin '{old_checkout}'
Write-Output "$ours|$theirs|$notbin"
"""
    result = subprocess.run([pwsh, "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    ours, theirs, notbin = result.stdout.strip().split("|")
    assert ours == "True", "a PDF Forge bin at an old location must be recognised"
    assert theirs == "False", "an unrelated bin must be left alone"
    assert notbin == "False", "a non-bin folder must not be removed"


@requires_pwsh
def test_installer_parses():
    script = (
        "$errors=$null; "
        f"$null=[System.Management.Automation.Language.Parser]::ParseFile('{INSTALLER}',"
        "[ref]$null,[ref]$errors); "
        "if($errors){$errors|ForEach-Object{$_.Message}; exit 1}else{'ok'}"
    )
    result = subprocess.run([pwsh, "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
