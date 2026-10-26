# VPS Capacity: how many devices per Contabo "Small" VPS?

> **Short answer:** a Contabo **Small VPS (4 vCPU / 8 GB RAM / ~32 TB traffic)**
> comfortably handles **~300-500 concurrently active calls** when *relaying*
> audio (no transcoding), and a registry of **~10,000-40,000 mostly-idle
> devices**. Add transcoding and it drops to **~100-150 concurrent calls**.
> Do **not** run STT on the same box.

These are engineering estimates to size a pilot, not vendor guarantees. Validate
with a load test before go-live (see §6).

## 1. Reference specs (Contabo, indicative)

| Tier | vCPU | RAM | Traffic | Bandwidth |
| --- | --- | --- | --- | --- |
| **Small** | 4 | 8 GB | ~32 TB/mo | 200-250 Mbps |
| Medium | 6 | 16 GB | ~32 TB/mo | 500 Mbps |
| Large | 8 | 30 GB | ~32 TB/mo | 800 Mbps-1 Gbps |

*(Contabo renames tiers periodically; treat these as a "small" baseline.)*

## 2. Per-device / per-call resource model

| State | Traffic | CPU | Notes |
| --- | --- | --- | --- |
| **Idle** (registered) | ~2-5 MB/mo | ~0 | MQTT heartbeat every 30 s |
| **Active call (relay)** | ~80 kbps | ~1-2% core | SRTP pass-through, jitter buffer |
| **Active call (transcode)** | ~80 kbps | ~5-10% core | Opus <-> PCMU/G.722 |

### Bandwidth math (per continuously-active call)

```
Opus payload          = 20 kbps
RTP+UDP+IP+SRTP hdr   ~ 20 kbps      (40-50 bytes/packet, 50 pkt/s)
-------------------------------------------------------
one-way               ~ 40 kbps
bidirectional         ~ 80 kbps  = 10 KB/s

monthly   = 10 KB/s * 2,592,000 s/mo  ~ 26 GB per 24/7 call
```

```
32,000 GB / 26 GB  ~ 1,230 concurrently-active calls (bandwidth ceiling)
```

### CPU math

```
relay only      : 4 cores / 1.5%  ~ 250-500 concurrent calls
with transcode  : 4 cores / 7.5%  ~  50-120 concurrent calls  (Opus<->PCMU)
with STT on-box : not viable      ->  use a GPU/separate ASR service
```

### RAM math

```
Per active session (jitter buffer + RTP + TLS) ~ 0.5-1 MB
Per idle device (registry row)                 ~ 1-4 KB
8 GB minus OS/services (~1.5 GB) -> plenty for CPU/bandwidth-limited counts
```

## 3. Result — capacity per Small VPS

| Workload | Concurrent active calls | Registered devices |
| --- | --- | --- |
| Signaling only (all idle) | - | **10,000-40,000** |
| Relay, no transcode | **300-500** | 10,000+ |
| Relay + Opus<->PCMU transcode | **100-150** | 10,000+ |
| With on-box STT/ASR | **not recommended** (20-40, CPU-bound) | - |
| Bandwidth hard ceiling | ~1,200 (24/7 calls) | - |

**Rule of thumb:** *Small VPS ≈ 400 concurrent calls (relay) or ~120 with
transcoding.*

## 4. Scaling model

- **Horizontal:** put media relays behind a load balancer; devices register to a
  signaling cluster and get assigned a media node.
- **Region/location:** clients route to the nearest VPS (multi-region) for
  latency; a truly global fleet needs >1 VPS.
- **Separate tiers:** signaling (tiny CPU, high device count) vs media (CPU/band
  heavy) can run on different VPS tiers.
- **STT:** always a separate service (GPU) — never on the signaling/media box.

## 5. Formula cheat-sheet

```
concurrent_calls  = min( bandwidth_cap, cpu_cap )

bandwidth_cap = monthly_traffic_GB / (kbps * 2 * 2592000 / 8 / 1e6)   # 24/7
cpu_cap       = vCPU_cores / cpu_per_call
registered    = min( traffic_GB / idle_GB_per_device , RAM/registry )
```

## 6. How to confirm (before you buy more)

1. Load-test with `sipp` / `sngrep` + a synthetic Opus/media generator.
2. Measure at 100/200/400 concurrent calls: CPU, RAM, egress Mbps, packet loss.
3. Watch **MOS** under load — capacity ends when MOS < 4.0, not when CPU hits 100%.
4. Add 30% headroom for bursts and OTA/telemetry.

## 7. Start-here recommendation

- **Pilot (≤ 200 devices, light usage):** one **Small VPS** is enough.
- **Production (500-2,000 devices, ~100-400 concurrent calls):** **Medium VPS**
  with relay-only, or **2 Small VPS** (1 signaling + 1 media) behind a LB.
- **With transcoding or STT:** size up + separate the ASR onto its own service.
