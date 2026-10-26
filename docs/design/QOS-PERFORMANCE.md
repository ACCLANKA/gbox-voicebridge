# QoS, Performance & Load Balancing

Latency, jitter, loss, and availability. See also [AUDIO-PIPELINE.md](AUDIO-PIPELINE.md).

## 1. Service Level Objectives (SLOs)

| Metric | Target |
| --- | --- |
| Mouth-to-ear latency (in-region) | < 250 ms |
| One-way media latency | < 150 ms |
| Jitter | < 30 ms |
| Packet loss | < 1 % |
| MOS | ≥ 4.0 |
| Bridge setup success rate | ≥ 99.5 % |
| Media availability | 99.9 % |
| Device online ratio | ≥ 99 % |

## 2. Load Balancing Layers

```
                    ┌──────────────────────────────┐
   Device ──DNS────►│  Geo-DNS (nearest PoP)       │
                    └──────────────┬───────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │  L4 LB (anycast / NLB)       │
                    └──────────────┬───────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │  Ingest Edge (SRTP terminate)│
                    └──────────────┬───────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │  Media Agent Pool (least-load)│
                    └──────────────┬───────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │  SIP-aware L7 (Kamailio)      │
                    └──────────────────────────────┘
```

### Balancing inputs
- **Device → edge:** DNS to nearest PoP + L4 anycast.
- **Edge → media agent:** **least-loaded** by `(active_sessions, CPU, egress_bandwidth)`; consistent hashing not needed (sessions are short-ish) but sticky within a bridge.
- **SIP legs:** Kamailio dispatcher with load-based and location-based selection.
- **WebRTC legs:** SFU node selection by region + capacity.

### Capacity guardrails
- **Max sessions per agent** (per CPU class). Reject/drain above threshold.
- **Graceful drain:** stop accepting, move new bridges elsewhere; existing finish.
- Autoscale on `active_sessions` and `cpu` metrics; scale-down only when drained.

## 3. Autoscaling

| Signal | Action |
| --- | --- |
| Session count / CPU > 70 % | Scale out media agents |
| Session count / CPU < 30 % (sustained) | Drain + scale in |
| Regional surge | Prefer burstable pool; blend with reserved |
| Control-plane RPS | Scale API pods (HPA) |

**Sticky bridging:** once a bridge is assigned to an agent, it stays there
unless the agent fails (then re-anchor at another agent).

## 4. High Availability & Failover

| Failure | Mitigation |
| --- | --- |
| Media agent crash | Redundant pairs; re-anchor bridge; client re-INVITE |
| PoP outage | Geo-DNS health checks reroute to next PoP |
| Carrier trunk down | Fallback route / alternate trunk |
| DB primary down | Managed HA (Patroni/multi-AZ) with automatic failover |
| Redis down | Degraded (no presence) but media continues |
| Region outage | Multi-region active-active (Phase 3) |

**Rule:** the media plane must survive control-plane degradation. Never block media on a DB write.

## 5. Real-Time Monitoring

- Per-bridge live metrics: latency, jitter, loss, MOS, buffer depth, codec.
- Fleet dashboards: online devices, active bridges, agent load, region health.
- **Alerts:** MOS < 3.5, loss > 3 %, agent CPU > 85 %, device offline > 5 min, trunk failures.
- Distributed tracing across control plane; RTCP-XR / QoS reports from media.

## 6. Capacity Planning (rough)

- Codec bandwidth: Opus ~24 kbps → **~6 KB/s per direction**; add SRTP/IP overhead (~40–60 KB/s per bridge both ways with packet overhead).
- Transcoding costs ~10–20× CPU vs passthrough — minimize by matching codecs end-to-end.
- Size agents by **max concurrent bridges** at target MOS, then add 30 % headroom.

## 7. Device-Side Reliability

- Watchdog reboots a hung device; boot reason reported to dashboard.
- Auto-reconnect with jittered backoff; last-known config cached locally.
- Local buffering (100–300 ms) rides out Wi-Fi blips without dropping the call.
- Degradation ladder: wideband → narrowband; buffer grows; mute-before-garbage.

## 8. Continuous Quality Program

1. Synthetic audio probes between PoPs (measure latency/loss/MOS 24×7).
2. Periodic real-call sampling for MOS (with consent).
3. Regression gates in CI: latency + loss test on a canary agent before rollout.
4. Post-incident reviews fed back into SLOs and tuning defaults.
