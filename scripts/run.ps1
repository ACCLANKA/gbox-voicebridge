#requires -Version 5.1
<#
.SYNOPSIS
    Run the application locally.
#>
[CmdletBinding()]
param(
    [string]$Service = 'control-api'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host "Starting '$Service'..." -ForegroundColor Cyan

# Adapt to your stack:
# go run ./services/$Service
# npm --prefix web run dev
