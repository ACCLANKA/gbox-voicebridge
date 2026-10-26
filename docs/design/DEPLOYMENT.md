# Deployment & Infrastructure Topology

## 1. Regions & PoPs

- **PoPs (media ingest):** deploy where customers are (e.g. Colombo/Ap-south, plus 1–2 more).
- **Control plane:** 1 primary region + 1 warm standby (Phase 1), multi-region active-active later.
- **Media plane:** per-PoP pools; geo-DNS steers devices to the nearest healthy PoP.
- **Data:** regional Postgres primary + read replicas; S3 with cross-region replication for recordings.

## 2. Cluster Layout (Kubernetes)

```
Namespace: edge        -> Ingest edge (SRTP term), Kamailio (SIP edge)   [public LB]
Namespace: media       -> Media agents, FreeSWITCH, SFU (LiveKit/Janus)  [node ports, hostNetwork opt]
Namespace: control     -> api-gateway, identity, device, routing, call, billing, notifier
Namespace: web         -> dashboard (Next.js)
Namespace: data        -> Postgres (operator), Redis, MinIO/S3 gateway
Namespace: observ      -> Prometheus, Grafana, Loki, Tempo, Alertmanager
```

**Media nodes** are special: enable **hostNetwork** or dedicated node pools,
hugepages, and low-latency tuning. Keep media and CRUD workloads separate so a
database spike never touches audio.

## 3. Environment Strategy

| Env | Purpose |
| --- | --- |
| dev | local / docker-compose |
| staging | full stack, synthetic calls, mirrors prod |
| prod | multi-PoP, HA |

Infrastructure as code (**Terraform**) + manifests (**Helm/Kustomize**).
No manual prod changes.

## 4. CI/CD

```
PR -> lint + unit tests -> build images -> deploy to staging -> integration/QoS tests
   -> manual approval -> canary (5%) -> monitor -> full rollout
```
- Blue/green for the dashboard; **canary** for media agents (validate MOS before full).
- **DB migrations** with backward-compatible expand/contract.
- Rollback plan per service; media agents drain before replacement.
- Firmware ships on its own pipeline (staged OTA rollout, see DEVICE-FIRMWARE.md).

## 5. Configuration & Secrets

- Config via environment + config maps; per-region and per-PoP overrides.
- Secrets via Vault / cloud secrets manager; injected at runtime.
- Feature flags for gradual rollout of routing/integration features.

## 6. Backups & DR

| Asset | Backup | RPO | RTO |
| --- | --- | --- | --- |
| Postgres | PITR + daily snapshots | 5 min | 1 h |
| Recordings/S3 | versioned + cross-region | 0 | 1 h |
| Redis | ephemeral (rebuildable) | n/a | n/a |
| Config/IaC | git | 0 | 30 min |

Restores are **tested** on a schedule. Documented DR runbook with PoP failover.

## 7. Observability Stack

- **Metrics:** Prometheus + Grafana; business + QoS dashboards.
- **Logs:** Loki (structured JSON); correlation IDs across services.
- **Traces:** Tempo/OpenTelemetry; trace a bridge from device → edge → agent → destination.
- **Alerts:** Alertmanager → on-call; SLO-based burn-rate alerts.

## 8. Cost Controls

- Right-size media agents; passthrough codecs to avoid transcode CPU.
- Regional storage tiering for recordings; retention policies.
- Monitor egress bandwidth (the dominant media cost).

## 9. Local Development

```
docker-compose up   # postgres, redis, minio, freeswitch, api, web
```
Provide a **device simulator** (sends synthetic SRTP + control) so full bridge
flows can be developed without hardware.
