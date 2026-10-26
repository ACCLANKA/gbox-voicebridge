# Routing & Integrations

The route engine decides **where a device's audio goes**. A tenant can send each
device's voice to any project, VPS, PBX, or number.

## 1. Route Model

A **Route** = match conditions + destination + options.

```yaml
route:
  id: rt_001
  tenant: acme
  project: reception
  match:
    devices: ["dev_abc", "dev_def"]   # or device tags / group
    direction: inbound                # inbound | outbound | both
    caller_number: "+1555*"            # optional pattern
    time_window: "Mon-Fri 09:00-18:00"
  destination:
    type: sip                         # sip | pstn | webrtc | webhook | device | recording
    uri: "sip:reception@pbx.acme.com"
  options:
    codec: opus
    record: true
    max_duration_sec: 3600
  fallback:
    type: pstn
    uri: "tel:+15551234"
```

Evaluation order: **priority → specificity → first match**. No match ⇒ default route or reject.

**Implemented** (`api/services/routing.py`): match keys `devices`, `direction`,
`caller_number` (exact or trailing-`*` prefix) and `time_window`
(`"Mon-Fri 09:00-18:00"`, `;`-separated windows, or a JSON list; a window whose
start is after its end wraps past midnight). `options` and `fallback` are stored as
JSON. The forwarding resolver (`api/services/forwarding.py`) walks matched routes
best-first and, for each, tries its primary destination then its `fallback`, so a
dead primary (e.g. `pstn` with no carrier) degrades to the fallback before the next
route.

## 2. Destination Types

| Type | Description | Target |
| --- | --- | --- |
| `sip` | Register/invite to a SIP URI or PBX | FreeSWITCH/Kamailio |
| `pstn` | Outbound call via carrier trunk | Twilio/Telnyx/plivo/local carrier |
| `webrtc` | Browser/softphone monitoring or 2-way | SFU (LiveKit/Janus) |
| `webhook` | Stream audio/metadata to a custom endpoint | Customer VPS |
| `device` | Device-to-device bridge | Another ESP32 |
| `recording` | Capture only (voicemail/log) | Object store |

> **Implementation status.** The egress plane implements `sip` (UDP/TCP/TLS,
> REGISTER + digest auth, SRTP), `webhook` (HTTP chunked + WebSocket), `recording`,
> `simulated`, and a generic **`socket`** type for a customer VPS (UDP/TCP; this is
> how §7 "VPS streaming" is realised). `pstn`, `webrtc` and `device` are still
> target-only. **The exact URI grammar, options and API are in
> [VOICE-FORWARDING.md](VOICE-FORWARDING.md)** — that document is authoritative for
> the implemented behaviour; this one describes the intended model.

## 3. SIP / PBX Integration

- **Registration mode:** device-tenant registers to their own PBX (bring-your-own-PBX).
  *Implemented:* `register=1` + digest challenge on the `SipTrunkLeg`.
- **Trunk mode:** platform dials tenant PBX via a SIP trunk. *Implemented:* native
  UAC INVITE (UDP/TCP/TLS) — see VOICE-FORWARDING.md §5.
- **Codec:** prefer G.711/G.722 to PBX; transcode from Opus at the media agent.
  *Implemented:* PCMU/8000 (G.711 µ-law); others pending.
- **DTMF:** RFC 2833 + SIP INFO. *(pending)*
- **Security:** TLS (SIPS) + SRTP where the PBX supports it.
  *Implemented:* SIPS signaling + SDES SRTP (`AES_CM_128_HMAC_SHA1_80`); DTLS-SRTP/mTLS pending.
- **Multi-PBX:** a tenant can have several PBX endpoints; route by project/device.

## 4. PSTN / Carrier

- Terminate via carrier SIP trunk; number provisioning and porting handled by carrier.
- **E.164** normalization for all numbers.
- Cost and regulatory compliance vary by country — surface per-destination rates in billing.

## 5. Caller Number (DID) & WhatsApp {#whatsapp}

### Inbound phone call to a platform number
```
PSTN --> Carrier SIP --> Kamailio --> Media Agent --> route (device/PBX)
```

### WhatsApp — important constraint

WhatsApp **voice calling** is **not** a general public API. Options, in order of practicality:

1. **WhatsApp Business Cloud API** — messaging (and some calling features via
   limited beta); voice calling availability depends on approval/region.
2. **Partner BSP** (approved Business Solution Provider) — may expose more.
3. **Missed-call / callback pattern** — user messages/calls; we ring back via PSTN.

**Design decision:** put WhatsApp behind an **Integration Adapter** interface so
the platform is not coupled to one provider's (changing) capabilities. Until a
voice path is approved, support the **messaging + callback** flow.

## 6. Integration Adapter Pattern

```
Routing Service
      │
      ▼
 Integration Adapter (common interface)
   ├── SipAdapter
   ├── PstnAdapter (carrier)
   ├── WebRtcAdapter (SFU)
   ├── WebhookAdapter (custom VPS)
   └── WhatsAppAdapter (BSP / Cloud API)
```
Each adapter exposes: `createLeg()`, `bridge(legA, legB)`, `hangup()`, `events()`.

## 7. Webhook / VPS Streaming

For "send voice to their any project VPS or location":

- **Media modes:** (a) RTP relay (low latency), (b) WebSocket Opus stream, (c) HTTP chunked.
- **Auth:** per-route HMAC signature + mTLS option.
- **Contract:** document expected packet format, sample rate, and backpressure behavior.
- Provide a **reference SDK** so customers can receive the stream easily.

## 8. Failover & Routing Policy

| Scenario | Behavior |
| --- | --- |
| Destination unreachable | Try `fallback` route |
| All routes fail | Play/return error tone, emit failed CDR |
| Device offline mid-call | Hold, attempt reconnect, else tear down |
| Media agent fails | Re-route to another agent (SIP re-INVITE / RTP re-anchor) |
| Region outage | Geo-failover to secondary PoP |

## 9. Number / Address Book

- Tenants manage a **DID inventory**, **contact lists**, and **SIP accounts** in the dashboard.
- Numbers are E.164 normalized; labels + tags for human-friendly routing.
