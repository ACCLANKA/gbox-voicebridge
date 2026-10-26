# Builder ⇄ Hermes channel

The live channel is the **ops bridge**: a task queue + device-presence API that
rides the existing control plane (`/v1/ops`, see `api/routers/ops.py`). Both
sides already reach that API, so no third-party account or new credential store
is needed.

## Why this and not a webhook / git remote

- **Webhook into the automation service:** impossible here — `RUNTIME_URL` is
  unset, so this deployment is not publicly reachable for inbound delivery.
- **Git remote:** still the best *artifact* transport, but it needs a repo + token
  that has not been provided, and it is not a live signal.
- **The VPS is the one shared surface.** Both the builder (outbound HTTPS) and
  Hermes (local/redeploy) can reach it, so the channel lives there.

## Endpoints (`/v1/ops`)

| method | path | auth | purpose |
| --- | --- | --- | --- |
| GET | `/v1/ops/ping` | none | liveness — proves the channel exists |
| GET | `/v1/ops/devices` | ops | device presence for the tenant |
| GET | `/v1/ops/devices/{serial}` | ops | one device: status/online/ip/fw/last_seen |
| POST | `/v1/ops/tasks` | ops | enqueue a task (subject, body, serial, ref, attachments) |
| GET | `/v1/ops/tasks?status=` | ops | list tasks |
| GET | `/v1/ops/tasks/{id}` | ops | one task |
| POST | `/v1/ops/tasks/{id}/claim` | ops | take it (409 if not pending) |
| POST | `/v1/ops/tasks/{id}/ack` | ops | close it: `{status: done|failed, report}` |

**Auth:** an API key with the `ops` scope (or `admin`/`*`), or an owner/admin user.
Tasks are tenant-scoped — a key in tenant 1 cannot read tenant 2's tasks
(covered by `tests/integration/test_ops.py`).

## Clients

- `scripts/bridge.py` — stdlib-only CLI: `ping`, `send`, `tasks`, `show`,
  `claim`, `ack`, `devices`, `watch`. Run on either side.
- `tools/bench.py` — hardware side: `verify`, `flash` (app-only, sha-guarded),
  `flash-merged`, `nvs`, `monitor`, `sweep`.

## Deploy path

Hermes re-syncs the bundle over `/opt/voicebridge` and redeploys once to get
`/v1/ops`; after that the loop is live. Full copy-paste steps, including minting
the `ops` key, are in `HERMES-COMMANDS.md`.
