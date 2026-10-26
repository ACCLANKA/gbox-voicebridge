# Nuwan Toy Bot v2 — firmware + settings snapshot

Flashed ESP32-S3 node ("GBox I2S toy"): INMP441 mic + MAX98357A amp, no Bluetooth.
Speaks Sinhala through the VPS pipeline (Whisper → DeepSeek → your VITS voice).

Board: ESP32-S3 N16R8, port COM21, serial **GBX-I2S-01**, Wi-Fi `pchost`.

## Firmware image

| File | Offset | Bytes | SHA-256 |
|---|---|---|---|
| `bin/gbox_toy-v2.bin` | `0x10000` | 1,071,488 | `053417fd405db6ef67f4071646685c54ffebeb95ae366c7c96173bc3b3699d40` |
| `bin/bootloader.bin` | `0x0` | — | see SHA256SUMS.txt |
| `bin/partition-table.bin` | `0x8000` | — | see SHA256SUMS.txt |
| `config/nvs-GBX-I2S-01.bin` | `0x9000` | 24,576 | see SHA256SUMS.txt |

Verified by reading the app back off the flash and hashing it — identical to the file above.

## Firmware settings (src/main.c)

| Constant | Value | Why |
|---|---|---|
| `RATE` / `FRAME` | 16000 / 320 | 20 ms frames, what the server expects |
| `MIC_WS/BCLK/DIN` | 4 / 5 / 6 | INMP441, L/R tied to GND |
| `SPK_LRC/BCLK/DOUT` | 16 / 15 / 7 | MAX98357A, stereo L=R |
| `MIC_GAIN` | 4.0 | the INMP441 reads ~20 dB quiet on this board |
| `PLAY_GAIN` | 2.0 | +6 dB; 3.0 was too loud |
| `HPF_A` | 0.96 | one-pole DC blocker ~100 Hz |
| `VAD_RMS` | 0.030 | after gain; room ≈ −39 dB |
| `MIN_SPEECH_FRAMES` | 15 | 300 ms before it counts as speech |
| `MAX_UTTER_S` | 4 | capture cap |
| `SILENCE_STOP_FRAMES` | 40 | 800 ms of quiet ends the utterance |
| `PREBUF_MS` | 60000 | wait for the whole reply — streaming starves at this TTS speed |
| `dma_desc_num` | 8 | 160 ms of I2S cushion |
| filler | `src/filler.h` | 2.45 s "එක මොහොතක් ඉන්න, මම බලන්නම්", same voice, plays while fetching |

Behaviour: 3 beeps at boot (amp check) → listen → on speech end send the clip and play the
filler **concurrently** → play the answer → resume. Silence for 8 s after a reply triggers
`/v1/toy/nudge` (max 3). Saying bye/goodbye/stop/නවත්ත/එපා/බායි idles it for 60 s.

## Server side (VPS `/opt/voicebridge/api/routers/toy.py`)

| Setting | Value |
|---|---|
| TTS calls per reply | **one** (`_ONE_CALL_CHARS = 400`); long replies grouped at sentence boundaries |
| Parallel TTS | **1 worker** — the container serves one request at a time |
| Cache | PCM by text hash in `/app/data/tts_cache` (`toy:` prefix) |
| Audio returned | 16 kHz mono PCM16 (device no longer resamples) |
| Trim | 100 ms margin, so the first consonant survives |
| Endpoints | `POST /v1/toy/talk`, `POST /v1/toy/nudge`, `GET /v1/toy/health` |
| Auth | `X-Toy-Key` from the server `.env` (`TOY_KEY`) |

## Restore

```bash
# app only (keeps Wi-Fi credentials)
esptool.py --chip esp32s3 --port COM21 write_flash 0x10000 bin/gbox_toy-v2.bin

# full restore including Wi-Fi
esptool.py --chip esp32s3 --port COM21 write_flash \
  0x0 bin/bootloader.bin 0x8000 bin/partition-table.bin \
  0x9000 config/nvs-GBX-I2S-01.bin 0x10000 bin/gbox_toy-v2.bin
```

Build: `D:\build-toy.bat` (ESP-IDF 5.4 at `D:\esp\esp-idf`), project `D:\fw-toy`.
`src/secrets.h` is generated locally (52-char `VB_TOY_KEY`) and is **not** in the repo.

## Measured behaviour

- Turn latency ≈ 12–30 s: Whisper 4–6 s, DeepSeek 1–2 s, TTS ~1× realtime.
- Playback reports `0 starve waits` and played time == audio duration.
- TTS saturates all six VPS cores; one 18 s answer costs ~21 s of synthesis.
