# Operations & Deployment Guide

See [../design/DEPLOYMENT.md](../design/DEPLOYMENT.md) for the topology rationale.

## Environments

| Environment | Purpose | Notes |
| --- | --- | --- |
| dev | Local work | docker-compose |
| staging | Pre-production | full stack + synthetic calls |
| production | Live | multi-PoP, HA |

## Build

```bash
# Control + media services
go build ./...
# Dashboard
cd web && npm ci && npm run build
# Firmware
cd firmware && idf.py build
```

## Release Process

1. Update `CHANGELOG.md`.
2. Bump version (SemVer).
3. Tag: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`.
4. Push tag: `git push origin vX.Y.Z`.
5. CI builds images + firmware artifacts.

## Deploy

```bash
terraform apply            # infra
helm upgrade --install control ./deploy/helm/control
helm upgrade --install media   ./deploy/helm/media
```

Media agents deploy **canary-first**; validate MOS on synthetic calls before rollout.

## Firmware Rollout

1. Upload signed firmware to the registry.
2. Canary 1% of fleet → monitor boot success + QoS.
3. Widen to 10% → 100%.
4. Auto-halt on failure telemetry; rollback via A/B partition.

## Rollback

- **Services:** redeploy previous Helm revision.
- **Firmware:** point fleet to previous signed version (A/B rollback).
- **DB:** migrations are expand/contract; revert app first, then schema.

## Post-Deployment Checks

- Health endpoints return `200`.
- Synthetic call: MOS ≥ 4.0, loss < 1%, one-way latency < 150 ms.
- Devices reconnecting and reporting telemetry.
- No alert-manager firings for 30 minutes.

## On-Call Runbooks

- [ ] Device cert revocation
- [ ] PoP failover
- [ ] Carrier trunk failover
- [ ] Tenant suspension (fraud/balance)
- [ ] Restore Postgres from PITR
- [ ] Recording key rotation
