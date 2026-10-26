# DTMF Handling

Dual-Tone Multi-Frequency (DTMF) tones ("press 1 for sales") are how callers
navigate IVRs and enter PINs. Voice Bridge must carry them **reliably** on
normal (cellular/PSTN/SIP) calls, and treat WhatsApp calls separately.

## 1. Two ways DTMF travels

| Method | How | When to use |
| --- | --- | --- |
| **Inband (audio)** | The tone is part of the audio stream | Legacy PSTN, some mobile calls |
| **Out-of-band (signaling)** | RFC 2833 / RFC 4733 RTP events, or SIP INFO | SIP trunks, modern gateways |

The bridge should **support both** and, where possible, prefer out-of-band
signaling for accuracy.

## 2. Incoming DTMF (caller presses keys)

Three detection stages, best-effort:

1. **Signaling first** — if `RFC2833` events arrive on the RTP stream, decode them
   (no DSP needed, 100% accurate).
2. **DSP fallback** — run a Goertzel/DTMF detector on the 16 kHz PCM
   (Board A or the cloud media node), validating with the twist/energy rules.
3. **De-duplicate** — collapse a signaling event and a coincident inband tone
   into a single digit.

```
RFC2833 event? --yes--> digit (authoritative)
              --no ---> Goertzel(inband) --valid?--> digit
```

Detector requirements:

- Frequencies 697/770/852/941 x 1209/1336/1477/1633 Hz.
- Reject talk-off (speech that mimics tones) via twist + duration (>= 40 ms).
- Report each digit as an event on the control channel (UART type `DTMF`).

## 3. Outgoing DTMF (bridge sends digits to the far end)

- Prefer **RFC 4733** events on the RTP leg; fall back to generating inband tones
  (75 ms tone + 75 ms pause) if the far end is inband-only.
- Expose digit send via the API for IVR automation / testing.

## 4. WhatsApp calls — NOT reliably supported

> **Important:** WhatsApp calls use WhatsApp's own end-to-end signaling and a
> proprietary audio transport. There is **no standard RFC 2833 / SIP INFO DTMF
> channel** on a WhatsApp call, so the bridge **cannot guarantee** DTMF delivery
> for WhatsApp-originated calls.

Practical implications:

| Call type | DTMF reliability |
| --- | --- |
| Cellular / PSTN / SIP | **Reliable** (signaling preferred, inband fallback) |
| WhatsApp (via BT HFP headset) | **Best-effort only** |

For a WhatsApp call bridged through the phone's Bluetooth HFP link, inband tones
*do* travel as audio, so an IVR may react — but detection depends on codec
(mSBC/Opus) fidelity and is **not guaranteed**. Treat WhatsApp DTMF as
**unsupported** in SLAs; document it for clients.

## 5. Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `dtmf.mode` | `auto` | `signaling` \| `inband` \| `auto` |
| `dtmf.min_tone_ms` | 40 | Minimum tone duration to accept |
| `dtmf.send_mode` | `rfc4733` | Outgoing method |
| `dtmf.send_tone_ms` | 75 | Inband tone length |

## 6. Telemetry

Report `dtmf_received`, `dtmf_sent`, `dtmf_detect_method` (`signaling`/`inband`)
so operations can see which path is in use per call.
