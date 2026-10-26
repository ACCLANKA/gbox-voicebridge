# Bluetooth Headset Mode (no WhatsApp API required)

Voice Bridge **does not need any WhatsApp API**. Instead, the phone uses the
Gbox as a **Bluetooth headset**, and the call audio still flows through the
bridge.

## How it works

```
WhatsApp call on phone  ->  audio routed to "Gbox-XXXX" (BT HFP headset)
                        ->  Gbox captures mic / plays speaker
                        ->  Gbox streams over Wi-Fi to the cloud
                        ->  routed to your PBX / VPS / listener
```

The phone thinks it is talking to a normal Bluetooth headset. No provider API,
no WhatsApp Business account required.

## Setup

1. Pair **`Gbox-XXXX`** in the phone's Bluetooth settings.
2. Start (or answer) the call in **WhatsApp**.
3. On the phone, set the audio output to **Gbox-XXXX** (headset). Many phones
   do this automatically when the headset is connected.
4. The dashboard shows the call under **Calls** (source = **WhatsApp**).

### Cellular calls
The same works for normal cellular calls: select **Gbox-XXXX** as the audio
device and the call is bridged identically (source = **Cellular**).

## What works / what doesn't

| Feature | WhatsApp via BT headset | Cellular via BT headset |
| --- | --- | --- |
| Two-way audio bridging | **Yes** | **Yes** |
| Incoming-number detection | Best-effort (app) | Carrier CLI |
| DTMF through the call | **Not guaranteed** | Reliable (SIP/inband) |
| Call answer/hangup control | On the phone | On the phone |

> **DTMF limitation:** WhatsApp uses its own transport, so we cannot guarantee
> DTMF digits survive or are detected on a WhatsApp call. For IVR/PIN flows,
> prefer a **cellular/PSTN/SIP** call leg.

## Why audio quality is acceptable

- Wideband **mSBC** is used on the HFP link when the phone supports it.
- Opus 16-24 kbps, in-band FEC over Wi-Fi hides minor loss.
- AEC + gentle AGC keep the stream clean for both listeners and **STT**.

## Phone compatibility notes

- **Android:** enable "Media audio" / "Phone audio" for the Gbox in BT settings.
- **iOS:** select the Gbox as the audio output during the call.
- Keep the phone within ~10 m of the device; BT range is the weak link.

## When to use the companion app

The app (optional) reads the caller identity to enrich the dashboard and
improves **incoming-number** accuracy, but it is **not required** for audio.
