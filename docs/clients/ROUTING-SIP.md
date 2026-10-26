# Routing to SIP / PBX / VPS

A **Route** decides where a device's voice goes. Routes are per project, so you
can point one device at your PBX and another at a phone number.

## Concepts

```
Device ---> Project ---> Route ---> Destination
                          |             |
                    (match rules)   sip: | pstn: | webhook: | webrtc:
```

| Field | Example | Meaning |
| --- | --- | --- |
| `device_id` | `dev_123` | Which device |
| `prefix` | `+44` | Optional number-prefix match |
| `priority` | `10` | Lower wins |
| `action` | `route` | route / reject / voicemail |

## 1. Route to your PBX (SIP)

Ask whoever runs your PBX for:

- **SIP URI / host** (e.g. `pbx.acme.com`) and port (usually 5060, or 5061 TLS)
- if **registration** is required, or if they will **IP-allowlist** us
- the **dialled destination** format (extension or DID)
- allowed **codecs** (Opus, G.722, PCMU, PCMA)

Then in the dashboard: **Routes -> New route**

```
Destination type : SIP
SIP URI          : sip:reception@pbx.acme.com:5061;transport=tls
Register         : yes
Username/Secret  : <from your PBX admin>
Codecs           : opus, G.722, PCMU
DTMF             : RFC4733 (out-of-band)
```

Save, assign to the device/project, then place a test call. See
[PBX-SBC-EXAMPLES.md](PBX-SBC-EXAMPLES.md) for concrete PBX snippets.

> Exact URI/option syntax (`transport`, `register`, `srtp`, `mode`, …) and the API
> live in [../design/VOICE-FORWARDING.md](../design/VOICE-FORWARDING.md). To verify a
> PBX before a live call: `python scripts/pbx-smoke.py --type sip --uri 'sips:…' …`.

## 2. Route to a phone number (PSTN)

If your plan includes PSTN termination:

```
Destination type : PSTN
Number           : +15551234567
```

## 3. Route to your own VPS (webhook / SIP)

Two options:

- **SIP to your VPS:** treat your VPS as a PBX (above). Point it at our gateway.
- **Webhook (metadata/control):** receive call and DTMF events:

```
Destination type : Webhook
URL              : https://vps.acme.com/voicebridge/events
Secret           : <shared HMAC secret>
```

We POST signed JSON (call start/end, CLI, DTMF digits, QoS). Return `200` to ack.

## 4. Multiple devices, one account

- Devices -> Projects -> Routes is a many-to-many model.
- A tenant can own **any number of devices**; each device is independently routed.
- Use **Projects** to group devices by site (e.g. "London", "Reception").

## 5. Failover

Add a second route with a higher `priority` number as a fallback. If the primary
destination is unreachable, the bridge tries the next route automatically.

## 6. DTMF note

Out-of-band DTMF (RFC 4733) is preferred and reliable for SIP. In-band DTMF
works for legacy IVRs. **WhatsApp calls cannot guarantee DTMF** — see the help
topic **Calls** and the DTMF design doc.
