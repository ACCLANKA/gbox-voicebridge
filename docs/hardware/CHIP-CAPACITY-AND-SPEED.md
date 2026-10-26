# Chip Capacity & Speed Management

How the firmware keeps audio real-time while Wi-Fi/crypto work continues.

## 1. Budgets (per chip)

### ESP32-WROOM-32 (Link board — Board A)

| Resource | Total | Reserved for audio |
| --- | --- | --- |
| CPU | 2 cores @ 240 MHz | core 0 audio + codec, core 1 HFP/BT |
| SRAM | ~320 KB DRAM | audio DMA + jitter buffers ~64 KB |
| Flash | 4 MB | app + 2 OTA slots |

### ESP32-S3 (Stream board — Board B)

| Resource | Total | Reserved |
| --- | --- | --- |
| CPU | 2 cores @ 240 MHz | core 0 Opus encode, core 1 Opus decode + RTP |
| SRAM + PSRAM | 512 KB + 2 MB | jitter buffer in PSRAM (100-300 ms) |
| Flash | 8 MB | A/B OTA |

## 2. CPU (speed) management

- **Clock:** 240 MHz during an active call; **80 MHz idle** (DFS) to save power.
- **Core pinning** (`xTaskCreatePinnedToCore`):
  - `audio_in` / `audio_out` -> core 0 (never preempted by net).
  - `bt_hfp` -> core 1.
  - `net` / `ota` / `telemetry` -> core 1, **lower priority**.
- **Priority order:** audio > codec > bt > net > control > telemetry.

```
Core 0: [audio_in][audio_out][AEC/NS]        <- hard real-time
Core 1: [bt_hfp] [net/opus/rtp] [ota/tele] <- best effort
```

## 3. Load shedding

| Condition | Action |
| --- | --- |
| CPU > 85% for 1 s | drop AEC refinement, keep core AEC |
| Queue high-water | drop oldest non-key frame, mark `loss` |
| Free heap < 20 KB | disable telemetry verbosity, GC buffers |
| RSSI < -80 dBm | reduce Opus bitrate (24k -> 12k), enable FEC |

Never block audio on flash/network. All queues are **bounded** with an explicit
drop policy that is logged and reported to the dashboard.

## 4. Memory (capacity)

- **Jitter buffer:** adaptive 100-300 ms, in PSRAM on Board B; hard-capped so it
  cannot grow unbounded (prevents the classic "drifting delay").
- **Opus encoder:** fixed-point, ~40 KB state; single instance.
- **TLS/MQTT:** ~50 KB; renegotiate off the audio core.
- **Serving tasks:** each task stack sized and measured; no `malloc` in ISRs.

## 5. Speed metrics reported

`cpu_pct`, `free_heap_kb`, `psram_free_kb`, `audio_overruns`, `audio_underruns`,
`opus_bitrate`, `jitter_ms`, `rssi` — surfaced to the dashboard via telemetry.

## 6. Tuning rules (anti-stutter)

1. One job per core; audio never yields to network.
2. Pre-allocate all buffers at boot (no runtime fragmentation).
3. Adaptive bitrate before adaptive buffering (degrade gracefully).
4. Report every drop/overrun; silent loss is forbidden.

## 7. Flash transport (bench)

The GBX bench boards use a CP2102 USB-UART. At **460800** baud a ~1.36 MB app image
dies mid-write — esptool returns rc=2 around 81% and leaves the board unbootable.
Use **230400**; `tools/bring_up.py` (`FLASH_BAUD`) and `tools/bench.py flash` both
default to it now. A failed high-baud write is recoverable by re-flashing at 230400
— it does not damage the partition table.
