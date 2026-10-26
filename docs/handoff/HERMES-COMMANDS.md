# Hermes → exact commands (copy-paste)

Three things: install the **live channel**, flash **0.3.0**, then use it. Windows
PowerShell shown; bash equivalents are the same except for `$env:` vs `$`.
Paths assume the shared tree is at the repo root.

## TL;DR — one command to make the board live

```powershell
$env:VOICEBRIDGE_KEY = "vb_..."   # ops-scoped key (step 2 below)
.\scripts\hermes-kickoff.ps1 -Repo C:\voicebridge -Bundle C:\voicebridge\voicebridge.bundle
```

It re-syncs the bundle, pings the ops channel, then runs `tools/bring_up.py`:
verify sha → flash app-only → watch the boot for hello/acks → poll
`/v1/ops/devices/GBX-BENCH01` until it is **online**, and prints PASS/FAIL.
Bash equivalent: `./scripts/hermes-kickoff.sh /path/to/repo /path/to/bundle`.

## 0. Corrections to the record (so we're building on the same truth)

Your last report was two revisions stale. The bundle tip you verified (`b4d2f2c`,
fw `0.2.0`, app sha `d8ddd0df…`) has since moved:

| | you verified | current |
| --- | --- | --- |
| bundle tip | `b4d2f2c` | **`2caf06c`** (this commit adds the channel) |
| firmware | `0.2.0` | **`0.3.2`** |
| app sha256 | `d8ddd0df…` | **`9e5eb1a2259bd7ce290d513cddfb28e5039a2ab5a2d59b48621b718f54280f7a`** |

Two attribution/scope fixes: the **WebSocket-library** bug was yours (thank you);
the **status lifecycle** was implemented here off your report. And the earlier
"cloud-side audio egress leg" as the follow-on is wrong — with no mic the
**ingress** (Bluetooth) has to exist first. Audio is parked; see the round-3 brief.

## 1. Install the live channel (one more manual sync, then it's live)

```powershell
git -C C:\path\to\bundle fetch C:\path\to\voicebridge.bundle master:refs/remotes/bundle/master
git -C C:\path\to\bundle merge refs/remotes/bundle/master
```

Then deploy that tree over `/opt/voicebridge` **as before** (keep `data/`) and
recreate the compose project. This adds `/v1/ops/*` (see `api/routers/ops.py`).

Prove it is up (no auth needed):

```powershell
curl.exe -s http://127.0.0.1:8000/v1/ops/ping
# {"service":"voicebridge-ops","ok":true,"time":"..."}
```

## 2. Mint an ops key and hand it to Nuwan

```powershell
# log in as the tenant owner, then create a scoped key
$tok = (curl.exe -s -X POST http://127.0.0.1:8000/v1/auth/login `
  -H "Content-Type: application/json" `
  -d '{\"email\":\"owner@acme.example\",\"password\":\"password123\"}' | ConvertFrom-Json).access_token
curl.exe -s -X POST http://127.0.0.1:8000/v1/api-keys `
  -H "Authorization: Bearer $tok" -H "Content-Type: application/json" `
  -d '{\"name\":\"builder-bridge\",\"scopes\":[\"ops\"]}'
```

Give the `api_key` to Nuwan (he relays it here). It is scoped to `ops` and
revocable — do **not** paste it into the repo or a log.

## 3. Flash fw 0.3.0 (app-only; NVS kept) — optional

```powershell
python tools\bench.py verify firmware\build\handoff\voicebridge-app.bin
python tools\bench.py flash --port COM17 --app firmware\build\handoff\voicebridge-app.bin `
  --expect-sha 9e5eb1a2259bd7ce290d513cddfb28e5039a2ab5a2d59b48621b718f54280f7a
```

`--expect-sha` refuses to flash on a hash mismatch. The app goes to `0x10000`;
NVS at `0x9000` is untouched. Not required — no mic, nothing on the pass gate.

## 4. Use the channel

```powershell
$env:VOICEBRIDGE_URL = "http://127.0.0.1:8000"
$env:VOICEBRIDGE_KEY = "vb_..."      # the ops key from step 2

python scripts\bridge.py ping
python scripts\bridge.py tasks --status pending
python scripts\bridge.py claim <task_id>
python scripts\bridge.py ack <task_id> --status done -m "flash verified; distinct=1 (no mic)"
```

`bridge.py` is stdlib-only, so it runs from any python on the bench.

## 5. Sweep a pinout yourself (no round-trip to me)

```powershell
python tools\bench.py sweep --port COM17 `
  --combos 26/25/22,14/15/32,33/32/25 `
  --ssid Oshadha --host vb.site.gigies.lk --ingest-port 443 --tls `
  --serial GBX-BENCH01 --key vb_...
```

For each `bclk/ws/din` it regenerates NVS, reflashes only `0x9000`, reboots and
prints `distinct`/`rms` — `distinct=1` means nothing is on those pins. Needs
`pip install pyserial` for the serial read.

## Driving a live device from the cloud (tone / answer / downlink)

`/v1/ops` can now **write** to a connected device (the missing direction — see
`api/routers/ops.py`). Same `ops`-scoped key as the rest of `/v1/ops`:

```powershell
# 1 kHz, 2 s into the phone earpiece (proves device->phone; needs an answered call)
python scripts\bridge.py control --serial GBX-BENCH01 --action tone --hz 1000 --ms 2000

# accept an incoming call
python scripts\bridge.py control --serial GBX-BENCH01 --action answer

# push one downlink PCM16@16k frame (payload = [0x01] + PCM, added by the server)
python scripts\bridge.py audio --serial GBX-BENCH01 --pcm-file tone.pcm
```

Raw HTTP equivalents:

```
POST /v1/ops/devices/{serial}/control   {"action":"tone","hz":1000,"ms":2000}
POST /v1/ops/devices/{serial}/audio     {"pcm_base64":"...","kind":1}
```

`404` = no such serial for this tenant; `409` = device known but **not connected**
(no live socket) — check `/v1/ops/devices/{serial}` first.
