# Security

Voice is sensitive; the platform is multi-tenant. Treat every layer as hostile.

## 1. Identity & Access

- **Users:** email + password (argon2id), optional **SSO (OIDC/SAML)** for enterprise, **MFA** (TOTP) encouraged, enforced for admins.
- **Sessions:** short-lived JWTs + refresh tokens; revoke on password change.
- **RBAC:** roles per [BILLING-MULTITENANCY.md](BILLING-MULTITENANCY.md#2-roles-rbac).
- **API keys / service accounts:** scoped, hashed at rest, rotatable, last-used tracked.
- **Admin actions:** impersonation requires reason + is fully audited.

## 2. Device Trust

- **Per-device X.509 certificate** issued at provisioning; private key in encrypted NVS / secure element.
- **mTLS** for control channel; certificate rotation supported.
- Device **claim tokens** are single-use and short-lived.
- **Secure Boot + Flash Encryption** on production units.
- Signed firmware; A/B partitions with rollback.
- Lost/compromised device ⇒ revoke cert; re-provision required.

## 3. Media Security

- **SRTP** (AES-CM/128) for all RTP legs; keys via **DTLS-SRTP** or SDES over TLS.
  *Implemented:* SDES inline keys (`AES_CM_128_HMAC_SHA1_80`) on SIP and socket
  legs (`api/services/rtp.py`, RFC 3711 KDF vector-tested); DTLS-SRTP pending.
- **SIPS/TLS** for SIP signaling where the PBX supports it.
  *Implemented:* `transport=tls` on the SIP leg; **mTLS/cert pinning pending**.
- **WebRTC:** DTLS-SRTP end-to-end between browser and SFU; SFU-to-agent encrypted.
- No plaintext RTP over untrusted networks.
- Media servers in private subnets; only ingest edges are internet-facing.

## 4. Data Security

- **TLS 1.2+** everywhere; HSTS on web.
- Encryption at rest: disk/volume encryption + **application-level encryption for recordings**.
- Recordings: encrypt with per-object keys (KMS-backed); signed, short-lived download URLs.
- Secrets in a **secrets manager** (Vault/KMS/cloud SM), never in code/images.
- PII minimization: store only what billing/support requires; define data map.

## 5. Tenant Isolation

- `tenant_id` enforced at API layer **and** DB (RLS or schema-per-tenant for large accounts).
- No cross-tenant queries in application code; enforced by repository layer.
- Per-tenant rate limits and quotas.
- Separate object-store prefixes/buckets and encryption keys per tenant (defense in depth).

## 6. Network & Platform

- WAF + DDoS protection at the edge; TLS termination at LB.
- Private networking between services; service mesh mTLS optional.
- Least-privilege IAM for all workloads; no long-lived cloud keys on nodes.
- Egress allowlists for carrier/BSP/WhatsApp endpoints.
- **Fraud controls:** toll-fraud detection (sudden destination/cost spikes), call rate caps, geo restrictions.

## 7. Telephony-Specific Threats

| Threat | Mitigation |
| --- | --- |
| SIP scanning / registration brute force | Kamailio ACLs, fail2ban, rate limits, strong creds |
| Toll fraud | spend caps, anomaly alerts, allow/deny lists, PIN on sensitive routes |
| Spoofed caller ID | validate DIDs; document trust model |
| Recording interception | app-level encryption + access control |
| SIP flooding/DoS | edge rate limiting, blacklists, concurrency caps |

## 8. Application Security

- OWASP Top 10 baseline; SAST/DAST/dependency scanning in CI.
- Input validation on all schemas (route match patterns, numbers, URIs).
- Parameterized queries only; no string-built SQL.
- CSRF protection on state-changing web endpoints; secure cookie flags.
- Content Security Policy on the dashboard.

## 9. Compliance & Privacy

- **GDPR/CCPA** readiness: DSR (access/delete), consent, retention controls.
- **Call recording consent** varies by jurisdiction — surface a consent flag/prompt per route.
- **Number portability & emergency call (911/112)** handling is regulated — must be explicitly scoped.
- **PCI:** never store card data; use tokenized provider integration.
- SOC 2 / ISO 27001 roadmap for enterprise sales.

## 10. Incident Response

- Centralized audit/security logs with alerting.
- Documented runbooks: device cert revocation, key rotation, tenant suspension, breach playbook.
- Backups encrypted + restore tested.
