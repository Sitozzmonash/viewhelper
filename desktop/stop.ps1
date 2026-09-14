#Requires -Version 5.1
<#
.SYNOPSIS
    Stop the viewhelper desktop app.
.NOTES
    Graceful first: writes the runtime\app.stop sentinel that the app watches,
    then waits up to $GracefulTimeoutSec seconds. If the process is still alive
    it is force-killed. The PID file is always removed at the end. Not running
    is not an error.
#>
[CmdletBinding()]
param(
    [int]$GracefulTimeoutSec = 15
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root      = $PSScriptRoot
$RuntimeDir = Join-Path $Root 'runtime'
$PidFile    = Join-Path $RuntimeDir 'app.pid'
$StopFile   = Join-Path $RuntimeDir 'app.stop'

function Clear-PidFile {
    if (Test-Path $PidFile) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue }
    if (Test-Path $StopFile) { Remove-Item $StopFile -Force -ErrorAction SilentlyContinue }
}

if (-not (Test-Path $PidFile)) {
    Write-Host 'STOPPED (app was not running)'
    exit 0
}

$procId = 0
$raw = (Get-Content $PidFile -Raw)
if ($null -eq $raw -or -not [int]::TryParse($raw.Trim(), [ref]$procId)) {
    Clear-PidFile
    Write-Host 'STOPPED (invalid PID file removed)'
    exit 0
}

$proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
if ($null -eq $proc -or $proc.ProcessName -notmatch 'python') {
    Clear-PidFile
    Write-Host 'STOPPED (stale PID file removed)'
    exit 0
}

# Ask the app to shut down gracefully (closes WebSocket, audio, SQLite, tasks).
Write-Host "Stopping PID=$procId gracefully..."
Set-Content -Path $StopFile -Value (Get-Date -Format 'o') -Encoding Ascii

$deadline = (Get-Date).AddSeconds($GracefulTimeoutSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if ($null -eq (Get-Process -Id $procId -ErrorAction SilentlyContinue)) { break }
}

if ($null -ne (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
    Write-Host "Graceful stop timed out after ${GracefulTimeoutSec}s; force killing PID=$procId (tree)."
    # /T kills the tree: uv venv python.exe is a shim that spawns the real
    # interpreter as a child, so Stop-Process alone could orphan it.
    & taskkill.exe /PID $procId /T /F 2>$null | Out-Null
    Start-Sleep -Seconds 1
}

Clear-PidFile
Write-Host 'STOPPED'
exit 0
