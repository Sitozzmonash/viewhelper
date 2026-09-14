#Requires -Version 5.1
<#
.SYNOPSIS
    Report whether the viewhelper desktop app is running.
.OUTPUTS
    RUNNING PID=xxxx  or  STOPPED
.NOTES
    If the PID file exists but the process does not (or the PID was reused by a
    non-python process), the stale PID file is removed automatically.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root      = $PSScriptRoot
$RuntimeDir = Join-Path $Root 'runtime'
$PidFile    = Join-Path $RuntimeDir 'app.pid'
$StopFile   = Join-Path $RuntimeDir 'app.stop'

if (-not (Test-Path $PidFile)) {
    Write-Host 'STOPPED'
    exit 0
}

$procId = 0
$raw = (Get-Content $PidFile -Raw)
if ($null -eq $raw -or -not [int]::TryParse($raw.Trim(), [ref]$procId)) {
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host 'STOPPED'
    exit 0
}

$proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
if ($null -ne $proc -and $proc.ProcessName -match 'python') {
    Write-Host "RUNNING PID=$procId"
    exit 0
}

# PID file present but the process is gone (or PID reused) -> clean up.
Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
if (Test-Path $StopFile) { Remove-Item $StopFile -Force -ErrorAction SilentlyContinue }
Write-Host 'STOPPED'
exit 0
