#Requires -Version 5.1
<#
.SYNOPSIS
    Vendor backend/app into frontend/api/app for single-project Vercel deploy.
.DESCRIPTION
    Vercel only uploads files under the project Root Directory (frontend/), so
    the FastAPI relay package must be copied next to frontend/api/ws.py before
    deploying. Run this after any change to backend/ and before `vercel deploy`
    (or `vercel --prod`). The copy is gitignored; backend/ stays the source of
    truth.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Source   = Join-Path $RepoRoot 'backend\app'
$Target   = Join-Path $RepoRoot 'frontend\api\app'

if (-not (Test-Path $Source)) {
    Write-Host "ERROR: $Source not found."
    exit 1
}

if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
Copy-Item $Source $Target -Recurse

# __pycache__ must not ship.
Get-ChildItem $Target -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

$files = (Get-ChildItem $Target -Recurse -File).Count
Write-Host "Synced $files files: backend\app -> frontend\api\app"
exit 0
