# Control API Reference (Draft v0.1)

REST + WebSocket. Base URL: `https://api.voicebridge.example/v1`.
All requests require `Authorization: Bearer <token>` (user JWT) or a scoped API key.
Every resource is tenant-scoped; `tenant_id` is derived from the token.

## Auth

| Method | Path | Description |
| --- | --- | --- |
| POST | `/auth/login` | Email/password → access + refresh tokens |
| POST | `/auth/refresh` | Refresh access token |
| POST | `/auth/mfa/verify` | Complete MFA challenge |
| POST | `/auth/logout` | Revoke session |

## Tenants & Users

| Method | Path | Description |
| --- | --- | --- |
| GET | `/tenant` | Current tenant profile |
| GET | `/users` · POST `/users` | List / invite users |
| PATCH | `/users/{id}` | Update role/status |
| GET | `/audit-logs` | Tenant audit trail |

## Projects

| Method | Path | Description |
| --- | --- | --- |
| GET | `/projects` · POST `/projects` | List / create |
| GET · PATCH · DELETE | `/projects/{id}` | Manage |

## Devices

| Method | Path | Description |
| --- | --- | --- |
| GET | `/devices` | List (filter: status, project) |
| GET | `/devices/{id}` | Detail + last telemetry |
| PATCH | `/devices/{id}` | Rename, assign project, codec prefs |
| DELETE | `/devices/{id}` | Decommission |
| POST | `/devices/claim` | Claim via provisioning token |
| POST | `/devices/{id}/reboot` | Remote reboot |
| GET | `/devices/{id}/telemetry` | Time-series metrics |
| POST | `/devices/{id}/ota` | Trigger firmware update |
| GET | `/devices/{id}/status` | Live online/offline + bridge state |

### Device status (WebSocket) `wss://api.../v1/devices/stream`
```json
{ "device_id": "dev_abc", "state": "online", "bridge": "br_123",
  "rssi": -58, "jitter_ms": 12, "loss_pct": 0.2, "mos": 4.3, "ts": "..." }
```

## Provisioning

| Method | Path | Description |
| --- | --- | --- |
| POST | `/provision/claim` | Device redeems a claim token → tenant, cert, config |
| GET · POST | `/provision/tokens` | List / create provisioning tokens |
| DELETE | `/provision/tokens/{id}` | Revoke a token |
| GET | `/provision/manifest` | Device fetches config + OTA manifest after claiming |

`POST /provision/claim` (device-authenticated):
```json
{ "serial": "GBX-A1B2C3", "claim_token": "clm_...", "fw": "1.0.3" }
```
Response:
```json
{ "device_id": "dev_abc", "tenant_id": "ten_1", "project_id": "prj_1",
  "certificate": "-----BEGIN CERTIFICATE-----...", "pop": "eu-west-1.voicebridge.example" }
```

> Wi-Fi setup itself (Bluetooth name "Gbox", SoftAP "GboxWifi", portal at
> `http://192.168.4.1`) is a **device-local** feature — see
> [../design/PROVISIONING.md](../design/PROVISIONING.md) for its local
> `/api/scan`, `/api/config`, `/api/status` endpoints.

## Routes

| Method | Path | Description |
| --- | --- | --- |
| GET | `/routes` · POST `/routes` | List / create |
| GET · PATCH · DELETE | `/routes/{id}` | Manage |
| POST | `/routes/{id}/test` | Dry-run match against a device |

## Routes — example body
```json
{
  "name": "Reception to PBX",
  "project_id": "prj_1",
  "priority": 100,
  "match": { "devices": ["dev_abc"], "direction": "inbound" },
  "destination": { "type": "sip", "uri": "sip:reception@pbx.acme.com" },
  "options": { "codec": "opus", "record": true },
  "fallback": { "type": "pstn", "uri": "tel:+15551234" }
}
```

## Numbers (DIDs)

Inbound E.164 numbers a tenant owns. See
[design/PSTN-AND-DIDS.md](../design/PSTN-AND-DIDS.md).

| Method | Path | Description |
| --- | --- | --- |
| GET | `/dids` · POST `/dids` | List / add a number |
| POST | `/dids/validate` | Normalise/validate a number (no store) |
| GET · PATCH · DELETE | `/dids/{id}` | Manage |
| POST | `/dids/{id}/assign` | Attach to a device / project / route |

Numbers are stored in canonical E.164 (`+15551234567`); national input like
`(415) 555-0199` is normalised. Duplicate → `409`, invalid number → `400`.
The dashboard's **Numbers (DIDs)** tab uses the equivalent `/v1/ui/dids` routes.

## Voice forwarding (egress)

Where a device's audio actually goes. Full grammar/options:
[design/VOICE-FORWARDING.md](../design/VOICE-FORWARDING.md).

| Method | Path | Description |
| --- | --- | --- |
| GET | `/forwarding/capabilities` | Supported types/transports/options (public) |
| POST | `/forwarding/validate` | Parse a destination without storing it |
| GET · PUT · DELETE | `/forwarding/devices/{id}` | Per-device destination override |

`dest_type` ∈ `sip | pstn | socket | webhook | recording | simulated`; `options` carries
protocol knobs (e.g. `{"register":1,"password":"…","srtp":1}`, `{"mode":"rtp"}`,
`{"carrier_host":"sbc.carrier.example:5060"}` for `pstn`).
A device with no override follows its matched route; with neither it is `simulated`.

## Bridges (live media sessions)

| Method | Path | Description |
| --- | --- | --- |
| GET | `/bridges` | List active (filter: tenant/project/device) |
| GET | `/bridges/{id}` | Detail + live QoS |
| POST | `/bridges/{id}/hangup` | Force teardown |
| POST | `/bridges/{id}/listen` | Get WebRTC listen-in credentials |
| GET | `/bridges/stream` (WS) | Live bridge events |

## CDR & Recordings

| Method | Path | Description |
| --- | --- | --- |
| GET | `/cdr` | Query call detail records (filters + pagination) |
| GET | `/recordings` | List recordings |
| GET | `/recordings/{id}/url` | Short-lived signed download URL |
| DELETE | `/recordings/{id}` | Delete (retention/policy permitting) |

## Billing

| Method | Path | Description |
| --- | --- | --- |
| GET | `/plans` | Available plans |
| GET · POST | `/subscription` | View / change plan |
| GET | `/usage` | Usage by metric/period |
| GET | `/invoices` · GET `/invoices/{id}` | List / detail |
| GET | `/invoices/{id}/pdf` | Download PDF |
| POST | `/payment-methods` | Add card (tokenized) |
| GET | `/balance` | Current balance/credits |

## Admin (platform operator only)

| Method | Path | Description |
| --- | --- | --- |
| GET · POST | `/admin/tenants` | List / create tenants |
| GET · PATCH | `/admin/tenants/{id}` | Suspend, set limits |
| GET | `/admin/fleet` | Devices online/offline, firmware spread |
| POST | `/admin/credits` | Grant/refund credits |
| GET | `/admin/carriers` | Trunk health |
| POST | `/admin/impersonate` | Support impersonation (audited) |

## Errors
```json
{ "error": { "code": "route_not_found", "message": "No matching route",
             "request_id": "req_..." } }
```
Standard codes: `400` validation, `401` unauthenticated, `403` forbidden,
`404` not found, `409` conflict, `429` rate limited, `5xx` server.

## Webhooks (outbound to tenants)
Events: `bridge.started`, `bridge.ended`, `device.online`, `device.offline`,
`invoice.created`, `invoice.paid`, `balance.low`.
Signed with HMAC-SHA256 (`X-VB-Signature`); retried with backoff.
