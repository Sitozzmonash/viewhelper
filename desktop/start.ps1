#Requires -Version 5.1
<#
.SYNOPSIS
    Start the viewhelper desktop app as a detached background process.
.NOTES
    The process survives closing this terminal (Start-Process gives it its own
    hidden console). PID is stored in runtime\app.pid; stdout/stderr go to
    runtime\logs\app-<timestamp>.{out,err}.log.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root      = $PSScriptRoot
$RuntimeDir = Join-Path $Root 'runtime'
$LogDir     = Join-Path $RuntimeDir 'logs'
$PidFile    = Join-Path $RuntimeDir 'app.pid'
$StopFile   = Join-Path $RuntimeDir 'app.stop'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Get-LivePid {
    param([string]$File)
    if (-not (Test-Path $File)) { return $null }
    $procId = 0
    $raw = (Get-Content $File -Raw)
    if ($null -eq $raw) { return $null }
    if (-not [int]::TryParse($raw.Trim(), [ref]$procId)) { return $null }
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($null -ne $proc -and $proc.ProcessName -match 'python') { return $procId }
    return $null
}

# Clean up a stale PID file (process gone or PID reused by a non-python process).
if ((Test-Path $PidFile) -and ($null -eq (Get-LivePid $PidFile))) {
    Remove-Item $PidFile -Force
    Write-Host 'Removed stale PID file.'
}

$existing = Get-LivePid $PidFile
if ($null -ne $existing) {
    Write-Host "ALREADY RUNNING PID=$existing"
    exit 0
}

# A leftover stop sentinel would shut the app down right after start.
if (Test-Path $StopFile) { Remove-Item $StopFile -Force }

# Prefer the project venv, fall back to system python.
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path $VenvPy) {
    $PythonExe = $VenvPy
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        Write-Host 'ERROR: python not found (no .venv and no python on PATH).'
        exit 1
    }
    $PythonExe = $cmd.Source
}

$Stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$OutLog = Join-Path $LogDir "app-$Stamp.out.log"
$ErrLog = Join-Path $LogDir "app-$Stamp.err.log"

$proc = Start-Process -FilePath $PythonExe `
    -ArgumentList '-m', 'src.main' `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru

Set-Content -Path $PidFile -Value $proc.Id -Encoding Ascii

# Fail fast if the process died immediately (bad config, missing deps...).
Start-Sleep -Seconds 2
$alive = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
if ($null -eq $alive) {
    Write-Host 'ERROR: process exited right after start. Last stderr lines:'
    if (Test-Path $ErrLog) { Get-Content $ErrLog -Tail 20 }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "STARTED PID=$($proc.Id)"
Write-Host "python : $PythonExe"
Write-Host "logs   : $OutLog"
Write-Host "         $ErrLog"
exit 0
