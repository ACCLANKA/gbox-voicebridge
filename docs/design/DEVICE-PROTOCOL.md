# Device ⇄ Cloud Protocol (v0)

> Status: **implemented (Phase 1 slice)**. The ingest edge, the codec and the
> media-agent seam live in `api/routers/device_ws.py`, `api/protocol.py` and
> `api/services/media.py`. This document is the contract the ESP32 firmware
> (`firmware/`) and the cloud must both satisfy.

Related: [DEVICE-FIRMWARE.md](DEVICE-FIRMWARE.md) (BT/task layout),
[AUDIO-PIPELINE.md](AUDIO-PIPELINE.md) (codecs/plc), [ROADMAP.md](ROADMAP.md) (Phase 1).

## 1. Transport & endpoint

- WebSocket, `ws(s)://<host>/v1/device/ws?serial=<SERIAL>&token=<API_KEY>`.
- `token` is a **tenant API key** (`vb_…`). The serial must belong to that
  tenant, otherwise the server closes the socket with code **4401** *before*
  accepting (application-level "unauthorized").
- Two frame types share the socket:
  - **text** frames → JSON control messages (§3),
  - **binary** frames → audio (§4).

## 2. Handshake

The device must send `hello` first; the server records `last_seen` and replies
`ack`. There is no session cookie — auth is per-connection.

```
device → {"type":"hello","fw":"1.2.3","serial":"GBX-A1B2C3"}
server → {"type":"ack","seq":null,"call_session_id":null,"action":"hello"}
```

## 3. Control messages (JSON, text frames)

### 3.1 Device → cloud

| `type` | Fields | Meaning |
| --- | --- | --- |
| `hello` | `fw`, `serial` | Handshake / firmware version. |
| `event` | `event`, `seq`, `data`, `ts?` | Call lifecycle event (table below). |
| `telemetry` | `rssi`, `free_heap_kb?`, `uptime_s?`, `mos?` | Periodic metrics. |
| `ping` | — | Keepalive / latency probe. |

`event` values and their effect on the call session:

| `event` | Effect |
| --- | --- |
| `call.ringing` | create session, `state=ringing`, HFP detected, optional `data.clip` caller id |
| `call.audio_link_up` | SCO link detected (`detection_sco=1`) |
| `call.answered` / `call.active` | `state=answered` → **media leg opens** |
| `call.held` | `state=held` (leg stays open) |
| `call.resumed` | `state=bridged` (if bridged) else `answered` |
| `call.ended` | `state=ended`, duration from `data.duration_ms` → **media leg closes**, CDR written |

### 3.2 Cloud → device

| `type` | Fields | Meaning |
| --- | --- | --- |
| `ack` | `seq`, `call_session_id`, `action` | Per-event acknowledgement. |
| `pong` | — | Reply to `ping`. |
| `control` | `action`, … | Server-initiated call control (`answer`, `hangup`, …). |

`action` in an `ack` is one of `hello`, `telemetry`, `unsupported`, `error`, or
`none` for accepted call events.

## 4. Audio (binary frames)

One type byte, then the payload:

```
byte 0      : kind   0x01 = PCM16-LE, 0x02 = Opus
byte 1..    : payload
```

- PCM16 is **16 kHz mono, 20 ms frames** = `320` samples = `640` bytes.
- The server decodes with `api.protocol.decode_audio`, measures level
  (`level_dbfs`) and writes the payload to the session's `MediaLeg` — but only
  while a call is live (`answered`/`bridged`/`held`). Audio outside a call is
  dropped (the firmware should not send it).

## 5. Session / media-leg lifecycle

```
call.ringing ──► call.audio_link_up ──► call.answered ──► [audio flows] ──► call.ended
   (session)                        (leg OPENS)                        (leg CLOSES + CDR)
```

The leg is also closed if the socket drops mid-call. Which leg a call gets is
decided by the **forwarding plane** from the device's override or matched route —
a **SIP trunk**, a **UDP/TCP VPS socket**, a recording sink, or `simulated`; see
[VOICE-FORWARDING.md](VOICE-FORWARDING.md). None of this changes the wire
protocol above.

## 6. Errors

- Malformed JSON control frame → `ack{action:"error"}`; the socket stays open.
- Malformed binary audio frame → dropped silently (no reply).
- Unknown control `type` → `ack{action:"unsupported"}`.

## 7. Reference

- Codec + models: `api/protocol.py`
- Ingest edge: `api/routers/device_ws.py` (WebSocket `/v1/device/ws`)
- Media plane: `api/services/media.py`
- Tests: `tests/test_protocol.py`, `tests/integration/test_device_ws.py`
