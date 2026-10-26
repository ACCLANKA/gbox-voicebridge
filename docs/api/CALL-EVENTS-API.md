# Call Events API (Device ↔ Server)

The API our system needs so the ESP32 (and the optional companion app) can tell
the cloud **a call is happening** and **which call it is**. See the design in
[../design/CALL-DETECTION.md](../design/CALL-DETECTION.md).

Two producers:
- **Device (ESP32)** — emits call *state* events (Layer A/B).
- **Companion app (phone)** — emits call *identity* enrichment (Layer C).

One consumer surface:
- **Tenant apps** — receive identified call events via WebSocket + webhooks.

## 1. Device Transport

| Channel | Protocol | Use |
| --- | --- | --- |
| Primary (low latency, bidirectional) | **WebSocket / MQTT over TLS** | Call events + control (answer/hangup) |
| Fallback (restricted networks) | HTTPS POST | Batched events |
| Media | SRTP/Opus (separate, see AUDIO-PIPELINE) | Audio |

Authentication: **mTLS** with the device certificate (issued at provisioning).
Endpoint: `wss://api.voicebridge.example/v1/device/events`

## 2. Device → Server: Call Events

### Event envelope
```json
{
  "type": "call.event",
  "device_id": "dev_abc",
  "seq": 1042,
  "ts": "2026-10-12T21:30:00.120Z",
  "event": "ringing",
  "data": { "...event-specific..." }
}
```

### Event types

| `event` | Trigger (device) | Meaning |
| --- | --- | --- |
| `call.ringing` | HFP `callsetup=1` (if emitted) | Incoming call ringing |
| `call.audio_link_up` | **SCO/eSCO established** | Phone is routing call audio (reliable) |
| `call.answered` / `call.active` | HFP `call=1` | Call is active |
| `call.audio_link_down` | SCO/eSCO closed | Call audio ended |
| `call.ended` | HFP `call=0` | Call finished |
| `call.held` / `call.resumed` | HFP `callheld` | Hold state |
| `call.direction` | `callsetup` 1 vs 2/3 | incoming / outgoing |

### Examples

`call.audio_link_up` (the reliable backbone signal):
```json
{ "type":"call.event","device_id":"dev_abc","seq":1042,
  "ts":"2026-10-12T21:30:00.120Z","event":"call.audio_link_up",
  "data": { "sco": true, "codec": "msbc", "sample_rate": 16000,
            "ag_bd_addr": "AA:BB:CC:11:22:33" } }
```

`call.ringing` with best-effort caller ID:
```json
{ "type":"call.event","device_id":"dev_abc","seq":1040,
  "ts":"2026-10-12T21:29:58.500Z","event":"call.ringing",
  "data": { "direction":"incoming", "clip": "+94771234567", "clip_type":"national" } }
```

`call.ended`:
```json
{ "type":"call.event","device_id":"dev_abc","seq":1090,
  "ts":"2026-10-12T21:33:42.900Z","event":"call.ended",
  "data": { "duration_ms": 222780, "reason":"remote_hangup" } }
```

The server replies to each event:
```json
{ "ack": true, "seq": 1042, "call_session_id": "cs_9f2a",
  "action": "none" }        // action may be "answer", "hangup", "none"
```

### Server → Device: Call Control
```json
{ "type":"call.command","device_id":"dev_abc","session_id":"cs_9f2a",
  "command":"answer", "args":{} }        // answer | hangup | hold | resume | reject
```
The device executes the corresponding **AT** command (`ATA`, `AT+CHUP`, etc.).

## 3. Companion App → Server: Enrichment (Layer C)

Endpoint: `POST /v1/call/enrich` (mTLS or user token; phone bound to device)

```json
{
  "device_id": "dev_abc",
  "source_app": "com.whatsapp",
  "direction": "incoming",
  "caller": { "name": "Kamal Perera", "number": "+94771234567", "avatar_url": null },
  "ts": "2026-10-12T21:30:00.050Z",
  "confidence": 0.95
}
```
Response:
```json
{ "call_session_id": "cs_9f2a", "matched": true, "source": "whatsapp" }
```
`source` is normalized server-side: `whatsapp | cellular | sip_app | telegram | unknown`.

If no session matches within the correlation window, the server creates a
**phone-only** session (phone saw a call the device didn't report) and flags it.

## 4. Server → Tenant: Live Identified Events (WebSocket)

Tenant dashboard connects to `wss://api.voicebridge.example/v1/calls/stream`
and receives **identified** sessions:

```json
{
  "type": "call.session.updated",
  "call_session_id": "cs_9f2a",
  "device_id": "dev_abc",
  "project_id": "prj_1",
  "state": "enriched",
  "source": "whatsapp",
  "direction": "incoming",
  "caller": { "name": "Kamal Perera", "number": "+94771234567" },
  "detection": { "sco": true, "hfp": true, "companion_app": true,
                 "confidence": 0.95, "identify_ms": 120 },
  "started_at": "2026-10-12T21:30:00.120Z",
  "qos": { "jitter_ms": 11, "loss_pct": 0.1, "mos": 4.4 }
}
```

Events emitted: `call.session.created`, `call.session.enriched`,
`call.session.answered`, `call.session.bridged`, `call.session.ended`.

## 5. Server → Tenant: Webhooks

Events: `call.incoming`, `call.enriched`, `call.answered`, `call.ended`,
`call.failed`. Signed with **HMAC-SHA256** (`X-VB-Signature`), retried with backoff.

`call.enriched` example:
```json
{
  "event": "call.enriched",
  "call_session_id": "cs_9f2a",
  "device_id": "dev_abc",
  "source": "whatsapp",
  "caller": { "name": "Kamal Perera", "number": "+94771234567" },
  "direction": "incoming",
  "ts": "2026-10-12T21:30:00.120Z"
}
```

## 6. REST: Query Call Sessions

| Method | Path | Description |
| --- | --- | --- |
| GET | `/v1/calls` | List sessions (filters: device, project, source, state, time range) |
| GET | `/v1/calls/{id}` | Session detail (detection sources, QoS, route) |
| POST | `/v1/calls/{id}/answer` | Answer via device (Layer B control) |
| POST | `/v1/calls/{id}/hangup` | Hang up |
| POST | `/v1/calls/{id}/route` | Apply/override route |
| GET | `/v1/calls/{id}/recording` | Recording URL (if any) |

### `GET /v1/calls` example
```json
{
  "data": [
    { "id":"cs_9f2a","device_id":"dev_abc","project_id":"prj_1",
      "source":"whatsapp","direction":"incoming",
      "caller":{"name":"Kamal Perera","number":"+94771234567"},
      "state":"ended","started_at":"2026-10-12T21:30:00Z",
      "duration_sec":222,"detection_confidence":0.95,"mos":4.4 }
  ],
  "next_cursor": null
}
```

## 7. Data Model Additions

```
call_sessions
  id (pk), tenant_id, device_id, project_id, route_id, bridge_id,
  state (detected|enriched|answered|bridged|ended|failed),
  source (whatsapp|cellular|sip_app|telegram|unknown),
  direction (incoming|outgoing),
  caller_name, caller_number,
  detection_sco (bool), detection_hfp (bool), detection_companion (bool),
  detection_confidence (numeric), identify_ms (int),
  started_at, answered_at, ended_at, duration_sec, created_at

call_events   (raw append-only log)
  id, device_id, tenant_id, call_session_id, event, data (jsonb), device_ts, server_ts

enrichment_events (raw append-only log)
  id, device_id, call_session_id (nullable), source_app, caller_name,
  caller_number, confidence, phone_ts, server_ts

phone_devices  (companion app registrations)
  id, tenant_id, user_id, device_id (fk devices), platform (android|ios),
  push_token, app_version, paired_at, last_seen_at
```

## 8. Detection: Normalization & Confidence

| Signal set present | `source` | `confidence` |
| --- | --- | --- |
| Companion app (WhatsApp) + SCO | whatsapp | 0.95 |
| +CLIP number + SCO | cellular (likely) | 0.70 |
| SCO only | unknown | 0.40 |
| HFP ringing only (no SCO) | unknown | 0.30 |

`identify_ms` = time from first Layer-A/B event to successful enrichment
(a key accuracy/latency metric for the dashboard).

## 9. Security

- Device channel: **mTLS** device certificate; events are signed; replay protection via `seq` + `ts`.
- Companion channel: user auth (OAuth) + **phone↔device pairing** (a code proven at pairing time).
- Tenant channel: JWT; every call session is `tenant_id`-scoped.
- Rate limits per device and per user; anomaly detection for spoofed events.
- Privacy: enrichment carries **metadata only** (app + caller), never content; opt-in per user.
