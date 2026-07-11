@echo off
rem PDF Forge command shim. Added to the user PATH by Install-pdf-forgeCommand.ps1
rem so that typing `pdf-forge` in any terminal (cmd or PowerShell) launches the
rem app. Resolves the project from its own location, so the folder can move -
rem just re-run the installer after moving.
where pwsh >nul 2>&1
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\Run.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\Run.ps1" %*
)
exit /b %errorlevel%
