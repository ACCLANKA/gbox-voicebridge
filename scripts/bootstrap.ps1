#requires -Version 5.1
<#
.SYNOPSIS
    Set up the local development environment.
.DESCRIPTION
    Installs dependencies and prepares configuration for local development.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host "Bootstrapping project at $repoRoot" -ForegroundColor Cyan

# 1. Copy example environment file if missing
if (-not (Test-Path 'config/.env') -and (Test-Path 'config/.env.example')) {
    Copy-Item 'config/.env.example' 'config/.env'
    Write-Host 'Created config/.env from example.' -ForegroundColor Green
}

# 2. Install dependencies (uncomment and adapt for your stack)
# npm install
# pip install -r requirements.txt

Write-Host 'Bootstrap complete.' -ForegroundColor Green
