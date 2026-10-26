# Audio Pipeline — Quality, Latency & QoS

> Status: Draft v0.1 — **This is the core of the product.**

The pipeline has four stages. Each stage is independently tunable and measured.

```
Phone --BT HFP--> ESP32 Input --SRTP--> Cloud Ingest --> Media Agent --> Destination
       (SCO/eSCO)  (capture/enc)      (jitter/FEC/PLC)  (transcode/mix/record)
Phone <--BT HFP-- ESP32 Output <--SRTP-- Cloud Egress <-- Media Agent <-- Destination
```

## 1. Codecs

| Segment | Codec(s) | Notes |
| --- | --- | --- |
| Bluetooth link | **CVSD** (narrowband) / **mSBC** (wideband 16 kHz) | HFP 1.6+ = mSBC. "HDF" here = the wideband/HD path on the Bluetooth link. |
| Device -> cloud | **Opus** (primary), G.711 fallback | Opus 16-24 kbps mono, 20 ms frames |
| Cloud -> PBX/PSTN | **G.711 (PCMA/PCMU)**, **G.722**, **Opus** | Depends on trunk capabilities |
| Cloud -> browser | **Opus** (WebRTC) | With in-band FEC |
| Transcoding | EC (720p) in media agent | Only when legs differ; counted in CPU budget |

**Rule:** negotiate the best common codec end-to-end; transcode at the media
agent only when required. Prefer passthrough when both legs share a codec.

## 2. Bluetooth / HFP (device <-> phone)

- Role: **Hands-Free (HF)**; phone is Audio Gateway (AG).
- **mSBC wideband** is the default when the phone supports it -> 16 kHz speech.
- SCO/eSCO is **isochronous** — no retransmission. Loss/errors must be hidden
  locally (PLC) and not amplified upstream.
- Keep one paired AG at a time per device by default; multi-AG is a config option.
- Reconnect policy: exponential backoff; re-announce HFP service on link loss.

## 3. Capture & Encode (ESP32)

- **I2S + DMA** double/triple buffering; never block the audio task.
- Fixed **20 ms** processing blocks (320 samples @16 kHz) — this is the "frame rate".
- **Fixed-function DSP chain** (I2S -> AEC -> VAD -> Opus encode -> RTP packetize).
- **Gain/AGC** to normalize levels; clipping is worse than a low level.
- Prefer **hardware codec** (e.g. ES8311/ES7210) over internal ADC when audio quality matters.
- Leave >= 30 % CPU headroom for OTA + telemetry + BT.

## 4. Transport (device <-> edge)

- Media: **RTP over UDP**, encrypted with **SRTP** (AES-CM). Opus, 20 ms p-time.
- Control: **MQTT over TLS** (provisioning, config, call signals, telemetry).
- **STUN-like** candidate selection to the nearest ingest PoP.
- Optional **QUIC datagram** transport for head-of-line-blocking immunity (Phase 2).

## 5. Cloud Media Plane

### 5.1 Jitter buffer & de-jitter
- **Adaptive jitter buffer** per leg, target depth = measured jitter + safety margin.
- Start **optimistic** (small buffer, low latency); grow on detected jitter, shrink slowly.
- Report per-bridge: `jitter_ms`, `buffer_depth_ms`, `late_lost_pkts`.

### 5.2 Packet loss handling (the "dragging"/robotic-voice problem)
- **In-band FEC** (Opus LBRR, redundant payloads) for random loss.
- **PLC** (packet loss concealment) — interpolate lost 20 ms frames.
- **NACK/RTX** where supported (mostly browser legs) — never for isochronous BT.
- **Redundancy**: send previous Opus frame piggybacked on ~1 in N packets under loss.

### 5.3 Voice queuing & reordering
- RTP sequence numbers drive resequencing; buffer small reorder windows (~40 ms).
- **Dropping (dragging) prevention:** never let the buffer grow unbounded — drop oldest
  when depth exceeds a hard ceiling and mark `forced_drop`. Unbounded growth = latency spiral.
- Keep a **bounded** queue per direction; backpressure to source rather than queue explosion.

### 5.4 Transcoding / mixing / recording
- Transcode only when codecs differ; GPU/CPU budgeted per agent.
- Recording taps the mix (not each leg) to save storage; store encrypted (see SECURITY).
- DTMF: RFC 2833 telecom events + SIP INFO fallback; regenerate on the far leg.

## 6. Echo Cancellation (AEC)

The device is a **speakerphone** in many deployments: its mic hears the far-end
voice coming out of its own speaker. Without cancellation this returns to the
far end as **echo** (and can feed back as howling). AEC is therefore a
first-class, always-on part of the pipeline.

### 6.1 Where echo happens (and who cancels it)

```
Caller (far end) ──► PBX/SIP ──► Cloud ──► Device speaker (plays caller)
                                                 │  acoustic coupling
                                                 ▼
                                            Device mic (hears caller)
                                                 │  ⇦ ECHO — must be removed
                                                 ▼
                                            Device ──► Cloud ──► Caller
```

| Echo source | Cancelled by | Notes |
| --- | --- | --- |
| **Device speaker -> device mic** (acoustic) | **Device AEC (primary)** | Hardware/software AEC on the ESP32; reference = speaker signal |
| Weak device AEC / loud volume | **Cloud AEC (secondary)** | Server-side AEC per bridge, reference = downlink RTP |
| Phone-side echo | Phone's built-in AEC | Already handled by the phone OS |

### 6.2 Primary: on-device AEC

- **Full-duplex** processing: keep playback and capture sample-aligned.
- **Reference signal** = the downlink audio we send to the speaker (before the
  amp, ideally after DAC for true linearity).
- Classic **NLMS/RLS adaptive filter** with a **double-talk detector (DTD)** and
  **non-linear residual suppressor (NLP/residual echo suppressor)**.
- **Echo Return Loss Enhancement (ERLE)** target: **>= 45 dB** typical,
  **>= 60 dB** for loud/hands-free units.
- Convert to **8 kHz (narrowband) / 16 kHz (wideband)** processing; run before
  AGC and encoder.
- CPU-heavy: use a hardware DSP if available (e.g. ES8311/ES7210-class codec with
  on-chip AEC, or an **ESP32-S3 + ESP-ADF** which provides `aec`/`afe`).

### 6.3 Secondary: cloud-side AEC / residual echo control

- Applied per bridge in the **media agent** when device AEC is insufficient
  (cheap boards, loud rooms, speaker distortion).
- Reference = the **downlink stream** (what we sent toward the device).
- Combined with **residual echo suppression** and **noise reduction**.
- Costs CPU/GPU; enable **per-bridge** based on `echo_erle` telemetry.
- Preserves **double-talk** (user and caller talking at once must not be clipped).

### 6.4 Supporting DSP chain (ordering matters)

```
Mic -> [High-pass] -> [AEC] -> [NS] -> [AGC] -> [VAD] -> [Encoder] -> RTP
                       ^
Speaker signal --------+(reference)
```

- **Noise suppression (NS):** spectral subtraction / DNN NS after AEC.
- **AGC:** normalize level *after* AEC (do not amplify echo before cancelling).
- **VAD:** gate silence/comfort noise; feeds "speaking" state to the dashboard.
- **Automatic volume:** reduce speaker gain when ERLE degrades (prevents howl).

### 6.5 Device-side echo-reduction rules

1. Keep speaker volume within the AEC's linear range (avoid clipping/distortion).
2. Physically separate mic and speaker envelope (acoustic isolation).
3. Prefer a **hardware codec with integrated AEC**; offload from the main CPU.
4. Mute playback briefly on Wi-Fi reconnect (avoids re-learning echo path).
5. Report `echo_erle_db`, `double_talk`, `aec_enabled` to the cloud per call.

### 6.6 Telemetry (feeds the dashboard)

| Metric | Meaning |
| --- | --- |
| `aec_enabled` | AEC on for this bridge |
| `echo_erle_db` | Echo return loss enhancement; higher = better |
| `echo_residual_db` | Residual echo after processing |
| `double_talk_active` | Both parties speaking (AEC must not clip) |
| `aec_location` | `device` / `cloud` / `device+cloud` |

## 7. Latency Budget (target one-way < 150 ms)

| Stage | Target | Notes |
| --- | --- | --- |
| BT SCO/eSCO | 20-40 ms | fixed by Bluetooth; can't tune |
| I2S + DSP (incl. AEC) + encode | 10-25 ms | 20 ms frames, small buffers |
| Device -> edge (network) | 15-40 ms | **nearest PoP matters** |
| Jitter buffer | 20-40 ms | adaptive |
| Media agent process | 5-15 ms | passthrough < transcode |
| Edge -> destination | 15-40 ms | trunk/PBX RTT |
| Destination processing | 10-30 ms | PBX/PSTN |
| **Total one-way** | **~100-190 ms** | measure per bridge; alert on regression |

**End-to-end target:** mouth-to-ear **< 250 ms** for in-region legs. AEC adds
~5-15 ms on device and (optionally) ~5-10 ms in cloud — budget for it.

## 8. QoS Tuning Matrix (your stated goals -> levers)

| Goal | Primary levers |
| --- | --- |
| **Audio quality** | mSBC wideband, Opus 20-24 kbps, hardware codec, AGC/AEC, avoid transcodes |
| **No echo** | device AEC (ERLE >= 45 dB) + optional cloud AEC/residual suppressor, volume control |
| **No dragging / stutter** | adaptive jitter buffer, FEC+PLC, bounded queues, wired/powerline Wi-Fi for devices |
| **Frame rate stability** | fixed 20 ms blocks, priority audio task, DMA buffers, CPU headroom |
| **Voice queuing** | RTP resequencing, small reorder window, hard max buffer depth + forced drop |
| **Latency** | nearest PoP, optimistic jitter buffer, passthrough codec, minimal hops |
| **Connectivity** | Wi-Fi RSSI telemetry, auto-reconnect, dual transport (RTP + QUIC opt), heartbeat |
| **Load balancing** | geo-DNS + L4 LB + SIP-aware L7; least-loaded media agent; agent drains on scale-down |

## 9. Wi-Fi / Connectivity (device side)

- Prefer **5 GHz** where available; report RSSI/retries to dashboard.
- Auto-reconnect with jittered backoff; persist multiple SSID profiles.
- **Local buffer** (e.g. 100-300 ms) to ride out short Wi-Fi glitches.
- Alert tenant when a device's link quality degrades below threshold.

## 10. What We Measure (per bridge, real time)

| Metric | Purpose |
| --- | --- |
| Round-trip & one-way latency (ms) | latency SLO |
| Jitter (ms), buffer depth (ms) | de-jitter tuning |
| Packet loss %, late/dropped % | Quality, dragging |
| **MOS (E-model/R-factor)** | single quality score |
| **ERLE (dB) / residual echo** | echo quality |
| Codec, sample rate, p-time | quality context |
| CPU/mem per media agent | capacity, load balancing |
| Wi-Fi RSSI / retries (device) | connectivity triage |

Targets: **MOS >= 4.0**, loss < 1 %, jitter < 30 ms, one-way < 150 ms, **ERLE >= 45 dB**.

## 11. Audio Session State Machine

```
NEGOTIATE_CODEC
   -> OPEN_UPLINK (device->edge SRTP)
   -> OPEN_DOWNLINK (edge->device SRTP)
   -> OPEN_DEST_LEG (SIP/WebRTC/PSTN)
   -> STREAMING (healthy)
        -> DEGRADED (high loss/jitter/echo)  [increase FEC, grow buffer, boost AEC]
        -> REBUFFERING (link blip)           [mute far end, hold]
   -> TEARDOWN  [flush, emit CDR]
```
