# Incoming Call Detection & Identification

> Goal: when a call reaches the phone (WhatsApp, cellular, or any VoIP app), the
> ESP32 acts as a Bluetooth **headset**, captures the audio, sends it to the
> server, and our system **identifies the incoming call** so it can be shown in
> the dashboard and routed correctly.

## 1. The hard truth about Bluetooth HFP

When the phone routes a call to the ESP32, it does so over **HFP (Hands-Free
Profile)** using the **SCO/eSCO** audio link. HFP gives us *state and audio*, but
it is deliberately **telephony-agnostic** — it does **not** tell the headset
which app or which number is calling.

| Information | Available to ESP32 over HFP? | Notes |
| --- | --- | --- |
| "A call is ringing" | ⚠️ Sometimes | via `+CIEV: callsetup=1` — **but VoIP apps may not emit it** |
| "A call is active" | ✅ Usually | via `+CIEV: call=1`, and reliably via **SCO link up** |
| "Call ended" | ✅ Usually | via `+CIEV: call=0` / **SCO link down** |
| Call direction (in/out) | ✅ If indicators emitted | `callsetup` 1=incoming, 2/3=outgoing |
| **Which app** (WhatsApp/cellular) | ❌ **No** | HFP has no app concept |
| **Caller number / name** | ❌ Rarely | `+CLIP` works for *some* cellular calls; WhatsApp/VoIP usually not |
| Audio stream | ✅ | SCO/eSCO, 8 kHz CVSD or 16 kHz mSBC (wideband) |

**Conclusion:** the device can reliably detect **that audio is being routed**
(i.e. *a call is happening*), but **identifying the app and caller requires
enrichment from the phone.** We solve this with three detection layers.

## 2. Three Detection Layers

```
┌─────────────────────────────────────────────────────────────────────┐
│ Layer A — SCO link detection  (robust, device-side)                  │
│   Phone opens eSCO/SCO to the ESP32 ⇒ "phone is sending call audio". │
│   Works for WhatsApp, Telegram, cellular, SIP apps — anything.       │
│   Gives: call active/inactive + timing.  NOT app/number.            │
├─────────────────────────────────────────────────────────────────────┤
│ Layer B — HFP call indicators  (device-side, best-effort)            │
│   +CIEV call/callsetup/callheld, RING, +CLIP.                        │
│   Gives: direction (incoming/outgoing), sometimes caller ID.         │
├─────────────────────────────────────────────────────────────────────┤
│ Layer C — Companion app enrichment  (phone-side, optional)           │
│   A small app reads the incoming-call notification (Android          │
│   NotificationListener) and posts {app, caller, ts} to our API.      │
│   Gives: WHICH APP + caller name/number.                             │
└─────────────────────────────────────────────────────────────────────┘
```

**Layer A alone** = "a call is happening on this device" (anonymous call session).
**Layer A + B** = direction + sometimes number.
**Layer A + B + C** = full identification (WhatsApp + caller name).

### Why Layer A (SCO) is the backbone
Unlike HFP indicators, **SCO link establishment is emitted for any app** that
routes call audio to the headset — including WhatsApp. The device watches:
- `SCO up` → mark `call.audio_link` active (start anonymous call session).
- `SCO down` → mark `call.ended`.

### 2.1 HFP specifics to implement (Layer B)

```
Enable indicators:   AT+BIA=1,1,1,1,1,1,1      (call, callsetup, callheld, ...)
Enable caller ID:    AT+CLIP=1                 (if AG supports it)
Enable CMER:         AT+CMER=3,0,0,1           (indicator events)
Listen for:
  +CIEV: <ind>,<val>   (ind values per AT+CIND?)
  RING
  +CLIP: "<number>",<type>
HFP controls we can send:
  ATA       -> answer incoming call
  AT+CHUP   -> hang up / reject
```
Android does **not** expose third-party VoIP caller IDs to the HF, so treat
`+CLIP` as *best-effort* (mostly cellular).

## 3. System Architecture for Detection

```
   Phone (WhatsApp / cellular / VoIP app)
        │  ① audio over BT HFP (SCO/eSCO)
        ▼
   ESP32 (Hands-Free)
        │ ② Call Event API  (WS/MQTT)   {device_id, event, ts, hfp_state}
        ▼
   ┌──────────────────────────────────────────────────────────┐
   │              Call Detection Service (cloud)              │
   │   • ingests device events (Layer A/B)                    │
   │   • ingests companion enrichment (Layer C)               │
   │   • CORRELATES by device_id + time window                │
   │   • creates/updates a CallSession                        │
   └───────────────┬───────────────────────────┬──────────────┘
                   │                           │
   Phone companion app (optional)             │ call.incoming / .answered / .ended
        │ ③ Enrichment API                    ▼
        └──────────────────────────────►  Dashboard · Webhooks · Routing Engine
        (app, caller, ts)                 (identify → route → meter → record)
        │
        └ ④ audio (SRTP) → Ingest Edge → Media Agent → Route destination
```

## 4. Correlation & Identification Algorithm

```
On device event (Layer A/B):
  find_or_create CallSession for device_id where state != ENDED and
     event.ts within [now-5s, now]
  update state: ringing | active | ended
  set direction if HFP provided it
  detection_sources += [sco_link, hfp_indicator]

On enrichment event (Layer C):
  match session where abs(enrich.ts - session.ts) <= CORRELATION_WINDOW (default 3s)
      and same device
  if matched:
      session.source   = normalize(app)      # whatsapp | cellular | sip_app | telegram
      session.caller   = { name, number }
      session.confidence = high (0.9+)
      detection_sources += [companion_app]
  else:
      buffer enrichment for up to WINDOW; if still unmatched, create a
      "phone-only" session (call seen by app but not via device) flagged for review

Identify result:
  1. Companion app match  -> source + caller, high confidence
  2. +CLIP number present  -> number known, source likely cellular
  3. SCO only              -> source = unknown, caller = null (anonymous)
```

`source` normalization maps Android package names → logical source:
`com.whatsapp → whatsapp`, `com.android.phone` / `com.samsung.android.dialer →
cellular`, `org.linphone`/`com.sip.* → sip_app`, etc.

## 5. Call Session State Machine

```
IDLE
  └─(SCO up / callsetup=1)─► DETECTED      (source=unknown)
        ├─(enrichment arrives)─► ENRICHED  (source+caller known)
        ├─(call=1 / answered)─► ANSWERED
        └─► BRIDGED (media bound to route) ─► RECORDING (optional)
  └─(SCO down / call=0)─────► ENDED ─► emit CDR + call.ended webhook
```
> Enrichment can arrive **after** ANSWERED (fast answer); the session is updated in place.

## 6. Companion App (Layer C)

### Android (recommended)
- **NotificationListenerService** — read the incoming-call notification from
  WhatsApp/Telegram/etc.; extract title (caller), and package name.
- Optionally **CallScreeningService / TelecomManager** for cellular caller ID.
- Foreground service (with notification) to stay alive; battery-optimization
  exemption requested.
- Posts to **Enrichment API** with `device_id` (learned from our backend or pair
  token), `app`, `caller`, `direction`, `ts`.
- Explicit user consent + privacy policy (reads notifications).

### iOS (limited)
- No notification access. Options: **CallKit `CXCallObserver`** can observe calls
  but not reliably the app/number for all cases; VoIP apps using CallKit may be
  observable. Treat iOS enrichment as **best-effort / limited**.
- Fallback on iOS: SCO-only anonymous sessions (Layer A).

### Privacy & consent
- Enrichment is **opt-in** per user; toggled in the app.
- Only metadata (app + caller) is sent — never message content.
- Documented in the tenant privacy policy; retention controls apply.

## 7. Reliability & Edge Cases

| Case | Handling |
| --- | --- |
| VoIP call emits no HFP indicators | Layer A (SCO) still detects the call |
| Quick answer before enrichment | Update session in place; webhook `call.enriched` after |
| No companion app installed | Session is anonymous (`source=unknown`) |
| Multiple calls / call waiting | HFP `callheld` → create sub-session; correlate by time |
| Outgoing call | Detected via `callsetup=2/3` or SCO up; `direction=outbound` |
| Phone plays music (A2DP) | Ignore — only **call** SCO is relevant; filter by profile |
| Clock skew between phone & device | Use server receive time + tolerance window |
| Spoofed enrichment | mTLS/auth on Enrichment API; bind phone↔device via pairing |

## 8. Why This Design

- **Works with no WhatsApp API.** Detection rides on Bluetooth + optional companion app, not on any provider API.
- **Provider-agnostic.** Same mechanism identifies cellular, WhatsApp, Telegram, SIP apps.
- **Degrades gracefully.** SCO-only ⇒ still a usable (anonymous) call session.
- **Server-side identification.** The cloud correlates events and is the single source of truth — so the dashboard, routing, and billing all see a consistent `CallSession`.

See **[../api/CALL-EVENTS-API.md](../api/CALL-EVENTS-API.md)** for the concrete API.
