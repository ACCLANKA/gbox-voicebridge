# Development Guide

## Prerequisites

- **Docker** & **Docker Compose** — local stack.
- **Go >= 1.22** — control/media services.
- **Node.js >= 20** — dashboard.
- **ESP-IDF v5.x** — device firmware.
- A **SIP PBX** (FreeSWITCH/Asterisk) or a test trunk for destination legs.

## Setup

```bash
git clone <repository-url>
cd gbox
cp config/.env.example config/.env
docker-compose up -d          # postgres, redis, minio, freeswitch
./scripts/bootstrap.ps1       # install deps, prepare env
```

## Common Tasks

| Task | Command |
| --- | --- |
| Bring up local stack | `docker-compose up -d` |
| Run API | `./scripts/run.ps1` |
| Run tests | `./scripts/test.ps1` |
| Lint | `golangci-lint run` / `npm run lint` |
| Format | `gofmt -w .` / `npm run format` |
| Build firmware | `idf.py build` (in `firmware/`) |
| Flash device | `idf.py -p COMx flash monitor` |

## Repositories / Modules (planned)

| Module | Language | Purpose |
| --- | --- | --- |
| `firmware/` | C (ESP-IDF) | ESP32 device firmware |
| `services/control-api` | Go | REST/WS control plane |
| `services/media-agent` | Go/C | Bridge, jitter buffer, transcoding |
| `services/billing` | Go | Usage metering, invoices |
| `web/` | Next.js | Tenant + admin dashboard |
| `simulator/` | Go/Python | Device simulator (no hardware needed) |

## Device Simulator

Use the simulator to develop bridging without hardware:

```bash
./simulator/run --device sim-01 --pop localhost:5060 --codec opus
```

It emits synthetic SRTP + control messages and can be scripted to inject
jitter/loss to test QoS handling.

## Media plane / egress

The cloud media path is **implemented in-process** under `api/`:

- device audio in via `/v1/device/ws` (`api/routers/device_ws.py`)
- destination resolution (`api/services/forwarding.py`) → an egress leg
  (`api/services/egress.py`): **SIP trunk** (UDP/TCP/TLS, REGISTER, SRTP),
  **VPS socket** (UDP/TCP), **webhook** (HTTP chunked / WebSocket), recording, simulated
- RTP/SRTP/G.711 (`api/services/rtp.py`) and SDP (`api/services/sdp.py`)

Drive a real PBX/VPS with a tone before wiring a device:

```bash
python scripts/pbx-smoke.py --type sip --uri 'sips:1000@pbx:5061' \
    --option register=1 --option password=secret --option srtp=1
python scripts/pbx-smoke.py --type socket --uri 'udp://vps:9999?mode=rtp'
```

See [../design/VOICE-FORWARDING.md](../design/VOICE-FORWARDING.md) (egress) and
[../design/DEVICE-PROTOCOL.md](../design/DEVICE-PROTOCOL.md) (device wire contract).

## Debugging

- **Media:** capture with `tcpdump`/`wireshark` on the RTP port; decode Opus.
- **Control:** structured JSON logs with correlation IDs; trace via OpenTelemetry.
- **Device:** `idf.py monitor`; enable `CONFIG_LOG_LEVEL_DEBUG`.
- **QoS:** watch the per-bridge dashboard (latency, jitter, loss, MOS).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| One-way audio | NAT / SRTP key mismatch | Check ICE/SDP, DTLS-SRTP config |
| Robotic/metallic voice | Packet loss, no PLC | Enable FEC/PLC, inspect loss % |
| Growing delay ("dragging") | Unbounded jitter buffer | Enforce max buffer depth + drop |
| Device won't pair | BT profile/role mismatch | Verify HFP-HF role, phone supports mSBC |
| Bridge drops on Wi-Fi blip | No local buffer | Increase device jitter buffer |
| High CPU on agent | Excess transcoding | Match codecs end-to-end |
