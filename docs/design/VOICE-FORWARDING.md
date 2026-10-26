# Voice Forwarding (egress)

> **Status: implemented.** This is the "how does the voice get out of the
> platform" half of the media plane — the destination types and adapter pattern
> described in [ROUTING-INTEGRATIONS.md](ROUTING-INTEGRATIONS.md) (§2 types,
> §6 adapter, §7 webhook/VPS) and the L2/L4 layers of
> [ARCHITECTURE.md](ARCHITECTURE.md) (§3, §4.3, §4.4). The ingest half is
> [DEVICE-PROTOCOL.md](DEVICE-PROTOCOL.md).
>
> Where MASTER-ARCHITECTURE names FreeSWITCH/Kamailio as the production media
> engine, **this repository implements the same seams natively in Python** so the
> platform is runnable and testable end-to-end today. A FreeSWITCH-backed leg
> would slot into the same `MediaLeg` interface without touching the ingest edge.

## 1. Where this sits

```
 ESP32 ──WS(control+audio)──►  Ingest edge ──►  forwarding.resolve(device)
                               /v1/device/ws        │  (override → route → default)
                                                    ▼
                                       egress leg ──►  SIP trunk / PBX      (RTP / SRTP, G.711)
                                                    ├─►  VPS socket          (UDP / TCP)
                                                    ├─►  webhook / VPS HTTP  (chunked) or WS
                                                    └─►  recording (WAV) / simulated
```

| Layer (ARCHITECTURE.md) | Component here | File |
| --- | --- | --- |
| L1 Transport (device↔cloud) | device WebSocket ingest edge | `api/routers/device_ws.py`, `api/protocol.py` |
| L2 Media Plane | forwarding plane + egress legs | `api/services/forwarding.py`, `api/services/egress.py` |
| L2 Media Plane (codecs/crypto) | RTP / SRTP / G.711 / SDP primitives | `api/services/rtp.py`, `api/services/sdp.py` |
| L3 Control Plane | REST + dashboard BFF | `api/routers/forwarding.py`, `api/routers/ui.py` |

The ingest edge never knows the transport: at `call.answered` it asks the
forwarding plane to open a leg, streams device audio into it, and closes it at
`call.ended` (or on disconnect).

## 2. Choosing a destination

Resolved **first hit wins**:

| # | Source | Configure via |
| --- | --- | --- |
| 1 | **Per-device override** | Dashboard → *Manage device* → **Voice forwarding**, or `PUT /v1/ui/devices/{id}/forwarding` / `PUT /v1/forwarding/devices/{id}` |
| 2 | **Route** | `routes` table (existing `/v1/routes` API); matched best-first, skipping types with no leg yet |
| 3 | **`simulated`** | Default when nothing matches — counts frames, sends nothing |

## 3. Destination grammar

Two ways to pass options: in the URI (`;k=v` for SIP, `?k=v` for socket/webhook)
or in the `options` JSON object. **`options` overrides URI params.**

| `dest_type` | Sample URI | Transports | Options |
| --- | --- | --- | --- |
| `sip` | `sip:1000@pbx.example:5060;transport=udp` | `udp` `tcp` `tls` | `transport`, `register`, `password`, `realm`, `srtp`, `tls_verify` |
| `pstn` | `tel:+15551234?carrier_host=sbc.carrier.example:5060` | `udp` `tcp` `tls` | `carrier_host`, `carrier_port`, `transport`, `country`, `register`, `password`, `realm`, `srtp`, `tls_verify` |
| `socket` | `udp://vps.example:9999?mode=rtp` | `udp` `tcp` | `mode=rtp\|raw`, `codec=pcmu\|pcm16`, `srtp`, `key` |
| `webhook` | `https://vps.example/audio?mode=chunked` | `http` `https` `ws` `wss` | `mode=chunked\|ws`, `signing_secret`, `headers`, `tls_verify` |
| `recording` | `file:///var/lib/voicebridge/call.wav` | — | — |
| `simulated` | `simulated` | — | — |

`GET /v1/forwarding/capabilities` returns this table machine-readably (the
dashboard and docs derive from it).

The `pstn` destination dials an E.164 number through a carrier SBC; the number is
normalised to E.164 and the carrier comes from `carrier_host` (or the
`VB_PSTN_CARRIER` env default). See [PSTN-AND-DIDS.md](PSTN-AND-DIDS.md).

## 4. The legs

| Leg | Class | Sends |
| --- | --- | --- |
| SIP trunk | `SipTrunkLeg` | INVITE(SDP)→[REGISTER]→ACK→RTP/SRTP→BYE over UDP/TCP/**TLS** |
| UDP VPS socket | `UdpSocketLeg` | one datagram per 20 ms frame (raw PCM16, RTP/G.711, or SRTP) |
| TCP VPS socket | `TcpSocketLeg` | 2-byte length-prefixed frames (raw or RTP) |
| Webhook / VPS | `WebhookLeg` | **HTTP chunked POST** (optional HMAC) or **WebSocket** binary frames |
| Recording | `RecordingLeg` | 16 kHz mono PCM16 WAV |
| Simulated | `SimulatedLeg` | counts frames/bytes/peak dBFS |

All implement `MediaLeg` (`start / write / stop / stats`) — the documented
adapter seam.

## 5. SIP details

* **Transports:** `udp` (signaling+media on UDP), `tcp`, and `tls` (SIPS, default
  port 5061; signaling over the TLS stream, media stays on UDP). `tls_verify=0`
  disables certificate checks (lab use).
* **REGISTER:** set `register=1` (+ `password`, optional `realm`). Handles the
  `401/407` challenge with **Digest** auth (MD5, with `qop` when offered) before
  INVITE.
* **Media:** PCMU/8000 (`RTP/AVP 0`); SDP offer advertises our RTP port.
* **SRTP:** `srtp=1` offers `RTP/SAVP` with an **SDES** `a=crypto` inline key
  (`AES_CM_128_HMAC_SHA1_80`) and encrypts/authenticates RTP (RFC 3711). The
  key-derivation function is validated against the RFC 3711 Appendix B.3 vector
  in `tests/test_rtp.py`.
* **Not implemented yet:** TLS **certificate auth**/mutual TLS, SRTP with
  **DTLS-SRTP**, re-INVITE/PRACK/100rel, `REGISTER` refresh, codecs other than
  PCMU, DTMF (RFC 4733 / INFO). See §9.

## 6. Socket & webhook details

* **`mode=rtp`** wraps each 20 ms frame in an RTP packet; **`raw`** (default)
  sends the payload directly. `codec=pcmu` transcodes 16k→8k G.711; `codec=pcm16`
  forwards the 16 kHz PCM16 untouched.
* **Socket SRTP:** `srtp=1` requires the shared `key` = base64 of a 30-byte
  master key+salt (`AES_CM_128_HMAC_SHA1_80`), since there is no SDP to carry it.
* **Webhook `mode=chunked`:** `POST` with `Transfer-Encoding: chunked`; each
  audio frame is one chunk; `Content-Type: application/octet-stream` +
  `X-Audio-Format: pcm16le/16000/mono`. With `signing_secret`, each chunk carries
  `;sig=<hmac-sha256(frame)>` as a chunk extension.
* **Webhook `mode=ws`:** masked binary WebSocket frames (RFC 6455), a compact
  built-in client (no third-party dependency at runtime).

## 7. Audio conversion

Device audio is 16 kHz PCM16. Telephony legs use `downsample_16k_to_8k` (2:1
decimation) → `pcmu_encode` (µ-law). Raw socket/webhook legs forward 16 kHz
PCM16 untouched, documented so the receiver can decode. A proper resampler and
Opus transcode are Phase-2 work (`AUDIO-PIPELINE.md`).

## 8. Failure handling

* **Unreachable destination at call time** (DNS/refused/timeout) → the leg is
  degraded to `simulated` and the session's `route` is marked `…:unreachable`;
  the call still runs (ROUTING-INTEGRATIONS §8). An unreachable leg must never
  drop a call.
* **Destination type with no leg yet** is skipped so a later wired route can win.
* **Invalid destination cannot be stored** — it is validated on write (422).

## 9. API reference

```
GET    /v1/forwarding/capabilities         # supported types/transports/options (public)

GET    /v1/ui/devices/{id}/forwarding      # resolved {destination, source, override}
PUT    /v1/ui/devices/{id}/forwarding      # {dest_type, dest_uri, options}
DELETE /v1/ui/devices/{id}/forwarding      # clear override

GET    /v1/forwarding/devices/{id}         # same, API-key/JWT auth
PUT    /v1/forwarding/devices/{id}
DELETE /v1/forwarding/devices/{id}
POST   /v1/forwarding/validate             # {dest_type, dest_uri, options} -> {ok, destination?}
```

Example — pin *Reception Desk* to a PBX with REGISTER + SRTP:

```sh
curl -X PUT https://host/v1/forwarding/devices/GBX-A1B2C3 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"dest_type":"sip","dest_uri":"sip:1000@pbx.example:5060;transport=tls;register=1",
       "options":{"password":"s3cret","srtp":1}}'
```

## 10. Verifying a real destination

`scripts/pbx-smoke.py` drives a real PBX/VPS with a 440 Hz tone and prints stats:

```sh
python scripts/pbx-smoke.py --type sip --uri sips:1000@pbx.example \
    --option register=1 --option password=s3cret --option srtp=1 --seconds 5
python scripts/pbx-smoke.py --type socket --uri 'udp://vps.example:9999?mode=rtp'
python scripts/pbx-smoke.py --type webhook --uri https://vps.example/audio --option mode=ws
python scripts/pbx-smoke.py --type socket --uri udp://vps:9999 --dry-run   # parse only
```

The automated suite exercises every leg against local stubs (UDP/TCP/TLS SIP,
REGISTER+digest, SRTP, HTTP chunked, WebSocket) in `tests/test_egress.py`.

## 11. Adding a new leg

1. Add a class in `api/services/egress.py` implementing `MediaLeg`
   (`start/write/stop/stats`) — copy `WebhookLeg` as a template.
2. Handle it in `egress.build_leg()` and add parsing in `egress.parse_destination()`.
3. Add the type to `egress.capabilities()` (the API + docs pick it up).
4. Reuse `_RtpMixin` if it emits RTP/SRTP.
5. Test it in `tests/test_egress.py` against a local stub.

That is the whole change — the ingest edge and dashboard need no edits.

## 12. Security notes & next increments

SIP-over-UDP and raw socket legs are **plaintext**; HMAC-signed webhook chunks are
integrity-only (no encryption). Before production: SIPS + SRTP everywhere (SDES
done; DTLS-SRTP + mutual TLS pending), per-route egress **allow-listing**, and
secret storage in the platform keystore rather than route rows
([SECURITY.md](SECURITY.md)). Next: PSTN carrier trunk, `webrtc` SFU leg,
failover chains (`fallback`), and recording → object store with retention.

## 13. Files & tests

| Concern | File |
| --- | --- |
| Destination parsing + legs | `api/services/egress.py` |
| RTP / SRTP / G.711 | `api/services/rtp.py` |
| SDP offer/answer | `api/services/sdp.py` |
| Resolution + registry | `api/services/forwarding.py` |
| Endpoints | `api/routers/forwarding.py`, `api/routers/ui.py` |
| Dashboard panel | `api/web/app.js` → *Manage device* → Voice forwarding |
| Smoke test | `scripts/pbx-smoke.py` |
| Tests | `tests/test_rtp.py`, `tests/test_egress.py`, `tests/integration/test_forwarding.py` |
| Schema | `device_forwarding` (per-device override) |
