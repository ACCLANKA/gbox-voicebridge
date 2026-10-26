# Telephony Signaling (DTMF, SIP, in-band tones)

How call **signaling** and **in-band tones** are carried end-to-end.

## 1. Legs of a call

```
 Phone app (WhatsApp / cellular)
        |  Bluetooth HFP (mSBC audio, phone does signaling)
        v
   [Gbox A] -- PCM -- [Gbox B] -- SRTP/Opus + control --> Cloud media node
                                                                |
                                        +-----------------------+----------------------+
                                        |                      |                      |
                                   SIP/PBX leg            PSTN/SIP trunk         WebRTC listener
```

- The **BT HFP leg** carries audio only; call control (answer/hangup) is HFP AT
  commands from the device (hands-free) — the phone owns the actual signaling.
- The **cloud SIP leg** is where RFC 2833 / SIP INFO DTMF and SIP signaling live.

## 2. Signaling on the cloud leg

| Concern | Mechanism |
| --- | --- |
| Call setup / teardown | SIP (INVITE/BYE) via the integration adapter |
| DTMF | RFC 4733 events preferred, SIP INFO alt, inband fallback |
| Early media / ringback | SIP 183 with SDP, or locally generated comfort tone |
| Codes | PCMA/PCMU/G.722/Opus negotiated per leg |
| Fax/CED | optional T.38 pass-through (out of scope v1) |

## 3. In-band tone generation

When a far end only understands inband tones (legacy IVR), the media node
generates:

- **DTMF:** dual sine, 75 ms on / 75 ms off, standard twist.
- **Ringback / busy / SIT:** generated locally if the far leg is silent.
- **Comfort noise:** low-level on silence to avoid "dead air".

## 4. Detection pipeline

1. RTP RFC 4733 event -> digit (authoritative).
2. Goertzel DSP on PCM -> digit (fallback, with twist/duration checks).
3. Optional cloud ASR can surface spoken digits for analytics (not for control).

## 5. Mapping to device control

The cloud signals the device over the control channel; the device signals the
phone via HFP where possible:

| Event | Cloud -> Device | Device -> Phone |
| --- | --- | --- |
| Incoming call | `CALL_INCOMING` | HFP `RING` / answer |
| Answer | `CALL_ANSWER` | HFP `ATA` |
| Hangup | `CALL_END` | HFP `AT+CHUP` |
| DTMF in | `DTMF <digit>` | inband audio to phone call |
| DTMF out | `DTMF_SEND <digit>` | HFP dial-tone / inband |

## 6. Limits

- On a **WhatsApp** call the phone owns signaling; the bridge can only pass audio.
  See [DTMF.md](DTMF.md) for what that means for tones.
- Codec mismatch (narrowband PSTN <-> wideband phone) requires a **transcoding**
  step at the media node; flag the session bandwidth for STT.
