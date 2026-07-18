<#
    PDF Forge - PowerShell launcher.

    Responsibilities:
      * Resolve its own directory and run the pdf_forge package from there.
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
$MainPackage = Join-Path $ScriptDir 'pdf_forge'
$Requirements = Join-Path $ScriptDir 'requirements.txt'
$VenvDir     = Join-Path $ScriptDir '.venv'
$VenvPython  = Join-Path $VenvDir 'Scripts\python.exe'
$StampFile   = Join-Path $VenvDir '.deps_installed'

Write-Good 'PDF Forge launcher starting...'

if (-not (Test-Path -LiteralPath (Join-Path $MainPackage '__init__.py'))) {
    Fail "Main application package not found: $MainPackage"
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
function Test-VenvHealthy {
    # PF-034: an existing .venv is not trusted just because python.exe exists.
    # It may have been built by a Python that has since been upgraded or
    # removed, leaving an interpreter that cannot start or import its own
    # standard library. Verify it actually runs, is a supported version, has the
    # expected architecture, and can import the bootstrap modules.
    param([string] $PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath)) { return $false }
    $probe = @'
import struct, sys
ok = sys.version_info >= (3, 10)
print("%d.%d %d %s" % (sys.version_info[0], sys.version_info[1],
                       struct.calcsize("P") * 8, "ok" if ok else "old"))
'@
    try {
        $output = & $PythonPath -c $probe 2>$null
    } catch {
        return $false
    }
    if ($LASTEXITCODE -ne 0 -or -not $output) { return $false }
    $parts = ($output | Select-Object -First 1).Trim().Split(' ')
    if ($parts.Count -lt 3) { return $false }
    if ($parts[2] -ne 'ok') {
        Write-Warn "The virtual environment uses Python $($parts[0]); 3.10+ is required."
        return $false
    }
    return $true
}

function Get-DependencyHash {
    # PF-033: freshness was decided by requirements.txt mtime alone, so editing
    # the file while preserving its timestamp (or restoring an older copy) left
    # a stale environment installed. Hash the *contents* of every dependency
    # input instead, and include the interpreter identity so a Python upgrade
    # forces a reinstall.
    param([string[]] $Files, [string] $PythonPath)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $buffer = New-Object System.Text.StringBuilder
    foreach ($file in ($Files | Sort-Object)) {
        if (Test-Path -LiteralPath $file) {
            [void]$buffer.Append((Get-Content -LiteralPath $file -Raw -ErrorAction SilentlyContinue))
        }
    }
    try {
        $ident = & $PythonPath -c "import sys,struct;print(sys.version, struct.calcsize('P')*8)" 2>$null
        [void]$buffer.Append($ident)
    } catch { }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($buffer.ToString())
    return [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '')
}

function Test-DependenciesImportable {
    param([string] $PythonPath)
    & $PythonPath -c "import pymupdf, PIL" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

$recreate = $false
if (Test-Path -LiteralPath $VenvPython) {
    if (-not (Test-VenvHealthy $VenvPython)) {
        Write-Warn 'The existing virtual environment is unusable; recreating it.'
        $recreate = $true
    }
} else {
    $recreate = $true
}

if ($recreate) {
    $base = Get-BasePython
    if (-not $base) {
        Write-Err 'Python 3.10 or newer is required but was not found.'
        Write-Err 'Install Python from https://www.python.org/downloads/ and try again.'
        Pause-Before-Exit
        exit 1
    }
    if (Test-Path -LiteralPath $VenvDir) {
        Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Info 'Creating local virtual environment (.venv)...'
    $venvArgs = $base.Args + @('-m', 'venv', $VenvDir)
    & $base.Exe @venvArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-VenvHealthy $VenvPython)) {
        Fail 'Failed to create a working virtual environment.'
    }
}

# Install dependencies only when the resolved inputs actually changed.
$depFiles = @($Requirements)
$expected = Get-DependencyHash -Files $depFiles -PythonPath $VenvPython
$needInstall = $true
if (Test-Path -LiteralPath $StampFile) {
    $recorded = (Get-Content -LiteralPath $StampFile -Raw -ErrorAction SilentlyContinue)
    if ($recorded -and $recorded.Trim() -eq $expected) { $needInstall = $false }
}
if (-not $needInstall -and -not (Test-DependenciesImportable $VenvPython)) {
    Write-Warn 'Dependencies are recorded as installed but cannot be imported; repairing.'
    $needInstall = $true
}

if ($needInstall -and (Test-Path -LiteralPath $Requirements)) {
    Write-Info 'Installing dependencies (first run, updated requirements, or repair)...'
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -r $Requirements --quiet
    if ($LASTEXITCODE -ne 0) {
        Fail 'Dependency installation failed. Check your network connection and try again.'
    }
    if (-not (Test-DependenciesImportable $VenvPython)) {
        Fail 'Dependencies installed but still cannot be imported.'
    }
    # Record the exact inputs this environment satisfies (written atomically).
    $tempStamp = "$StampFile.tmp"
    Set-Content -LiteralPath $tempStamp -Value $expected -Encoding UTF8 -NoNewline
    Move-Item -LiteralPath $tempStamp -Destination $StampFile -Force
    Write-Good 'Dependencies installed.'
}

# --- Run the application ------------------------------------------------- #
Write-Good 'Starting PDF Forge...'
Write-Host ''

# Run as a module from the script directory so `import pdf_forge` resolves
# regardless of the caller's working directory.
Push-Location -LiteralPath $ScriptDir
try {
    if ($AppArgs -and $AppArgs.Count -gt 0) {
        & $VenvPython -m pdf_forge @AppArgs
    } else {
        & $VenvPython -m pdf_forge
    }
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

Write-Host ''
if ($exitCode -ne 0) {
    Write-Warn "PDF Forge exited with code $exitCode."
    Pause-Before-Exit
} else {
    Write-Good 'PDF Forge finished.'
}

exit $exitCode
