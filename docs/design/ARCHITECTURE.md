# Voice Bridge — System Architecture

> Status: Draft v0.1 · Audience: engineering, product

## 1. Purpose

Design a multi-tenant platform where low-cost ESP32 devices bridge **Bluetooth
audio** (phone calls, softphone, any HFP source) to **IP destinations** (SIP PBX,
PSTN, WebRTC browser, webhooks), managed through a **web dashboard** with
**billing**. Audio streaming quality is the primary success metric.

## 2. High-Level View

```
        PHONE / SOFTPHONE                ESP32 DEVICE                CLOUD (multi-region)
   ┌────────────────────┐        ┌──────────────────────┐      ┌───────────────────────────┐
   │  Mobile call       │  BT    │  HFP (classic BT)    │ WiFi │  Edge Ingest (SRTP/WS)    │
   │  WhatsApp / SIP app│◄──────►│  Audio codec + I2S   │◄────►│  Media Agents (bridge)    │
   │  Softphone         │  HFP   │  Jitter buffer       │      │  Routing / Call State     │
   └────────────────────┘        │  Provisioning (BLE)  │      │  Control API + WS         │
                                 │  OTA / telemetry     │      │  Integrations (SIP/PSTN)  │
                                 └──────────────────────┘      └─────────────┬─────────────┘
                                                                             │
                                         ┌───────────────────────────────────┴───────────────┐
                                         │  Web Dashboard (tenant + admin)  ·  Billing  ·  CDR │
                                         └───────────────────────────────────────────────────┘
```

## 3. Architectural Layers

| Layer | Responsibility | Key tech |
| --- | --- | --- |
| **L0 Edge / Device** | BT HFP termination, capture/playout, local buffering, provisioning, OTA | ESP-IDF (C), FreeRTOS |
| **L1 Transport** | Reliable control channel + low-latency media channel device↔cloud | UDP/RTP(SRTP), MQTT+TLS (control), optional QUIC |
| **L2 Media Plane** | RTP termination, transcoding, mixing, recording, DTMF | FreeSWITCH / Asterisk, WebRTC SFU |
| **L3 Control Plane** | Auth, device/project/route config, call state machine, real-time events | Go/Node services, REST + WebSocket |
| **L4 Integration** | SIP trunks, PBX registration, PSTN carriers, WhatsApp, webhooks | Kamailio/FreeSWITCH, provider APIs |
| **L5 Application** | Dashboard UI, billing, CDR, analytics, notifications | React/Next.js |
| **L6 Data** | Relational, cache, object store, time-series | PostgreSQL, Redis, S3/MinIO, Prometheus |

> **Implementation status (this repo).** The layers above are the target. The
> current build realises **L1–L4 in a single Python/FastAPI process**: the device
> WebSocket ingest edge (L1), the forwarding plane + egress legs (L2), and the
> REST/BFF control surface (L3). See [DEVICE-PROTOCOL.md](DEVICE-PROTOCOL.md)
> (wire contract) and [VOICE-FORWARDING.md](VOICE-FORWARDING.md) (egress:
> SIP/UDP/TCP/TLS, VPS sockets, webhook, recording). FreeSWITCH/Kamailio/K8s
> remain the production target; each cloud component below names the module that
> implements it today, where one exists.

## 4. Component Inventory

### 4.1 Edge (device)
- **BT HFP stack** — Hands-Free (HF) role; terminates SCO/eSCO audio from the phone.
- **Audio engine** — I2S + hardware codec (or internal DAC), gain control, VAD, AEC hooks.
- **Transport client** — SRTP/Opus uplink; MQTT control; heartbeat + telemetry.
- **Provisioning** — BLE (or SoftAP captive portal) to set Wi-Fi + claim token.
- **OTA** — signed firmware update with rollback.

### 4.2 Cloud — Control Plane
- **API Gateway** — TLS, auth, rate limiting, request routing.
- **Identity service** — tenants, users, roles (RBAC), MFA, API keys.
- **Device registry** — device identity, certificates, firmware version, online state.
- **Provisioning service** — claim codes, tenant assignment, config templates.
- **Routing service** — evaluates route rules and prepares call legs.
- **Call Detection service** — ingests device call events (SCO/HFP) and companion
  enrichment; correlates them into identified `CallSession`s. See
  [CALL-DETECTION.md](CALL-DETECTION.md).
- **Call/State service** — bridge state machine, real-time events over WebSocket.
- **Billing service** — usage aggregation, plans, invoices, payment provider sync.
- **Notification service** — email/SMS/webhook/push for alerts (offline device, low balance).

### 4.3 Cloud — Media Plane
- **Ingest edge** — device audio + control over `/v1/device/ws`.
  *Implemented:* `api/routers/device_ws.py` + `api/protocol.py`.
- **Forwarding plane** — resolves a device to a destination (override → route →
  default) and owns the live leg registry. *Implemented:* `api/services/forwarding.py`.
- **Egress legs** — the adapter seam to whatever is downstream.
  *Implemented:* `api/services/egress.py` — `SipTrunkLeg` (UDP/TCP/TLS, REGISTER,
  SRTP; also powers the `pstn` destination = E.164 → carrier SBC),
  `UdpSocketLeg`/`TcpSocketLeg` (VPS), `WebhookLeg` (HTTP chunked +
  WebSocket), `RecordingLeg`, `SimulatedLeg`.
  *Codecs/crypto:* `api/services/rtp.py` (RTP/SRTP/G.711), `api/services/sdp.py`.
- **Numbering** — E.164 normalisation (`api/services/numbers.py`) and the DID
  inventory (`api/services/dids.py`, `dids` table; `/v1/dids` + dashboard
  *Numbers* tab). See [PSTN-AND-DIDS.md](PSTN-AND-DIDS.md).
- **Media agent (bridger)** — real-time relay between the two legs; jitter buffer,
  transcoding, recording. *Implemented:* the ingest edge bridges device→egress leg
  in-process today; a dedicated bridger is the scaling step.
- **Telephony engine** — FreeSWITCH/Asterisk for SIP/PBX/PSTN legs and IVR
  (production target; the native `SipTrunkLeg` is the reference path).
- **SFU (WebRTC)** — browser monitoring / softphone, WebRTC legs (*planned*).
- **Recording store** — encrypted audio objects + metadata (*local WAV today*).

### 4.4 Application (Web)
- **Tenant dashboard** — devices, projects, routes, live call monitor, recordings, usage/billing.
- **Admin console** — tenants, plans, fleet health, feature flags, audit, support tools.

### 4.5 Data & Observability
- **PostgreSQL** — transactional core.
- **Redis** — sessions, presence, pub/sub, rate limits.
- **S3/MinIO** — recordings, firmware, exports.
- **Prometheus/Grafana/Loki/Tempo** — metrics, logs, traces; per-bridge QoS dashboards.

## 5. Call Flows

### 5.1 Inbound phone call → customer PBX
```
Phone --BT HFP--> ESP32 --SRTP/Opus--> Ingest Edge --> Media Agent
     --> Routing (select PBX route) --> FreeSWITCH --> SIP Trunk --> Customer PBX
```
> **Reference path in this repo:** `ESP32 --WS--> /v1/device/ws --> forwarding.resolve
> --> SipTrunkLeg (INVITE/RTP/SRTP) --> PBX`, or a VPS socket / webhook leg. See
> [VOICE-FORWARDING.md](VOICE-FORWARDING.md).

### 5.2 Phone call → PSTN destination
```
Phone --BT HFP--> ESP32 --SRTP--> Media Agent --> PSTN carrier (SIP) --> callee
```

### 5.3 Browser monitoring / softphone (WebRTC)
```
Phone --BT--> ESP32 --SRTP--> Media Agent --(SFU)--> Browser (Opus/WebRTC)
```

### 5.4 WhatsApp voice
```
WhatsApp --(Cloud API / partner)--> Integration Adapter --> Media Agent --> route target
```
> Real-time WhatsApp *voice calling* is not broadly exposed as a public API.
> See [ROUTING-INTEGRATIONS.md](ROUTING-INTEGRATIONS.md#whatsapp) — this leg is
> partner/approval dependent and modeled behind an adapter so it can be swapped.

### 5.5 Bridge state machine
```
IDLE -> DEVICE_ONLINE -> BT_PAIRED -> CALL_RINGING -> BRIDGING -> TEARING_DOWN -> IDLE
                                   \-> FAILED / RECONNECTING
```
Transitions emit events consumed by the dashboard and CDR/billing pipeline.

## 6. Design Principles

1. **Media path is sacred.** Control-plane hiccups must never drop audio; media and control are separate substrates.
2. **Select nearest media PoP.** Keep device→cloud RTT low; load-balance at L4 and by SIP-aware L7.
3. **Codec-adaptive.** Prefer wideband (Opus/mSBC) but degrade gracefully; never renegotiate mid-word.
4. **Everything observable.** Every bridge is scored (jitter, loss, RTT, MOS) in real time.
5. **Multi-tenant by default.** Every row is tenant-scoped; isolation is enforced at the API and DB layers.
6. **Fail safe.** Watchdog + local buffering on device; graceful degradation and recording continuity in cloud.

## 7. Technology Choices (proposed, rationale in ADR sections)

| Concern | Choice | Why |
| --- | --- | --- |
| Media/telephony | **FreeSWITCH** (+ Kamailio for SIP edge) | Battle-tested SIP/PBX/PSTN, transcoding, recording |
| WebRTC monitoring | **LiveKit** or **Janus** | Mature SFU, browser + mobile SDKs |
| Device uplink | **SRTP/Opus** + **MQTT(control)** | Low latency media, reliable control |
| Backend | **Go** (media/control) + **Node/TypeScript** (app APIs) | Concurrency for media, velocity for CRUD |
| Frontend | **Next.js + React** | SSR, rich real-time dashboards |
| Data | **PostgreSQL** + **Redis** + **S3** | Proven, scalable |
| Deploy | **Kubernetes**, regional | Autoscaling, HA, PoPs |

> See [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) for the decisions that need your
> confirmation (e.g. HDF codec offload, WhatsApp scope, PBX ownership model).
