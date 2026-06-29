<#
    PDF Forge - PowerShell launcher.

    Responsibilities:
      * Resolve its own directory and run pdf_forge.py from there.
      * Verify Python 3.10+ is available (prefers 'py', then 'python').
      * Create a local .venv on first run and install requirements once.
      * Use UTF-8 so Unicode / Persian output and paths work correctly.
      * Forward command-line arguments and return the app's real exit code.
      * Keep the window open on fatal launcher errors.

    This launcher does not require administrator privileges.
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $AppArgs
)

$ErrorActionPreference = 'Stop'

# --- Console setup ------------------------------------------------------- #
$Host.UI.RawUI.WindowTitle = 'PDF Forge'
try {
    # Ensure UTF-8 so Unicode output renders correctly.
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # Non-fatal: continue with the default encoding.
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Write-Info    ($msg) { Write-Host $msg -ForegroundColor Cyan }
function Write-Good    ($msg) { Write-Host $msg -ForegroundColor Green }
function Write-Warn    ($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Err     ($msg) { Write-Host $msg -ForegroundColor Red }

function Pause-Before-Exit {
    Write-Host ''
    Write-Host 'Press Enter to close...' -ForegroundColor Yellow
    [void](Read-Host)
}

function Fail ($msg) {
    Write-Err "PDF Forge: $msg"
    Pause-Before-Exit
    exit 1
}

# --- Resolve paths relative to this script ------------------------------- #
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$MainScript  = Join-Path $ScriptDir 'pdf_forge.py'
$Requirements = Join-Path $ScriptDir 'requirements.txt'
$VenvDir     = Join-Path $ScriptDir '.venv'
$VenvPython  = Join-Path $VenvDir 'Scripts\python.exe'
$StampFile   = Join-Path $VenvDir '.deps_installed'

Write-Good 'PDF Forge launcher starting...'

if (-not (Test-Path -LiteralPath $MainScript)) {
    Fail "Main application file not found: $MainScript"
}

# --- Locate a suitable base Python (3.10+) ------------------------------- #
function Get-BasePython {
    # Returns an array describing how to invoke a Python >= 3.10, or $null.
    $candidates = @(
        @{ Exe = 'py';     Args = @('-3') },
        @{ Exe = 'python'; Args = @() },
        @{ Exe = 'python3'; Args = @() }
    )
    foreach ($c in $candidates) {
        $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $verArgs = $c.Args + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')
            $ver = & $c.Exe @verArgs 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }
            $parts = $ver.Trim().Split('.')
            $major = [int]$parts[0]; $minor = [int]$parts[1]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
                return $c
            }
        } catch {
            continue
        }
    }
    return $null
}

# --- Ensure the virtual environment and dependencies --------------------- #
if (-not (Test-Path -LiteralPath $VenvPython)) {
    $base = Get-BasePython
    if (-not $base) {
        Write-Err 'Python 3.10 or newer is required but was not found.'
        Write-Err 'Install Python from https://www.python.org/downloads/ and try again.'
        Pause-Before-Exit
        exit 1
    }
    Write-Info 'Creating local virtual environment (.venv)...'
    $venvArgs = $base.Args + @('-m', 'venv', $VenvDir)
    & $base.Exe @venvArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Fail 'Failed to create the virtual environment.'
    }
}

# Install dependencies only when needed (avoid reinstalling on every run).
$needInstall = $false
if (-not (Test-Path -LiteralPath $StampFile)) {
    $needInstall = $true
} elseif ((Test-Path -LiteralPath $Requirements) -and
          ((Get-Item $Requirements).LastWriteTimeUtc -gt (Get-Item $StampFile).LastWriteTimeUtc)) {
    # requirements.txt changed after the last successful install.
    $needInstall = $true
}

if ($needInstall -and (Test-Path -LiteralPath $Requirements)) {
    Write-Info 'Installing dependencies (first run or updated requirements)...'
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -r $Requirements --quiet
    if ($LASTEXITCODE -ne 0) {
        Fail 'Dependency installation failed. Check your network connection and try again.'
    }
    # Record successful installation.
    Set-Content -LiteralPath $StampFile -Value (Get-Date).ToUniversalTime().ToString('o') -Encoding UTF8
    Write-Good 'Dependencies installed.'
}

# --- Run the application ------------------------------------------------- #
Write-Good 'Starting PDF Forge...'
Write-Host ''

if ($AppArgs -and $AppArgs.Count -gt 0) {
    & $VenvPython $MainScript @AppArgs
} else {
    & $VenvPython $MainScript
}
$exitCode = $LASTEXITCODE

Write-Host ''
if ($exitCode -ne 0) {
    Write-Warn "PDF Forge exited with code $exitCode."
    Pause-Before-Exit
} else {
    Write-Good 'PDF Forge finished.'
}

exit $exitCode
