# Billing & Multi-Tenancy

## 1. Tenancy Model

| Entity | Description |
| --- | --- |
| **Tenant** | A customer account. Root of all data isolation. |
| **User** | Belongs to a tenant; has a role. |
| **Role** | `owner`, `admin`, `operator`, `viewer`, `billing`. |
| **Project** | Groups devices + routes (e.g. "Reception", "Support Desk"). |
| **Device** | Belongs to a tenant, optionally assigned to a project. |

**Isolation:** every business row carries `tenant_id`; the API enforces it on
every query. Optional: schema-per-tenant or RLS for large customers.

## 2. Roles (RBAC)

| Permission | owner | admin | operator | viewer | billing |
| --- | :-: | :-: | :-: | :-: | :-: |
| Manage users | ✅ | ✅ | ❌ | ❌ | ❌ |
| Manage devices/routes | ✅ | ✅ | ✅ | ❌ | ❌ |
| View live calls | ✅ | ✅ | ✅ | ✅ | ❌ |
| View recordings | ✅ | ✅ | ✅ | ❌ | ❌ |
| Manage billing | ✅ | ❌ | ❌ | ❌ | ✅ |

## 3. Plans & Pricing

| Plan | Base fee | Included | Overage |
| --- | --- | --- | --- |
| Starter | $X/mo | N devices, M minutes | per-minute |
| Business | $Y/mo | more devices/minutes, recording | per-minute |
| Enterprise | Custom | custom, SLA, dedicated | negotiated |

Metered dimensions (choose what the business needs):
- **Devices active** (count).
- **Bridge minutes** (per direction or combined).
- **Recording storage** (GB-month).
- **Telephony minutes** (PSTN in/out) with per-destination rates.
- **API calls** (if exposed).

## 4. Usage Metering Pipeline

```
Media agents ──emit usage events──► Message Bus (Kafka/NATS)
                                        │
                                        ▼
                             Usage Aggregator (per tenant/project/device)
                                        │
                                        ▼
                             PostgreSQL (usage_records, hourly/daily rollups)
                                        │
                                        ▼
                          Billing Service ──► Invoices ──► Payment Provider
```

- Each completed bridge emits a **CDR** (see DATA-MODEL) with duration, codec,
  routes, bytes, and cost components.
- **Idempotency:** usage events carry a unique `id`; aggregator dedupes.
- **Rounding:** define per-minute rounding (e.g. 60/60 or 6-second increments).

## 5. CDR (Call Detail Record)

Key fields: `tenant_id, device_id, project_id, direction, from, to, route_id,
started_at, answered_at, ended_at, duration_sec, codec, packets, loss_pct,
jitter_ms, cost, currency, recording_url`.

CDRs power: invoices, live/historical analytics, dispute resolution, QoS reports.

## 6. Invoicing & Payments

- Integrate a payment provider (**Stripe** recommended; regional alternatives as needed).
- Support **subscriptions** (base + seats/devices) and **metered usage** (minutes).
- **Prepaid credits** and/or **postpaid invoicing** — many telephony customers prefer prepaid.
- **Dunning:** retry failed payments, notify, suspend after grace period.
- **Taxes:** use provider tax tools or a tax engine; store tax IDs per tenant.
- **Currency:** store amounts + currency; multiple currencies supported.

## 7. Spend Controls (important for telephony)

- **Balance / credit limit** per tenant.
- **Rate caps**: max concurrent calls, max cost/minute.
- **Alerts** at thresholds (e.g. 80 % of quota).
- Auto-suspend routes when balance ≤ 0 (configurable grace).

## 8. Admin Console Capabilities

- Onboard/offboard tenants; impersonate (with audit) for support.
- Manage plans, prices, taxes, credits; issue one-off credits/refunds.
- Fleet health: devices online/offline, firmware versions, region distribution.
- Global routes/DIDs, carrier health, and platform-wide QoS dashboards.
- Audit log: who changed what, when.

## 9. Billing Data Integrity

- **Append-only** usage records; never mutate a charged period.
- Immutable **invoices** (versioned snapshots).
- **Audit trail** on all billing mutations.
- Export to **CSV/PDF**; webhook invoice events to tenants.
