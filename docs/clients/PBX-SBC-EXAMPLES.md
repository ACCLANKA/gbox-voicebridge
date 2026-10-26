# PBX / SBC Configuration Examples

Copy-paste starting points. Replace `voicebridge.example.com` (our SIP gateway),
`acme`, and credentials with your own values.

## Generic SIP trunk (most PBXs)

```
Host / SIP server : voicebridge.example.com
Port              : 5061 (TLS)  or 5060 (UDP/TCP)
Transport         : TLS preferred, UDP fallback
Auth              : username + secret (or IP allowlist)
Codecs (priority): opus, G.722, PCMU, PCMA
DTMF              : RFC 4733 (out-of-band)
NAT               : send keepalives; register every 120 s
```

## Asterisk / FreePBX (`pjsip`)

`/etc/asterisk/pjsip.conf`, then reload:

```ini
[voicebridge]
type=endpoint
context=from-voicebridge
disallow=all
allow=opus
allow=g722
allow=ulaw
dtmf_mode=rfc4733
direct_media=no

[voicebridge]
type=aor
contact=sip:voicebridge.example.com:5061\;transport=tls
qualify_frequency=60

[voicebridge]
type=auth
auth_type=userpass
username=acme
password=CHANGE_ME

[voicebridge]
type=identify
endpoint=voicebridge
match=voicebridge.example.com
```

Dialplan (`extensions.conf`):

```ini
[from-voicebridge]
exten => _X.,1,NoOp(Call from Voice Bridge, CLI=${CALLERID(num)})
 same => n,Dial(PJSIP/1001,30)
 same => n,Hangup()
```

## Cisco CUCM / CUBE (SBC)

```
SIP Trunk:
  Destination address : voicebridge.example.com
  Port                : 5061
  Transport           : TLS
  DTMF signaling      : RFC 2833
  Codec               : Opus (pass-through), G.722, G.711
  SIP profile          : Standard (or Early Offer if your CUBE needs it)
```

CUBE dial-peer:

```
dial-peer voice 100 voip
 description Voice Bridge trunk
 session protocol sipv2
 session target ipv4:voicebridge.example.com:5061
 session transport tcp tls
 codec profile 1
 dtmf-relay rtp-nte
 no vad
!
voice class codec 1
 codec preference 1 opus
 codec preference 2 g722
 codec preference 3 g711ulaw
```

## 3CX

```
Trunk type        : SIP Trunk (provider: generic)
Registrar/host    : voicebridge.example.com
Authentication    : username + password (or IP-based, no auth)
Codecs            : Opus, G.722, G.711
DTMF              : RFC 2833
Inbound rules     : map DIDs to extensions / ring groups
```

## Kamailio / OpenSIPS (SBC)

```
# minimal routing sketch
if (is_method("INVITE")) {
    $du = "sip:voicebridge.example.com:5061;transport=tls";
    t_relay();
}
# force out-of-band DTMF
modparam("rtpengine", "dtmf_relay", 1)
```

## Firewall summary (your side)

| Direction | Proto | Port | Purpose |
| --- | --- | --- | --- |
| In / Out | UDP | 5060 | SIP (plain) |
| In / Out | TCP/TLS | 5061 | SIP over TLS |
| Out | UDP | 16384-32767 | RTP media (our offered range) |
| Out | TCP | 443 | Control/MQTT/OTA |

## Test checklist

1. Place an inbound test call; confirm it reaches the PBX.
2. Press digits on the caller side; confirm the IVR reacts (DTMF path).
3. Check audio both ways (MOS ≥ 4.0 in dashboard).
4. Confirm CLI (caller number) arrives in your CDR.
