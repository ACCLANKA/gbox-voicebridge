# Delivery Roadmap

Phased so audio works end-to-end early and business features layer on top.

## Phase 0 — Foundations (weeks 1–3)
- Repo, CI, IaC skeleton, local docker-compose.
- Data model v1 (tenants, users, devices, projects, routes).
- Auth + RBAC + admin/tenant dashboard shell.
- **Device simulator** for media testing without hardware.

## Phase 1 — MVP Audio Bridge (weeks 4–10) 🎯
- ESP32 firmware: HFP-HF + mSBC, I2S codec, Opus, SRTP uplink.
- BLE provisioning + device registration/cert.
- Ingest edge + media agent (passthrough) + **SIP leg** to a test PBX.
  *Progress:* the cloud side is implemented — device WS ingest edge, forwarding
  plane and egress legs (SIP UDP/TCP/TLS + REGISTER + SRTP, UDP/TCP VPS sockets,
  HTTP-chunked/WS webhook, recording). See
  [VOICE-FORWARDING.md](VOICE-FORWARDING.md) and [DEVICE-PROTOCOL.md](DEVICE-PROTOCOL.md).
  Remaining: on-device HFP/Opus (firmware scaffold only) and a real-PBX MOS run.
- One PoP; dashboard: device list, live bridge monitor, basic routes.
- QoS metrics: latency, jitter, loss, MOS on dashboards.
- **Exit:** a real phone call on a real device reaches a PBX with MOS ≥ 4.0.

## Phase 2 — Productizing (weeks 11–18)
- ✅ PSTN via carrier trunk; DID inventory; E.164 normalization — see
  [PSTN-AND-DIDS.md](PSTN-AND-DIDS.md) (`tel:+E164` destination, `/v1/dids`,
  dashboard *Numbers* tab).
- WebRTC monitoring leg (browser listen-in).
- ✅ Routing engine fallback + time windows (`time_window` match key,
  per-route `fallback` chain) — see [ROUTING-INTEGRATIONS.md](ROUTING-INTEGRATIONS.md).
- Recording (encrypted) + retention.
- ✅ Billing v1: plans, usage metering (bridge minutes), invoices, spend controls
  — see [BILLING-MULTITENANCY.md](BILLING-MULTITENANCY.md).
- Multi-PoP + geo-DNS; autoscaling media agents.
- Firmware OTA (staged rollout).

## Phase 3 — Scale & Integrations (weeks 19–30)
- Webhook/VPS streaming adapter + reference SDK.
- WhatsApp integration adapter (per BSP/Cloud API availability — see ROUTING-INTEGRATIONS).
- Admin console advanced: fleet health, audit, impersonation.
- Advanced QoS: FEC/PLC tuning, RTCP-XR, synthetic probes.
- Multi-region active-active; DR drills.
- SOC 2 readiness workstream.

## Phase 4 — Optimization
- QUIC datagram transport option.
- AI features: noise suppression, transcription, sentiment, call summaries.
- LE Audio / newer SoC evaluation if BLE *audio* is required.
- Enterprise SLAs, SSO, dedicated PoPs.

## Critical Path

```
Firmware audio path ──► Media agent bridging ──► SIP leg ──► (MVP)
                                          └──► PSTN ──► Billing ──► (v1 launch)
```

**Highest-risk items:** ESP32 real-time audio stability, Opus CPU on-device,
WhatsApp voice API availability, PSTN regulatory/emergency-call scope.
