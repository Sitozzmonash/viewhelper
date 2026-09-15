#Requires -Version 5.1
<#
.SYNOPSIS
    Restart the viewhelper desktop app (stop, then start).
.DESCRIPTION
    Pass -Close (or --close) to forward the flag to start.ps1, restarting into
    pure screenshot mode without audio capture or ASR model loading.
.NOTES
    stop.ps1 waits for the old process to actually exit (graceful, then force),
    so start.ps1 can never race it into two concurrent processes.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [int]$GracefulTimeoutSec = 15,
    [switch]$Close,
    [Parameter(ValueFromRemainingArguments = $true)]
    [ValidateSet('--close')]
    [string[]]$ExtraArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot

& (Join-Path $Root 'stop.ps1') -GracefulTimeoutSec $GracefulTimeoutSec
if ($LASTEXITCODE -ne 0) {
    Write-Host "RESTART FAILED (stop exit code $LASTEXITCODE)"
    exit $LASTEXITCODE
}

$closeRequested = $Close.IsPresent -or ($ExtraArgs -contains '--close')
$startScript = Join-Path $Root 'start.ps1'
if ($closeRequested) {
    & $startScript -Close
} else {
    & $startScript
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "RESTART FAILED (start exit code $LASTEXITCODE)"
    exit $LASTEXITCODE
}

Write-Host 'RESTARTED'
exit 0
