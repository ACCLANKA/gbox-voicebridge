#requires -Version 5.1
<#
.SYNOPSIS
    Run the test suite with a per-test timeout so a hung test can't wedge the shell.
.DESCRIPTION
    Uses pytest-timeout (when installed) to bound each test, so a single blocking
    test - e.g. an SSE/streaming endpoint read with a blocking client - fails the
    suite instead of hanging it forever.
.EXAMPLE
    ./scripts/test.ps1
    ./scripts/test.ps1 -Timeout 30 -PytestArgs tests/integration -x
#>
[CmdletBinding()]
param(
    [int]$Timeout = 60,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host 'Running tests...' -ForegroundColor Cyan

python -c "import pytest_timeout" 2>$null
$args = @('-m', 'pytest')
if ($LASTEXITCODE -eq 0) {
    $args += @("--timeout=$Timeout", '--timeout-method=thread')
} else {
    Write-Host 'note: pytest-timeout not installed; no per-test timeout applied' -ForegroundColor Yellow
}
if ($PytestArgs) { $args += $PytestArgs }

& python @args
exit $LASTEXITCODE
