# Billing

Voice Bridge bills per **tenant**, scoped by device, project and call minutes.

## Concepts

| Term | Meaning |
| --- | --- |
| **Tenant** | Your organisation account |
| **Plan** | `starter`, `growth`, `scale` — sets included devices and rate |
| **Balance** | Prepaid credit in minor units (cents) |
| **CDR** | Call detail record — one row per completed call |

## How usage is counted

1. A call is bridged through a device.
2. On hang-up the CDR is written with duration, source and route.
3. Usage is priced by the project's rate card and deducted from the balance.

## Viewing billing

The dashboard header shows your tenant and plan. Call counts and durations are
available in **Calls** (history). The tenant object from `GET /api/tenant`
exposes `plan` and `balance_cents` for your own billing screens.

## Plans (example)

| Plan | Included devices | Notes |
| --- | --- | --- |
| **starter** | 3 | Email support |
| **growth** | 25 | Priority routing, alerts |
| **scale** | 200+ | SIP/VPS routing, SLA |

## Top-ups

A top-up increases `balance_cents`. When the balance reaches zero, non-emergency
routing is paused until topped up. Emergency/SIP-passthrough behaviour is
configurable in your deployment.

> Exact rate cards are per deployment. See the billing & multi-tenancy design
> document for the full model.
