# Dashboard — Live IoT Device & Call Status

The web dashboard is the customer/admin window into the fleet. It shows, in
real time, **each device's online state** and **each call's status and details**
(incoming number, WhatsApp number, source app, duration, QoS, echo quality).

See [../api/CALL-EVENTS-API.md](../api/CALL-EVENTS-API.md) for the events that
feed it and [../api/API.md](../api/API.md) for the REST/WS endpoints.

## 1. What the Dashboard Shows

### 1.1 Device cards (IoT status)
| Field | Example | Source |
| --- | --- | --- |
| Online/offline | `online` | heartbeat / MQTT LWT |
| Bluetooth name | `Gbox-A1B2` | provisioning |
| Wi-Fi SSID + IP | `Office` / `192.168.1.42` | telemetry |
| Wi-Fi RSSI | `-58 dBm` | telemetry |
| Firmware version | `1.0.3` | telemetry |
| Last seen | `2 s ago` | heartbeat |
| Current call | `in call: WhatsApp` | call session |
| Uptime / free heap | `3d 4h` / `84 KB` | telemetry |

### 1.2 Live call card (call status & details)
| Field | Example |
| --- | --- |
| **Status** | `ringing` / `active` / `held` / `bridged` / `ended` |
| **Direction** | `incoming` / `outgoing` |
| **Source / app** | `whatsapp` / `cellular` / `sip_app` / `unknown` |
| **Incoming number (CLI)** | `+94771234567` (when available) |
| **WhatsApp number** | `+94771234567` (from enrichment) |
| **Caller name** | `Kamal Perera` (from phone contacts, if permitted) |
| **Assigned project** | `Reception` |
| **Route / destination** | `sip:reception@pbx.acme.com` |
| **Started / duration** | `21:30:00` / `00:02:18` live |
| **QoS** | MOS `4.4`, jitter `11 ms`, loss `0.1 %` |
| **Echo** | ERLE `52 dB`, double-talk `-` |
| **Recording** | `on` (link) |
| **Detection** | SCO ✓ · HFP ✓ · companion ✓ (confidence 0.95) |

> **Incoming number vs WhatsApp number:** the *incoming number* is the CLI from
> the telephony layer (Best-effort for cellular via `+CLIP`; usually absent for
> VoIP). The *WhatsApp number* comes from the **companion-app enrichment**. When
> WhatsApp is the source, the dashboard shows it on a labeled **WhatsApp** row so
> operators can tell app-calls from cellular calls.

## 2. Real-Time Channel

### 2.1 Transport
- **WebSocket** `wss://api.voicebridge.example/v1/stream` for live updates.
- Fallback: **SSE** `GET /v1/stream` for restricted environments.
- One authenticated connection per dashboard session; server pushes deltas.

### 2.2 Subscription
Client subscribes to the scopes it needs:
```json
{ "type": "subscribe",
  "topics": ["devices.*", "calls.*"],
  "projects": ["prj_1"]              // optional filter
}
```

### 2.3 Message envelope
```json
{ "topic": "calls", "event": "call.session.updated",
  "ts": "2026-10-12T21:32:00.120Z", "data": { "...": "..." } }
```

| Event | When |
| --- | --- |
| `device.online` / `device.offline` | heartbeat / LWT change |
| `device.updated` | Wi-Fi, firmware, RSSI, current-call change |
| `call.session.created` | first detection (SCO/ringing) |
| `call.session.enriched` | app + number identified |
| `call.session.answered` | call answered |
| `call.session.bridged` | media bound to a route |
| `call.session.updated` | QoS/echo/state change mid-call |
| `call.session.ended` | call finished (final CDR) |

### 2.4 Live call payload
```json
{
  "call_session_id": "cs_9f2a",
  "device_id": "dev_abc",
  "project_id": "prj_1",
  "state": "bridged",
  "direction": "incoming",
  "source": "whatsapp",
  "incoming_number": "+94771234567",
  "whatsapp_number": "+94771234567",
  "caller_name": "Kamal Perera",
  "route": { "id": "rt_5", "destination": "sip:reception@pbx.acme.com" },
  "started_at": "2026-10-12T21:30:00.120Z",
  "duration_sec": 138,
  "qos": { "mos": 4.4, "jitter_ms": 11, "loss_pct": 0.1, "oneway_ms": 128 },
  "echo": { "aec_enabled": true, "erle_db": 52, "double_talk_active": false },
  "detection": { "sco": true, "hfp": true, "companion": true, "confidence": 0.95 }
}
```

## 3. View Modes

| View | Purpose | Default refresh |
| --- | --- | --- |
| **Fleet map/grid** | all devices + online state + current call | live |
| **Live calls board** | active calls with number/source/QoS, click to detail | live |
| **Call detail** | timeline, detection sources, QoS/echo charts, recording | live |
| **CDR history** | past calls, filter by number/source/date | on demand |
| **Device detail** | telemetry history, firmware, provisioning, logs | live + history |
| **Admin** | tenants, plans, fleet health, carrier status | live |

## 4. Status Semantics

### Device status
`booting | provisioning | connecting | online | degraded | offline`
- `degraded`: online but poor RSSI / high retries / unstable jitter.
- `offline`: missed heartbeats or LWT fired.

### Call status
`detected -> enriched -> ringing -> answered -> bridged -> held -> ended`
- `detected`: SCO/HFP saw a call but no identity yet (anonymous).
- `enriched`: app + number identified (companion app).
- `ended`: final CDR written; card moves to history.

## 5. Push Notifications

The dashboard can alert on:
- incoming call on a watched device (`call.incoming`),
- call enriched with a WhatsApp/known number,
- device offline / degraded,
- low balance / failed payment,
- echo/QoS threshold breach mid-call.

Channels: in-app toast, browser push, email, webhook, (optional) WhatsApp/SMS.

## 6. Permissions

| Role | Fleet | Live calls | CDR | Recordings | Billing | Admin |
| --- | --- | --- | --- | --- | --- | --- |
| Viewer | read | read | read | no | no | no |
| Operator | read | read + control | read | play | no | no |
| Manager | read | read + control | read | play | read | no |
| Owner | read | read + control | read | manage | manage | no |
| Platform admin | all tenants | all | all | all | all | yes |

All queries are `tenant_id`-scoped; admin actions are audited.

## 7. Data Sources (backend)

```
Device --telemetry(MQTT)--> Telemetry svc --> Timeseries (per-device metrics)
Device --call events(WS)---> Call Detection svc --> CallSession (state) --> WS hub
Phone  --enrichment(HTTP)--> Call Detection svc --> CallSession (identity)
Media agent --bridge QoS---> Metrics svc --> QoS store --> WS hub
                                            |
                                            +--> Dashboard (WS) / history (REST)
```

Stores: **Redis** (live state + WS fan-out), **Postgres** (CallSession, CDR),
**timeseries** (Prometheus/Timescale) for device + QoS history, **object store**
for recordings and firmware.

## 8. Performance Targets

- Dashboard live update **p99 < 1 s** from device event to screen.
- WS fan-out to 10k concurrent dashboards per region.
- History queries **p95 < 500 ms** over 90 days.
- Graceful stream reconnect with **delta resync** (no full reload).

## 9. Example Screens (textual)

```
FLEET
┌────────────────────────┬──────────┬───────────┬─────────────────────────┐
│ Gbox-A1B2 (Reception)  │ online   │ -58 dBm   │ in call: WhatsApp 02:18 │
│ Gbox-7F3C (Warehouse)  │ online   │ -71 dBm   │ idle                    │
│ Gbox-2D91 (Gate)       │ offline  │  -        │ last seen 6m ago        │
└────────────────────────┴──────────┴───────────┴─────────────────────────┘

LIVE CALL — cs_9f2a
Status: bridged · incoming · source: WhatsApp
Number: +94771234567 (WhatsApp) · Caller: Kamal Perera
Route : Reception → sip:reception@pbx.acme.com
QoS   : MOS 4.4 · jitter 11 ms · loss 0.1% · one-way 128 ms
Echo  : AEC on · ERLE 52 dB
```
