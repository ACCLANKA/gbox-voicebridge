# Firmware Development & Versioning

How the ESP32 firmware is developed, versioned, released and rolled back.

## 1. Repository layout

```
firmware/
  components/            # shared: audio, net, provisioning, dtmf
  boards/
    link_esp32_wroom/    # Board A (BT HFP + audio)
    stream_esp32_s3/     # Board B (Wi-Fi + Opus + SRTP)
  common/
    proto/               # UART control protocol (shared header)
  CMakeLists.txt         # ESP-IDF project
  sdkconfig.defaults
  VERSION                # single source of truth, e.g. 1.4.2
```

## 2. Versioning scheme (SemVer)

`MAJOR.MINOR.PATCH` + build metadata, applied **per board** but pinned together
in a **release train** so A and B stay compatible.

| Change | Bump |
| --- | --- |
| Breaking UART protocol / NVS schema | **MAJOR** |
| New feature, backward compatible | **MINOR** |
| Bug fix, no protocol change | **PATCH** |

```
FW_VERSION       = 1.4.2
RELEASE_CHANNEL  = stable | beta | canary
PROTO_VERSION    = 3        # UART control protocol version
HW_REV           = A2 / B1  # carrier + module revisions
```

Both boards report `fw_version`, `proto_version`, `hw_rev` and `boot_reason`.
The cloud **rejects** a media session if `proto_version` is incompatible.

## 3. Source control model

| Branch | Purpose |
| --- | --- |
| `main` | Always buildable; tagged releases |
| `develop` | Integration of features |
| `feat/*` | One feature |
| `fix/*` | One fix |
| `release/x.y` | Stabilisation before tagging |

Rules:

- Every change goes through a PR with a build + on-device smoke test.
- Tags: `fw-v1.4.2` (both boards), or `fw-link-v1.4.2` for one board only.
- **Changelog** is required per tag and links to the PR.
- CI runs: build (both targets) -> unit tests on host -> static analysis ->
  produce signed images.

## 4. OTA & release channels

```
manifest.json (signed)
  channel: stable -> fw 1.4.2 (link), 1.4.2 (stream)
  rollout: 1% -> 10% -> 100%
  halt_on: boot_fail_rate > 2% | wdt_boot_rate > 1%
```

- **A/B partitions** with automatic rollback on boot failure.
- Staged rollout with telemetry-gated halt (see DEVICE-FIRMWARE.md OTA).
- Devices advertise their channel; a device can be pinned to a version for
  support.

## 5. Compatibility matrix

| Link FW | Stream FW | Proto | Notes |
| --- | --- | --- | --- |
| 1.4.x | 1.4.x | 3 | Current |
| 1.3.x | 1.4.x | 3 | Stream may be ahead (safe) |
| 1.4.x | 1.2.x | 2 | **Blocked** — proto mismatch |

Rule: **proto_version** must match; minor FW skew is tolerated, major is not.

## 6. Release checklist

- [ ] `VERSION` bumped, CHANGELOG updated
- [ ] Both boards build in CI (link + stream)
- [ ] On-device smoke test: boot, BT pair, Wi-Fi join, call bridge, DTMF, AEC
- [ ] Images signed; manifest published to `beta`
- [ ] Canary 1% for 24 h, check WDT/BOD/boot-fail rates
- [ ] Promote to `stable`

## 7. Rollback

- Automatic: boot watchdog failure -> revert to previous partition.
- Manual: dashboard pin to `fw-vX.Y.Z`; device pulls on next check-in.
- Emergency: cut a hotfix PATCH from `release/x.y`.
