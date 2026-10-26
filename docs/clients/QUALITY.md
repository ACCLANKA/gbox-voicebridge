# Audio Quality, Ports & STT Setup

## Network requirements

| Requirement | Value |
| --- | --- |
| Wi-Fi band | **2.4 GHz** (required) |
| Signal at device | RSSI better than **-67 dBm**; avoid < -75 dBm |
| Uplink per active call | ~**80 kbps** (Opus, bidirectional) |
| Bandwidth (busy site) | ~1 Mbps per 10 concurrent calls + headroom |
| Latency to our gateway | < **50 ms** RTT preferred, < 100 ms acceptable |
| Jitter | < **15 ms** |
| Packet loss | < **0.5%** |

## Firewall ports (outbound from your site)

| Proto | Port(s) | Purpose |
| --- | --- | --- |
| UDP | 443 | Control / QUIC (preferred) |
| TCP | 443 | MQTT-over-TLS, HTTPS control, OTA |
| UDP | 3478 | STUN/TURN (NAT traversal) |
| UDP | 16384-32767 | RTP/SRTP media |
| TCP/UDP | 5061/5060 | SIP (only if you route to your own PBX) |

> No **inbound** ports are required for normal operation. Media is outbound.

## QoS / DSCP (recommended)

Mark media at the edge so your router prioritises it:

| Traffic | DSCP | Notes |
| --- | --- | --- |
| Voice (RTP) | **EF (46)** | Highest priority |
| Signaling (SIP) | **CS3 (24)** | |
| Control (MQTT) | AF21 | |
| OTA / bulk | CS1 | Lowest |

Enable **WMM** on the access point and put the device on a **dedicated SSID/VLAN**
if the network is busy.

## Speech-to-text (STT) setup

Voice Bridge audio is tuned for ASR by default.

| Setting | Value |
| --- | --- |
| Sample rate | **16 kHz**, mono, 16-bit |
| Codec | Opus wideband, 16-24 kbps, in-band FEC |
| AEC | **On** |
| Noise suppression | **Mild** |
| AGC | Gentle, target -18 LUFS |

**To attach your own ASR:**
1. Use a **Webhook** route (or SIP to your VPS) to receive the audio/metadata.
2. We can deliver **16 kHz PCM** (or Opus) frames + call events.
3. Keep transcoding to a single decode at your ASR edge.

**Telephony caveat:** if the call leg is narrowband (PSTN 8 kHz), ASR accuracy
drops; the event includes a `bandwidth` flag so you can switch models.

## Tuning tips

- Echo? Turn **AEC on**, lower speaker volume < 80, re-check sound balance.
- Choppy? Check MOS/jitter/loss; move closer to the AP; reduce 2.4 GHz congestion.
- Dropped calls? Inspect **Connection Monitor -> link events**; add a backup SSID.
