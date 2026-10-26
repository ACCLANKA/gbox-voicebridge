# Bring the ESP32 live end-to-end (one command). Run on the bench machine.
#
#   .\scripts\hermes-kickoff.ps1 -Repo C:\voicebridge -Bundle C:\voicebridge\voicebridge.bundle
#
# Steps: re-sync the bundle -> check the ops channel -> flash+watch+confirm online.
# Set $env:VOICEBRIDGE_KEY to an ops-scoped key first (see HERMES-COMMANDS.md).
param(
  [string]$Repo = "C:\voicebridge",
  [string]$Bundle = "C:\voicebridge\voicebridge.bundle",
  [string]$Port = "COM17",
  [string]$Serial = "GBX-BENCH01",
  [string]$Base = "http://127.0.0.1:8000",
  [string]$Key = $env:VOICEBRIDGE_KEY
)
$ErrorActionPreference = "Stop"

Write-Host "== 1. re-sync bundle =="
git -C $Repo fetch $Bundle master:refs/remotes/bundle/master
git -C $Repo merge --ff-only refs/remotes/bundle/master
git -C $Repo log --oneline -1

Write-Host "== 2. ops channel =="
try {
  Write-Host (Invoke-WebRequest -UseBasicParsing "$Base/v1/ops/ping" -TimeoutSec 10).Content
} catch {
  Write-Host "ops ping failed (deploy /v1/ops first): $_"
}

if (-not $Key) {
  Write-Host "!! VOICEBRIDGE_KEY is not set."
  Write-Host "   Mint an ops key (scopes=[ops]) - see HERMES-COMMANDS.md step 2 -"
  Write-Host "   then set `$env:VOICEBRIDGE_KEY and re-run."
}

Write-Host "== 3. bring the board live =="
python "$Repo\tools\bring_up.py" --port $Port `
  --app "$Repo\firmware\build\handoff\voicebridge-app.bin" `
  --pt "$Repo\firmware\build\handoff\voicebridge-partition-table.bin" `
  --expect-sha f91841605eabc64a68cc908f60bc56913bdca7bd823c5e5c08f1ed50d7a56058 `
  --base $Base --key $Key --serial $Serial
