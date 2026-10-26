# Calls & Detection

Voice Bridge sees incoming calls on the paired phone (via a companion app /
notification listener) and bridges the audio through the Gbox device.

## Sources

| Source | Meaning |
| --- | --- |
| **WhatsApp** | Call arrived through WhatsApp; the number is the WhatsApp identity |
| **Cellular** | Native SIM/PSTN call; the number is the carrier CLI when available |

If the WhatsApp calling API is unavailable, the phone links to the ESP32 as a
**Bluetooth headset** and the call audio still flows — no WhatsApp API needed.

## What the dashboard shows

For every call, **Calls** displays:

- device, direction (incoming/outgoing),
- **incoming number** (CLI) and **WhatsApp number** when known,
- caller name (if resolved),
- **route** (e.g. `Reception -> sip:reception@pbx.acme.com`),
- live **duration**, state, and confidence,
- **QoS**: MOS, jitter, loss,
- **echo**: AEC on/off and ERLE dB.

## Call controls

| Control | Effect |
| --- | --- |
| **Answer** | Bridge the incoming call to the device |
| **Hold / Resume** | Pause / restore the media path |
| **Hang up** | End the call and write the CDR |
| **Re-route** | Send the call to a different SIP/VPS target |

## Call history (CDR)

Ended calls remain in the history table with duration and quality, so you can
audit dropped or poor-quality calls. Combine this with **Connection Monitor**
to correlate a bad call with a Wi-Fi drop.

## Privacy

Only call metadata needed for routing and billing is stored. Audio is streamed,
not retained. Configure retention under your project's security policy — see the
design docs for details.
