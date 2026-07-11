<#
.SYNOPSIS
    Installs the `pdf-forge` command into the user PATH.

.DESCRIPTION
    Adds this project's `bin` folder (which contains the `pdf-forge` command
    shim) to the current user's PATH, so typing `pdf-forge` in any new
    terminal - PowerShell or cmd - launches PDF Forge via Run.ps1.

    * User-level only: no administrator rights required, nothing system-wide.
    * Idempotent: running it again reports "already installed" and changes
      nothing.
    * Reversible: remove the single PATH entry it added (the script prints the
      exact entry), or re-run after moving the project folder to register the
      new location.

    To run: right-click this file and choose "Run with PowerShell", or execute
    it from a PowerShell prompt.
#>

$ErrorActionPreference = 'Stop'

function Write-Good($msg) { Write-Host $msg -ForegroundColor Green }
function Write-Warn($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host $msg -ForegroundColor Red }

function Wait-AndExit([int]$code) {
    Write-Host ''
    Read-Host 'Press Enter to close'
    exit $code
}

# --- Resolve paths relative to this script (works from any CWD) ----------- #
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BinDir    = Join-Path $ScriptDir 'bin'
$Shim      = Join-Path $BinDir 'pdf-forge.cmd'
$Launcher  = Join-Path $ScriptDir 'Run.ps1'

Write-Host 'PDF Forge command installer' -ForegroundColor Cyan
Write-Host "Project: $ScriptDir"
Write-Host ''

if (-not (Test-Path -LiteralPath $Shim)) {
    Write-Err "Command shim not found: $Shim"
    Write-Err 'The project appears incomplete. Re-clone or restore bin\pdf-forge.cmd.'
    Wait-AndExit 1
}
if (-not (Test-Path -LiteralPath $Launcher)) {
    Write-Err "Launcher not found: $Launcher"
    Wait-AndExit 1
}

# --- Read the current user PATH (never touches the machine PATH) ---------- #
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($null -eq $userPath) { $userPath = '' }
$entries = $userPath -split ';' | Where-Object { $_ -ne '' }

# Already installed at this exact location?
if ($entries | Where-Object { $_.TrimEnd('\') -ieq $BinDir.TrimEnd('\') }) {
    Write-Good "Already installed: '$BinDir' is on your user PATH."
    Write-Good 'Type pdf-forge in any terminal to launch PDF Forge.'
    Wait-AndExit 0
}

# A stale entry from a previous project location? Offer to replace it.
$stale = $entries | Where-Object {
    $_ -match '\\bin\\?$' -and (Test-Path -LiteralPath (Join-Path $_ 'pdf-forge.cmd') ) -eq $false -and $_ -like '*PDF Forge*'
}

Write-Host "This will add the following folder to your user PATH:"
Write-Host "  $BinDir" -ForegroundColor Cyan
$answer = Read-Host 'Continue? [Y/n]'
if ($answer -and $answer.Trim().ToLower() -notin @('y', 'yes')) {
    Write-Warn 'Cancelled. Nothing was changed.'
    Wait-AndExit 0
}

try {
    $newEntries = @($entries | Where-Object { $_ -notin $stale }) + $BinDir
    $newPath = ($newEntries -join ';')
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    if ($stale) {
        Write-Warn "Removed stale entry: $($stale -join ', ')"
    }
} catch {
    Write-Err "Failed to update the user PATH: $($_.Exception.Message)"
    Wait-AndExit 1
}

# Make it available in this session too.
$env:Path = "$env:Path;$BinDir"

# Notify running applications that the environment changed, so newly opened
# terminals pick up the PATH without a logoff (best effort).
try {
    Add-Type -Namespace Win32 -Name NativeMethods -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
    $result = [UIntPtr]::Zero
    [void][Win32.NativeMethods]::SendMessageTimeout(
        [IntPtr]0xFFFF, 0x001A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result
    )
} catch {
    # Non-fatal: new terminals will still see the PATH after a re-login.
}

Write-Host ''
Write-Good 'Installed successfully.'
Write-Good 'Open a NEW terminal and type:  pdf-forge'
Write-Host ''
Write-Host "To uninstall later, remove this entry from your user PATH:" -ForegroundColor DarkGray
Write-Host "  $BinDir" -ForegroundColor DarkGray
Wait-AndExit 0
