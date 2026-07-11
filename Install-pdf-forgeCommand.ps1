<#
.SYNOPSIS
    Registers the `pdf-forge` command for the current user.

.DESCRIPTION
    Adds a `pdf-forge` function to your PowerShell profile(s) that launches
    PDF Forge via Run.ps1. After this, typing `pdf-forge` in any new PowerShell
    window runs the app from anywhere - no .cmd shim and nothing on PATH.

    * User-level only: no administrator rights, nothing system-wide.
    * Idempotent: re-running updates the function in place (safe after moving
      the project folder - it re-points to the new location).
    * Reversible: delete the block between the "# BEGIN pdf-forge command" and
      "# END pdf-forge command" markers in your profile.

    Also removes the older `bin\pdf-forge.cmd` PATH entry if a previous version
    added it.

    To run: right-click this file and choose "Run with PowerShell".
#>

$ErrorActionPreference = 'Stop'

function Write-Good($m) { Write-Host $m -ForegroundColor Green }
function Write-Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host $m -ForegroundColor Red }

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $PSCommandPath }
$Launcher = Join-Path $ScriptDir 'Run.ps1'

Write-Host 'PDF Forge command installer' -ForegroundColor Cyan
Write-Host "Project: $ScriptDir"
Write-Host ''

if (-not (Test-Path -LiteralPath $Launcher)) {
    Write-Err "Launcher not found: $Launcher"
    Read-Host 'Press Enter to close'
    exit 1
}

# --- Clean up the old bin\ PATH entry from a previous installer version ----- #
$oldBin = Join-Path $ScriptDir 'bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath) {
    $parts = $userPath -split ';' | Where-Object { $_ }
    $kept = $parts | Where-Object { $_.TrimEnd('\') -ine $oldBin.TrimEnd('\') }
    if ($kept.Count -ne $parts.Count) {
        [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
        Write-Warn "Removed the old bin PATH entry: $oldBin"
    }
}

# --- Add a `pdf-forge` function to the PowerShell profile(s) ---------------- #
$documents = [Environment]::GetFolderPath('MyDocuments')
$profilePaths = @(
    (Join-Path $documents 'PowerShell\Microsoft.PowerShell_profile.ps1'),          # PowerShell 7+
    (Join-Path $documents 'WindowsPowerShell\Microsoft.PowerShell_profile.ps1')    # Windows PowerShell
)

$begin = '# BEGIN pdf-forge command'
$end = '# END pdf-forge command'
$escapedLauncher = $Launcher.Replace("'", "''")
$block = @"
$begin
function pdf-forge {
    `$launcher = '$escapedLauncher'
    `$pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if (`$pwsh) {
        & `$pwsh.Source -NoLogo -NoProfile -ExecutionPolicy Bypass -File `$launcher @args
    } else {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `$launcher @args
    }
}
$end
"@

foreach ($profilePath in $profilePaths) {
    $profileDir = Split-Path -Parent $profilePath
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null

    $content = ''
    if (Test-Path -LiteralPath $profilePath) {
        $content = Get-Content -LiteralPath $profilePath -Raw
    }

    $pattern = [regex]::Escape($begin) + '(?s).*?' + [regex]::Escape($end)
    if ($content -match $pattern) {
        $content = [regex]::Replace($content, $pattern, $block)
    } elseif ([string]::IsNullOrWhiteSpace($content)) {
        $content = $block + [Environment]::NewLine
    } else {
        $content = $content.TrimEnd() + [Environment]::NewLine * 2 + $block + [Environment]::NewLine
    }

    Set-Content -LiteralPath $profilePath -Value $content -Encoding UTF8
    Write-Good "Updated PowerShell profile: $profilePath"
}

Write-Host ''
Write-Good 'Installed. Open a NEW PowerShell window and run:  pdf-forge'
Read-Host 'Press Enter to close'
