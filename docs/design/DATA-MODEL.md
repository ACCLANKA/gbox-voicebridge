# Data Model

Primary store: **PostgreSQL**. Cache/presence: **Redis**. Objects: **S3/MinIO**.
Every tenant-scoped table carries `tenant_id` and an index on it.

## 1. Entity Relationship (conceptual)

```
tenants 1──* users
tenants 1──* projects
tenants 1──* devices
projects 1──* devices
tenants 1──* routes
projects 1──* routes
devices  1──* bridges        (a device makes many bridge sessions)
routes   1──* bridges
bridges  1──1 cdr
bridges  1──0..1 recordings
tenants  1──* usage_records
tenants  1──* invoices
invoices 1──* invoice_lines
tenants  1──* payments
devices  1──* device_telemetry
devices  1──* firmware_deployments
tenants  1──* api_keys
tenants  1──* audit_logs
```

## 2. Core Tables

### tenants
`id (pk), name, slug, status, balance, currency, credit_limit, plan_id, created_at`

### users
`id (pk), tenant_id (fk), email, password_hash, role, mfa_enabled, last_login_at, created_at`

### projects
`id (pk), tenant_id (fk), name, description, created_at`

### devices
`id (pk), tenant_id (fk), project_id (fk nullable), serial, label, mac, cert_fingerprint,
 fw_version, status (unprovisioned|online|offline|error), last_seen_at, wifi_rssi,
 codec_pref, pop_region, created_at`

### device_telemetry (time-series; consider TimescaleDB/ClickHouse)
`device_id (fk), ts, rssi, rtt_ms, jitter_ms, loss_pct, free_heap, uptime_sec, fw_version`

### routes
`id (pk), tenant_id (fk), project_id (fk nullable), name, priority, match (jsonb),
 dest_type, dest_uri, options (jsonb), fallback (jsonb), enabled, created_at`

### dids (inbound numbers a tenant owns)
`id (pk), tenant_id (fk), number (E.164, unique), country, label, device_id (fk nullable),
 project_id (fk nullable), route_id (fk nullable), capabilities, enabled, created_at`
See [PSTN-AND-DIDS.md](PSTN-AND-DIDS.md).

### bridges (live + historical sessions)
`id (pk), tenant_id, device_id, project_id, route_id, direction, from_number, to_number,
 state, codec, started_at, answered_at, ended_at, duration_sec, packets, bytes,
 loss_pct, jitter_ms, mos, recording_id, agent_id`

### cdr (1:1 with finished bridge)
`id (pk), bridge_id (fk), tenant_id, cost, currency, billing_components (jsonb),
 rounded_minutes, created_at`

### recordings
`id (pk), tenant_id, bridge_id (fk), object_key, size_bytes, duration_sec,
 codec, encrypted (bool), retention_until, created_at`

### plans
`id (pk), name, base_price, currency, included_devices, included_minutes,
 overage_per_min, features (jsonb), is_public`

### subscriptions
`id (pk), tenant_id, plan_id, status, period_start, period_end, quantity, price`

### usage_records (append-only)
`id (uuid pk), tenant_id, project_id, device_id, metric (bridge_seconds|storage_gb|
 telephony_seconds|api_calls), quantity, unit, occurred_at, idempotency_key (unique)`

### usage_rollups
`tenant_id, project_id, metric, period (hour|day|month), quantity, computed_at`

### invoices
`id (pk), tenant_id, number, status (draft|open|paid|void), subtotal, tax, total,
 currency, period_start, period_end, issued_at, due_at, pdf_url`

### invoice_lines
`id (pk), invoice_id (fk), description, metric, quantity, unit_price, amount`

### payments
`id (pk), tenant_id, invoice_id (fk), provider, provider_ref, amount, currency,
 status, created_at`

### api_keys
`id (pk), tenant_id, name, key_hash, scopes, last_used_at, expires_at, created_at`

### firmware
`id (pk), version, channel (stable|beta), object_key, sha256, signature, notes, created_at`

### firmware_deployments
`id (pk), firmware_id (fk), target (device|group|fleet), device_id nullable,
 status (pending|downloading|applied|failed), created_at, applied_at`

### audit_logs
`id (pk), tenant_id, actor_user_id, action, target_type, target_id, metadata (jsonb),
 ip, created_at`

## 3. Indexing & Scale Notes

- Partition `device_telemetry` and `usage_records` by **time**.
- `bridges` hot rows: index on `(tenant_id, started_at desc)` and `(device_id, started_at desc)`.
- Use **jsonb + GIN** only where query patterns justify it.
- Recording objects live in S3; DB stores only metadata + keys.
- Consider a **time-series DB** (Timescale/ClickHouse) for telemetry and QoS analytics at scale.
- All money stored as **integer minor units** + currency, never floats.

## 4. Retention

| Data | Default retention |
| --- | --- |
| CDR / usage | 7 years (billing/legal) |
| Recordings | configurable (30/90/365 days) |
| Telemetry | 30–90 days raw, aggregates longer |
| Audit logs | 2+ years |
