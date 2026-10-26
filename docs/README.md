# Voice Bridge — Documentation Index

**Voice Bridge** is a hardware + cloud platform that turns a small ESP32-based
device into a Bluetooth-to-cloud audio bridge. A customer pairs a phone (or any
Bluetooth HFP audio source) to the device; the device forwards that audio over
Wi-Fi to the cloud, where it is routed to a SIP PBX, a PSTN number, a browser
softphone, a webhook, or another endpoint. Everything is managed from a
multi-tenant web dashboard with billing.

## Who this is for

- **End customer (tenant):** owns devices, projects, and routes; pays a subscription/usage bill.
- **Admin (platform operator):** onboards tenants, manages plans, monitors the fleet, handles support.

## Document map

| # | Document | What it covers |
| --- | --- | --- |
| 1 | [design/ARCHITECTURE.md](design/ARCHITECTURE.md) | Master architecture: layers, components, call flows |
| 2 | [design/AUDIO-PIPELINE.md](design/AUDIO-PIPELINE.md) | Codecs, jitter buffer, AEC, latency budget, QoS knobs |
| 3 | [design/DEVICE-FIRMWARE.md](design/DEVICE-FIRMWARE.md) | ESP32 firmware, HFP/BLE, provisioning, OTA |
| 3a | [design/PROVISIONING.md](design/PROVISIONING.md) | "Gbox" Bluetooth setup + "GboxWifi" SoftAP captive portal |
| 4 | [design/ROUTING-INTEGRATIONS.md](design/ROUTING-INTEGRATIONS.md) | Routes, SIP/PBX, PSTN, WhatsApp, WebRTC, webhooks |
| 4a | [design/PSTN-AND-DIDS.md](design/PSTN-AND-DIDS.md) | E.164 normalisation, PSTN carrier trunk, DID inventory |
| 5 | [design/CALL-DETECTION.md](design/CALL-DETECTION.md) | Detecting & identifying incoming calls (WhatsApp/cellular) |
| 5a | [design/DASHBOARD.md](design/DASHBOARD.md) | Live IoT device + call status, incoming/WhatsApp number |
| 6 | [design/BILLING-MULTITENANCY.md](design/BILLING-MULTITENANCY.md) | Tenants, plans, usage metering, invoicing |
| 7 | [design/DATA-MODEL.md](design/DATA-MODEL.md) | Database schema + ERD |
| 8 | [design/SECURITY.md](design/SECURITY.md) | Identity, device trust, media encryption, compliance |
| 9 | [design/QOS-PERFORMANCE.md](design/QOS-PERFORMANCE.md) | Latency, load balancing, scaling, monitoring |
| 10 | [design/DEPLOYMENT.md](design/DEPLOYMENT.md) | Topology, regions, HA, DR, CI/CD |
| 11 | [design/ROADMAP.md](design/ROADMAP.md) | Phased delivery plan |
| 12 | [design/OPEN-QUESTIONS.md](design/OPEN-QUESTIONS.md) | Decisions still needed from the business |
| 13 | [api/API.md](api/API.md) | Control API (tenants, devices, routes, billing) |
| 14 | [api/CALL-EVENTS-API.md](api/CALL-EVENTS-API.md) | Call events + identification API (device/phone/server) |
| 15 | [design/STT-AUDIO.md](design/STT-AUDIO.md) | STT-friendly audio: 16 kHz wideband, AEC/NS/AGC, ASR handoff |
| 16 | [design/DTMF.md](design/DTMF.md) | DTMF in/out, RFC 4733 vs in-band, WhatsApp-call limits |
| 17 | [design/SIGNALING-AND-TONES.md](design/SIGNALING-AND-TONES.md) | SIP signaling, ringback/comfort tones, call progress |
| 18 | [design/VPS-CAPACITY.md](design/VPS-CAPACITY.md) | How many devices/calls a Contabo small VPS handles |
| 19 | [hardware/TWO-BOARD-ARCHITECTURE.md](hardware/TWO-BOARD-ARCHITECTURE.md) | Split design: BT/audio board + Wi-Fi/streaming board |
| 20 | [hardware/CHIP-CAPACITY-AND-SPEED.md](hardware/CHIP-CAPACITY-AND-SPEED.md) | ESP32 vs ESP32-S3: CPU, RAM, headroom, limits |
| 21 | [hardware/RESET-AND-POWER.md](hardware/RESET-AND-POWER.md) | Reset design, brown-out, deep sleep, watchdog |
| 22 | [hardware/FIRMWARE-VERSIONING.md](hardware/FIRMWARE-VERSIONING.md) | Firmware dev workflow, SemVer, channels, A/B OTA |
| 23 | [clients/README.md](clients/README.md) | **Client configuration docs** (start here for customers) |
| 24 | [design/DEVICE-PROTOCOL.md](design/DEVICE-PROTOCOL.md) | Device↔cloud wire contract (WS control + audio framing) — **implemented** |
| 25 | [design/VOICE-FORWARDING.md](design/VOICE-FORWARDING.md) | **Voice egress**: SIP trunk, VPS socket, webhook, recording — grammar, options, API — **implemented** |

### Client documentation (`docs/clients/`)

Customer-facing, no coding required.

| Document | What it covers |
| --- | --- |
| [clients/QUICK-START.md](clients/QUICK-START.md) | First call in 15 minutes |
| [clients/DEVICE-PROVISIONING.md](clients/DEVICE-PROVISIONING.md) | Wi-Fi join, Bluetooth pairing, factory reset, LEDs |
| [clients/ROUTING-SIP.md](clients/ROUTING-SIP.md) | Route a device to a PBX, VPS, PSTN or webhook |
| [clients/PBX-SBC-EXAMPLES.md](clients/PBX-SBC-EXAMPLES.md) | Asterisk/FreePBX, CUCM/CUBE, 3CX, Kamailio snippets |
| [clients/QUALITY.md](clients/QUALITY.md) | Bandwidth, firewall ports, DSCP, STT setup |
| [clients/BLUETOOTH-HEADSET-MODE.md](clients/BLUETOOTH-HEADSET-MODE.md) | WhatsApp/cellular bridging via BT headset (no API) |
| [clients/FAQ.md](clients/FAQ.md) | Common client questions |

## Core terminology

| Term | Meaning |
| --- | --- |
| **Device** | The ESP32-WROOM hardware unit sold to a customer. |
| **Tenant** | A customer account (a company or an individual). |
| **Project** | A logical grouping of devices + routes under a tenant (e.g. "Reception"). |
| **Route** | Rule that says "audio from device X goes to destination Y". |
| **Bridge** | An active media session connecting a Bluetooth source to a cloud destination. |
| **Agent / Core** | Cloud media server that terminates and relays audio. |
| **CDR** | Call Detail Record — billing/analytics record of a bridge session. |
| **Call Session** | Server-side record of a single call (source, caller, state, QoS). |
| **Layer A/B/C** | Detection layers: SCO link / HFP indicators / companion app (see CALL-DETECTION.md). |
| **Enrichment** | Phone-side metadata (app + caller) that identifies a detected call. |
