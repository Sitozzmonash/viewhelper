#Requires -Version 5.1
<#
.SYNOPSIS
    Restart the viewhelper desktop app (stop, then start).
.NOTES
    stop.ps1 waits for the old process to actually exit (graceful, then force),
    so start.ps1 can never race it into two concurrent processes.
#>
[CmdletBinding()]
param(
    [int]$GracefulTimeoutSec = 15
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot

& (Join-Path $Root 'stop.ps1') -GracefulTimeoutSec $GracefulTimeoutSec
if ($LASTEXITCODE -ne 0) {
    Write-Host "RESTART FAILED (stop exit code $LASTEXITCODE)"
    exit $LASTEXITCODE
}

& (Join-Path $Root 'start.ps1')
if ($LASTEXITCODE -ne 0) {
    Write-Host "RESTART FAILED (start exit code $LASTEXITCODE)"
    exit $LASTEXITCODE
}

Write-Host 'RESTARTED'
exit 0
