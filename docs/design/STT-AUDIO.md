# STT-Friendly Audio Quality

Voice Bridge audio must be clean enough for **speech-to-text** (transcription,
IVR, analytics), in addition to being intelligible to a human listener.

## 1. Target parameters

| Parameter | Value | Rationale |
| --- | --- | --- |
| Sample rate | **16 kHz** | STT sweet spot (8 kHz loses fricatives) |
| Bit depth | 16-bit linear PCM | Standard for ASR front-ends |
| Channels | Mono | Avoid channel mix-down artefacts |
| Preferred bandwidth | **Wideband** | Prefer mSBC/Opus wideband over narrowband |
| Frame | 20 ms | Matches Opus/RTP |
| Codec on wire | Opus 16 kHz, 16-24 kbps | Preserves speech band 50-8000 Hz |

### Recommended Opus settings for STT

```
opus_encoder_ctl(enc, OPUS_SET_SAMPLE_RATE(16000));
opus_encoder_ctl(enc, OPUS_SET_BANDWIDTH(OPUS_BANDWIDTH_WIDEBAND));
opus_encoder_ctl(enc, OPUS_SET_BITRATE(18000));   /* 16-24k */
opus_encoder_ctl(enc, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
opus_encoder_ctl(enc, OPUS_SET_FORCE_CHANNELS(1));
opus_encoder_ctl(enc, OPUS_SET_INBAND_FEC(1));    /* hide loss, keep words */
```

> Prefer **CELT/hybrid** disabled and **SILK (voice)** mode for ASR: it preserves
> formants and is loss-tolerant. Enable **in-band FEC** so dropped packets don't
> delete whole words.

## 2. DSP that helps vs hurts

| Stage | Setting for STT |
| --- | --- |
| **AEC** | On (speakerphone). Prevents double-talk from smearing transcripts |
| **Noise suppression** | **Mild** only — aggressive NS removes fricatives ("s", "f") |
| **AGC** | Gentle, target -18 LUFS; too much pumps and clips words |
| **High-pass** | 80-100 Hz (remove rumble; keep voice) |
| **De-reverb** | Optional; helps in bare rooms |
| **Music/EQ** | Off |

## 3. Quality floor

- Require **MOS ≥ 4.0** for STT sessions (see QOS-PERFORMANCE.md).
- Keep **packet loss < 0.5%**; above 1% add FEC and drop bitrate.
- Avoid transcoding chains: keep one decode at the ASR edge, no 16k->8k round-trips.

## 4. Downlink (TTS / prompts)

- Render TTS at 16-24 kHz for naturalness; resample once to 16 kHz for the link.
- Do **not** apply the STT noise gate to playback.

## 5. Telephony caveat

A PSTN leg may be narrowband (8 kHz). When one leg is narrowband, STT accuracy
drops — the pipeline should **flag the session as narrowband** so downstream
ASR can switch models, rather than pretending it is wideband.
