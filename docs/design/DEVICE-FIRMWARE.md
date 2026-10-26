# Device Firmware — ESP32-WROOM

> Target: **ESP32-WROOM-32** (dual-core Xtensa, Wi-Fi + Classic BT + BLE)

> **Two-board variant:** for products that must never stutter, the firmware runs
> on **two boards** — a Bluetooth/audio board (**ESP32-WROOM**) and a
> Wi-Fi/streaming board (**ESP32-S3**). See
> [hardware/TWO-BOARD-ARCHITECTURE.md](../hardware/TWO-BOARD-ARCHITECTURE.md),
> [hardware/CHIP-CAPACITY-AND-SPEED.md](../hardware/CHIP-CAPACITY-AND-SPEED.md),
> [hardware/RESET-AND-POWER.md](../hardware/RESET-AND-POWER.md) and
> [hardware/FIRMWARE-VERSIONING.md](../hardware/FIRMWARE-VERSIONING.md).
> Everything below applies to **Board A (the link board)**; Board B reuses the
> same FreeRTOS/net code without the BT stack.
>
> **For customers / field setup:** see the client docs under
> [clients/](../clients/README.md) — quick start, provisioning, routing, and
> [clients/BLUETOOTH-HEADSET-MODE.md](../clients/BLUETOOTH-HEADSET-MODE.md).

## 1. Capability Reality Check

ESP32-WROOM is a strong, cheap choice, but plan within its limits:

| Capability | Supported | Notes |
| --- | --- | --- |
| Classic BT **HFP-HF** (phone call audio) | Yes | This is the **primary** audio path. |
| **BLE** | Yes | Use for **provisioning/control only** (not call media). |
| Wi-Fi | Yes | 2.4 GHz only (single band). |
| SoftAP + captive portal | Yes | Used for Wi-Fi setup (see PROVISIONING.md). |
| Opus encode | Limited | Use **fixed-point Opus** or a lighter codec; CPU-bound. |
| "HD" audio | Limited | **mSBC wideband** on the BT link; true lossless HD is not possible over HFP. |

> **Important:** If you need **BLE audio (LE Audio / LC3)** as an *audio* path,
> plain ESP32 is **not** sufficient — LE Audio requires a newer radio (e.g.
> ESP32-C5/H-series or a dedicated LE Audio SoC). See OPEN-QUESTIONS.
> On WROOM, **BLE = provisioning/control**, **Classic BT HFP = audio**.

## 2. Firmware Architecture (FreeRTOS tasks)

```
+-----------------------------------------------------------------------+
|                              app_main                                 |
+-----------+--------------+---------------+-------------+--------------+
| bt_hfp    | audio_in     | audio_out     | net         | prov_task    |
| task      | task         | task          | task        | (BT+SoftAP)  |
| (AG link) | (capture/enc)| (decode/play) | (SRTP/MQTT) |              |
+-----------+--------------+---------------+-------------+--------------+
| ctrl_task | ota_task     | telemetry_task| wdt_task    |              |
| (BLE ctrl)| (signed OTA) | (metrics)     | (watchdog)  |              |
+-----------+--------------+---------------+-------------+--------------+
```

Task priority (high -> low): `audio_in/out` > `bt_hfp` > `net` > `ctrl` > `telemetry`.
Audio tasks must **never block** on network or flash.

State machine:

```
BOOT -> PROVISIONING (if no valid Wi-Fi creds) -> CONNECTING -> OPERATIONAL
                                                   ^               |
                                                   |               v
                                                   +----- RECONNECTING / OTA <-+
```

## 3. Audio Path

- **Codec IC:** external I2S codec (e.g. ES8311 mic+speaker) preferred for quality.
- **I2S DMA:** 3 buffers x 20 ms; ISR copy is the only hard-real-time work.
- **Frames:** 20 ms @ 16 kHz (mSBC path) -> encode -> RTP.
- **Local jitter buffer** (downlink): 100-300 ms, adaptive, bounded.
- **AEC/NS/AGC:** run if CPU allows; otherwise rely on phone-side processing.
- **STT-friendly defaults** (16 kHz wideband, mild NS, gentle AGC): see
  [STT-AUDIO.md](STT-AUDIO.md). **DTMF** in/out (RFC 4733 + inband fallback):
  see [DTMF.md](DTMF.md) and [SIGNALING-AND-TONES.md](SIGNALING-AND-TONES.md).
- **Firmware development & versioning** (SemVer, release channels, A/B OTA,
  compatibility matrix): see
  [../hardware/FIRMWARE-VERSIONING.md](../hardware/FIRMWARE-VERSIONING.md).

## 4. Connectivity

- **Wi-Fi:** 2.4 GHz; store up to N SSID profiles; jittered reconnect backoff.
- **Media uplink:** SRTP/Opus -> nearest ingest PoP (DNS-resolved, health-checked).
- **Control:** MQTT/TLS persistent session; LWT (last-will) marks device offline.
- **Heartbeat:** 5-10 s; telemetry: RSSI, free heap, FW version, uptime, codec.
- If Wi-Fi is **unavailable or credentials invalid**, the device falls back to
  **provisioning mode** (see section 5).

## 5. Provisioning (Bluetooth + SoftAP)

The device supports two easy setup paths. Full detail in
[PROVISIONING.md](PROVISIONING.md).

### 5.1 Bluetooth name "Gbox" (easy connect from phone)
- The device advertises a Classic BT / BLE name **`Gbox`** so it appears in the
  phone's Bluetooth list and connects with a single tap.
- Optionally, a companion app uses BLE GATT to send Wi-Fi credentials directly.

### 5.2 SoftAP "GboxWifi" + captive portal (when Wi-Fi is unavailable)
- If no valid Wi-Fi, the device starts a **SoftAP** named **`GboxWifi`**.
- A captive portal serves a setup page at **`http://192.168.4.1`**.
- The user can **scan for nearby SSIDs** and/or **type the SSID + password**,
  then save. The device joins the network and leaves provisioning mode.

```
Device boots, no Wi-Fi creds
   -> SoftAP "GboxWifi" (192.168.4.1)
   -> user opens portal, picks SSID + enters password
   -> device saves to NVS, connects, verifies reachability
   -> PROVISIONING -> OPERATIONAL
```

## 6. Security on device

- Per-device **X.509 certificate** + private key in encrypted NVS / secure element.
- **Secure Boot** + **Flash Encryption** enabled in production.
- Signed OTA images; **A/B partition** with automatic rollback.
- No shared secrets in firmware images across the fleet.
- SoftAP is **setup-only**: time-limited, optionally password-protected, and it
  must not expose the internal API without the provisioning token.

## 7. OTA

- Pull-based: device checks manifest -> downloads -> verifies signature -> swaps partition -> reboots.
- **Staged rollout:** canary 1% -> 10% -> 100%; auto-halt on failure telemetry.
- Report `fw_version` + boot success to dashboard for fleet view.

## 8. Bill of Materials (indicative)

| Item | Example part | Purpose |
| --- | --- | --- |
| MCU module | ESP32-WROOM-32 | BT + Wi-Fi + compute |
| Audio codec | ES8311 / ES7210 | Mic in / speaker out |
| Mic | MEMS I2S / analog | Capture |
| Speaker amp | PAM8302 | Playback |
| Power | USB-C / 5V | Supply |
| (Optional) Secure element | ATECC608 | Key storage |
| (Optional) LE Audio radio | newer SoC | Only if BLE audio is required |

## 9. Anti-"dragging" firmware rules

1. Audio tasks pinned to a core; network never preempts audio.
2. Bounded queues everywhere - drop policy is explicit, logged, and reported.
3. Wi-Fi reconnection **mutes** the far end rather than emitting garbage.
4. Never send partial/corrupt frames upstream; drop and conceal locally.
