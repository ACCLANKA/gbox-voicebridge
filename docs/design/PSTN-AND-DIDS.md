# PSTN & DID Inventory

Phase 2 (see [ROADMAP.md](ROADMAP.md)): place outbound calls to the public
telephone network and own inbound numbers. This document is the single reference
for number handling, the carrier trunk, and the DID inventory.

## 1. E.164 normalisation

All phone numbers are stored and compared in canonical **E.164** form:
`+` then country calling code then national number, 7–15 digits total.

`api/services/numbers.py` is the only place that converts user input to E.164:

| Input | Default country | Result |
| --- | --- | --- |
| `+1 555 123 4567` | — | `+15551234567` |
| `00 44 20 7946 0958` | — | `+442079460958` |
| `15551234567` (code, no `+`) | `US` | `+15551234567` |
| `555 123 4567` | `US` | `+15551234567` |
| `(020) 7946 0958` | `GB` | `+442079460958` (trunk `0` dropped) |
| `+1 (555) 123-4567 x99` | — | `+15551234567` (extension ignored) |

Helpers: `normalize`, `try_normalize`, `is_e164`, `validate` (returns
`{ok, e164, error}`), `calling_code`, `region`, `to_sip_uri`, `dial_user`.
Anything that is not valid E.164 raises `NumberError`.

## 2. PSTN carrier trunk

A `pstn` egress destination dials an E.164 number through a carrier SBC, reusing
the SIP UAC leg (`SipTrunkLeg`) in `api/services/egress.py`.

Destination form: `tel:+E164` (or a bare number). Options (URI query or the
`options` JSON) — see also [VOICE-FORWARDING.md](VOICE-FORWARDING.md):

| Option | Meaning | Default |
| --- | --- | --- |
| `carrier_host` | SBC `host` or `host:port` | `VB_PSTN_CARRIER` |
| `carrier_port` | SBC port | port in `carrier_host`, else 5060/5061 |
| `transport` | `udp` `tcp` `tls` | `VB_PSTN_TRANSPORT` (`udp`) |
| `country` | default country for national numbers | `VB_PSTN_DEFAULT_COUNTRY` (`US`) |
| `register`, `password`, `realm`, `srtp`, `tls_verify` | passed to the SIP leg | — |

```bash
# Route all outbound to the carrier (process env):
VB_PSTN_CARRIER=sbc.carrier.example:5060

# Per-destination:
#   dest_type: pstn, dest_uri: tel:+15551234?carrier_host=sbc.carrier.example:5060
```

If no carrier is configured, parsing a `pstn` destination raises `EgressError`;
the forwarding resolver then degrades to the route's `fallback` (or the next
matching route), so a misconfigured trunk never wedges a call.

## 3. DID inventory

A **DID** is an inbound E.164 number a tenant owns and can point at a device,
project or route. Stored in the `dids` table (`api/db.py`), served by
`api/services/dids.py`.

| Column | Notes |
| --- | --- |
| `id` | `did_…` |
| `tenant_id` | tenant scope (all queries are tenant-scoped) |
| `number` | canonical E.164, `UNIQUE` |
| `country`, `label` | metadata |
| `device_id`, `project_id`, `route_id` | assignment (nullable) |
| `capabilities` | default `voice` |
| `enabled` | soft toggle |
| `created_at` | ISO timestamp |

Operations (`did.create` / `view` / `update` / `assign` / `delete`, plus
`get_by_number` for inbound resolution and `for_device`). Creating or updating a
number normalises it to E.164; a duplicate number raises `DuplicateDid`.

Seed data (tenant `ten_1`) ships three DIDs: `+15551230001` → `dev_1`,
`+15551230002` → `dev_3`, and an unassigned `+442079460958`.

## 4. API

Control plane, `Authorization: Bearer <token>`, permission `routes`
(`api/routers/dids.py`):

| Method & path | Purpose |
| --- | --- |
| `GET /v1/dids` | list the tenant's numbers |
| `POST /v1/dids` | add a number (body: `number`, `label?`, `country?`, `device_id?`, …) |
| `POST /v1/dids/validate` | normalise/validate without storing (`{number, country?}`) |
| `GET /v1/dids/{id}` / `PATCH` / `DELETE` | manage one number |
| `POST /v1/dids/{id}/assign` | attach to `device_id` / `project_id` / `route_id` (null clears) |

Dashboard BFF, cookie session (`api/routers/ui.py`): `GET`/`POST /v1/ui/dids`,
`POST /v1/ui/dids/{id}/assign`, `DELETE /v1/ui/dids/{id}` — surfaced in the
**Numbers (DIDs)** tab of the web dashboard.

Errors: `400` invalid number, `409` duplicate, `404` unknown id, `401` no session.

## 5. Code map

| Concern | Module |
| --- | --- |
| E.164 parsing | `api/services/numbers.py` |
| PSTN parse/build | `api/services/egress.py` (`parse_destination`, `build_leg`) |
| DID store | `api/services/dids.py` |
| Control-plane API | `api/routers/dids.py` |
| Dashboard BFF | `api/routers/ui.py` |
| Dashboard tab | `api/web/app.js` (`renderNumbers`) |
