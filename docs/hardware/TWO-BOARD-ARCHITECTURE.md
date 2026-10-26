# Two-Board Hardware Architecture

> **Design goal:** split real-time voice work from networking/streaming so each
> chip has a single, deterministic job. Two ESP32 modules on one carrier board
> communicate over **UART (control)** + **I2S (audio)**.

## 1. Why two boards

A single ESP32-WROOM can do HFP + Wi-Fi + Opus, but the CPU/RF contention causes
jitter, dropped frames and "dragging". Splitting the work:

| Board | Role | Chip | Why this chip |
| --- | --- | --- | --- |
| **A — Link board** | Bluetooth HFP hands-free + audio I/O + AEC | ESP32-WROOM-32 | Classic BT + dual-core, proven HFP stack |
| **B — Stream board** | Wi-Fi uplink, Opus, SRTP/RTP, MQTT control, OTA | ESP32-S3 | More RAM/PSRAM, faster core, better DSP for Opus |

```
                       +-------------------------------------------+
   Phone  <BT HFP>     |            CARRIER BOARD                  |
   (Gbox-XXXX)         |                                           |
        \              |  +----------------+     +----------------+ |
         \             |  |  A: LINK BOARD |     | B: STREAM BOARD| |
          \            |  |  ESP32-WROOM   |     |  ESP32-S3      | |
           \           |  |                |     |                | |
            +----------->| HFP/AG  <-> AEC|     | Opus <-> SRTP  | |
                       |  | I2S codec      |     | MQTT / OTA     | |
                       |  +--------+-------+     +-------+--------+ |  Wi-Fi
                       |           |  UART(ctrl)      |            |  =====>  Cloud
                       |           +------------------+            |
                       |           |  I2S(PCM audio)  |            |
                       |           +------------------+            |
                       |   5V rail + shared reset/status lines       |
                       +-------------------------------------------+
```

## 2. Interconnect

| Link | Direction | Purpose |
| --- | --- | --- |
| **UART** (2-wire + flow ctrl) | A <-> B | Control, telemetry, state, DTMF, provisioning |
| **I2S / PCM** | A -> B (uplink) and B -> A (downlink) | Real-time 16 kHz mono PCM |
| **GPIO `RESET_PEER`** | A <-> B | Each board can reset the other |
| **GPIO `STATUS`** | both | LED / health handshake |
| **5V + GND** | shared | Single supply, coordinated brown-out |

> Audio is **already PCM** on the I2S link, so no codec negotiation between
> boards. Opus is applied only on Board B (egress Wi-Fi) — Board A never burns
> cycles on compression.

### UART control protocol (framed, CRC)

```
+------+------+------+---------+--------+
| 0xA5 | TYPE | LEN  | PAYLOAD | CRC16  |
+------+------+------+---------+--------+
```
Types: `HELLO`, `STATE`, `TELEMETRY`, `DTMF`, `ROUTE`, `MUTE`, `VOL`, `AEC`, `OTA`.

## 3. Audio path

```
Mic -> codec -> I2S -> [A] AEC/NS/AGC -> PCM ---I2S---> [B] Opus -> SRTP -> Wi-Fi
Speaker <- codec <- I2S <- [A] render  <- PCM --I2S--- [B] Opus <- SRTP <- Wi-Fi
```

- **PCM:** 16 kHz, mono, 16-bit (STT-friendly — see STT-AUDIO.md).
- **Frame:** 20 ms; I2S DMA of 2-3 buffers.
- **Clock:** Board A is I2S **master** (owns the codec MCLK/BCLK/WS); Board B is slave.
- **Latency budget:** end-to-end target < 150 ms (see QOS-PERFORMANCE.md).

## 4. Power & brown-out

- Single **5 V** input (USB-C), LDO/buck to 3.3 V for both modules.
- A shared **brown-out detector** puts both boards into a known state.
- Board A may keep the BT link alive during a brief Wi-Fi outage on Board B, so
  the phone call is not torn down (audio is buffered/comfort-muted).

## 5. Reset coordination

See [RESET-AND-POWER.md](RESET-AND-POWER.md). Summary:

- **POR** resets both.
- Board B reset does **not** drop the BT link (Board A keeps audio + re-syncs).
- Board A reset re-announces via UART `HELLO`; Board B re-establishes the media.

## 6. BOM (indicative)

| Ref | Part | Notes |
| --- | --- | --- |
| A1 | ESP32-WROOM-32 / -32E | Link board |
| B1 | ESP32-S3-WROOM-1 (N8R2) | Stream board, PSRAM for jitter buffer |
| U1 | ES8311 (or ES7210 + ES8311) | Mic + speaker codec |
| U2 | PAM8302 | Speaker amp |
| J1 | USB-C | Power + debug |
| SW1/SW2 | EN / BOOT | Manual reset / flash |
| — | 5V->3.3V buck | Shared rail |

## 7. Alternative (single-chip) note

If cost dominates, a **single ESP32-S3** can host both roles, but you lose the
isolation guarantees above. Keep the two-board design for products that must
never stutter; use the single-chip variant only for low-duty-cycle deployments.
