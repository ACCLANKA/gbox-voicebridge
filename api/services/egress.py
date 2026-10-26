"""Egress plane: forward a device's call audio to a downstream destination.

Implements the adapter pattern from ``docs/design/ROUTING-INTEGRATIONS.md`` §6
and the destination types in §2. Every leg implements ``media.MediaLeg`` so the
ingest edge can bridge device audio without knowing the transport.

Grammar + option matrix: ``docs/design/VOICE-FORWARDING.md``. Summary:

* ``sip:user@host:port;transport=udp|tcp|tls;register=1;srtp=1`` → SIP + RTP/SRTP
* ``udp://host:port?mode=rtp&codec=pcmu|pcm16&srtp=1&key=<b64>`` → UDP datagrams
* ``tcp://host:port?mode=rtp|raw``                          → length-prefixed TCP
* ``http(s)://host/path``  (``mode=chunked|ws``)            → webhook / VPS stream
* ``tel:+E164``  (carrier via ``carrier_host``/``VB_PSTN_CARRIER``) → PSTN call
* ``file:///path/call.wav``                                 → recording sink
* ``simulated``                                             → no-op (default)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import os
import random
import re
import socket
import ssl
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import config, protocol
from . import numbers, rtp, sdp
from .media import MediaLeg, SimulatedLeg

SIP_TIMEOUT_SEC = 2.0
CONNECT_TIMEOUT_SEC = 3.0
MASTER_KEY_LEN = 16
MASTER_SALT_LEN = 14

# Re-export primitives other modules/tests rely on.
rtp_packet = rtp.rtp_packet
pcmu_encode = rtp.pcmu_encode
downsample_16k_to_8k = rtp.downsample_16k_to_8k


class EgressError(ValueError):
    """A destination could not be parsed or opened."""


# --------------------------------------------------------------------------- #
# Destination model + parsing
# --------------------------------------------------------------------------- #
@dataclass
class EgressTarget:
    kind: str                                  # sip | socket | webhook | recording | simulated
    uri: str
    host: str | None = None
    port: int | None = None
    transport: str = "udp"                     # sip/socket: udp | tcp | tls
    user: str | None = None
    mode: str = "raw"                          # socket: rtp|raw ; webhook: chunked|ws
    codec: str = "pcmu"
    path: str | None = None
    srtp: bool = False
    register: bool = False
    password: str | None = None
    realm: str | None = None
    srtp_key: bytes | None = None
    signing_secret: str | None = None
    headers: dict = field(default_factory=dict)
    tls_verify: bool = True
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d: dict = {"type": self.kind, "uri": self.uri}
        if self.host:
            d.update(host=self.host, port=self.port, transport=self.transport)
        if self.kind == "socket":
            d["mode"] = self.mode
            d["codec"] = self.codec
        if self.kind == "pstn":
            d["number"] = self.extra.get("number")
            d["carrier"] = f"{self.host}:{self.port}"
        if self.kind in ("sip", "socket", "pstn") and self.srtp:
            d["srtp"] = True
        if self.kind in ("sip", "pstn") and self.register:
            d["register"] = True
        if self.kind == "webhook":
            d["mode"] = self.mode
            d["signed"] = bool(self.signing_secret)
        if self.path:
            d["path"] = self.path
        return d


def _truthy(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


def _merge(options: dict, params: dict) -> dict:
    """URI query params first; explicit ``options_json`` values win."""
    return {**params, **{k: v for k, v in options.items() if v is not None}}


def parse_destination(dest_type: str | None, dest_uri: str | None,
                      options: dict | None = None) -> EgressTarget:
    """Parse a route/override destination into an :class:`EgressTarget`."""
    dest_type = (dest_type or "").lower()
    uri = (dest_uri or "").strip()
    opts = dict(options or {})

    if dest_type in ("", "simulated", "none"):
        return EgressTarget("simulated", uri or "simulated", extra=opts)

    if dest_type == "recording":
        path = uri[len("file://"):] if uri.startswith("file://") else uri
        if not path:
            raise EgressError("recording destination needs a file:// path")
        return EgressTarget("recording", uri, path=path, extra=opts)

    if dest_type == "socket":
        parts = urlsplit(uri)
        if parts.scheme not in ("udp", "tcp") or not parts.hostname or not parts.port:
            raise EgressError(f"invalid socket uri {uri!r} (want udp://host:port)")
        o = _merge(opts, {k: v[0] for k, v in parse_qs(parts.query).items()})
        srtp = _truthy(o.get("srtp", "0"))
        srtp_key = base64.b64decode(o["key"]) if o.get("key") else None
        if srtp_key is not None and len(srtp_key) != MASTER_KEY_LEN + MASTER_SALT_LEN:
            raise EgressError("srtp key must be a base64 30-byte master key+salt")
        if srtp and srtp_key is None:
            raise EgressError("srtp=1 on a socket leg requires key=<b64 30-byte>")
        return EgressTarget("socket", uri, host=parts.hostname, port=parts.port,
                            transport=parts.scheme, mode=o.get("mode", "raw"),
                            codec=o.get("codec", "pcmu"), srtp=srtp, srtp_key=srtp_key, extra=o)

    if dest_type == "sip":
        # Strict about the parts that decide whether this can ever connect: no whitespace anywhere, a
        # real hostname (or a bracketed IPv6 literal), and a port inside 1-65535. The previous pattern
        # accepted "sip:this is not a uri" as a hostname, so the console's Test button called a typo valid.
        m = re.match(
            r"^sips?:(?:([^@\s;:?]+)@)?"
            r"([A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])"
            r"(?::(\d{1,5}))?(?:[;?](.*))?$", uri)
        if not m:
            raise EgressError(f"invalid sip uri {uri!r} (want sip:user@host:port)")
        user, host, port, params = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        if port and not (0 < int(port) < 65536):
            raise EgressError(f"invalid sip port {port} in {uri!r}")
        o = _merge(opts, {k: v[0] for k, v in parse_qs(params.replace(";", "&")).items()})
        transport = o.get("transport", "tls" if uri.startswith("sips:") else "udp")
        if transport not in ("udp", "tcp", "tls"):
            raise EgressError(f"unsupported sip transport {transport!r}")
        return EgressTarget("sip", uri, host=host,
                            port=int(port or (5061 if transport == "tls" else 5060)),
                            transport=transport, user=user,
                            register=_truthy(o.get("register", "0")),
                            password=o.get("password"), realm=o.get("realm"),
                            srtp=_truthy(o.get("srtp", "0")),
                            tls_verify=_truthy(o.get("tls_verify", "1")), extra=o)

    if dest_type == "pstn":
        raw = uri[len("tel:"):] if uri.startswith("tel:") else uri
        o = dict(opts)
        if "?" in raw:
            raw, _, q = raw.partition("?")
            o = _merge(o, {k: v[0] for k, v in parse_qs(q).items()})
        number = numbers.try_normalize(raw, o.get("country") or config.PSTN_DEFAULT_COUNTRY)
        if not number:
            raise EgressError(f"invalid pstn number {uri!r}")
        carrier = o.get("carrier_host") or o.get("host") or config.PSTN_CARRIER
        if not carrier:
            raise EgressError("no PSTN carrier configured (set carrier_host or VB_PSTN_CARRIER)")
        chost, _, cport = carrier.partition(":")
        transport = o.get("transport") or config.PSTN_TRANSPORT
        if transport not in ("udp", "tcp", "tls"):
            raise EgressError(f"unsupported pstn transport {transport!r}")
        port = int(o.get("carrier_port") or cport or (5061 if transport == "tls" else 5060))
        return EgressTarget("pstn", uri, host=chost, port=port, transport=transport,
                            user=numbers.dial_user(number),
                            register=_truthy(o.get("register", "0")),
                            password=o.get("password"), realm=o.get("realm"),
                            srtp=_truthy(o.get("srtp", "0")),
                            tls_verify=_truthy(o.get("tls_verify", "1")),
                            extra={**o, "number": number})

    if dest_type in ("webhook", "vps"):
        parts = urlsplit(uri)
        if parts.scheme not in ("http", "https", "ws", "wss") or not parts.hostname:
            raise EgressError(f"invalid webhook uri {uri!r} (want https://host/path)")
        o = _merge(opts, {k: v[0] for k, v in parse_qs(parts.query).items()})
        secure = parts.scheme in ("https", "wss")
        mode = o.get("mode", "ws" if parts.scheme in ("ws", "wss") else "chunked")
        headers = o.get("headers") if isinstance(o.get("headers"), dict) else {}
        return EgressTarget("webhook", uri, host=parts.hostname,
                            port=parts.port or (443 if secure else 80),
                            transport=parts.scheme, mode=mode,
                            signing_secret=o.get("signing_secret"), headers=headers,
                            tls_verify=_truthy(o.get("tls_verify", "1")), extra=o)

    raise EgressError(f"destination type {dest_type!r} has no egress leg yet")


# --------------------------------------------------------------------------- #
# RTP-emitting legs
# --------------------------------------------------------------------------- #
class _RtpMixin:
    """RTP seq/timestamp/SSRC state (+ optional SRTP session keys)."""

    _srtp_session: tuple[bytes, bytes, bytes] | None = None
    _codec = "pcmu"

    def _init_rtp(self, *, srtp_key: bytes | None = None, codec: str = "pcmu") -> None:
        self._seq = random.randint(0, 60000)
        self._ts = 0
        self._ssrc = random.randint(1, 2 ** 31 - 1)
        self._roc = 0
        self._codec = codec
        self._srtp_session = None
        if srtp_key:
            self._srtp_session = rtp.session_keys(srtp_key[:MASTER_KEY_LEN],
                                                  srtp_key[MASTER_KEY_LEN:])

    def _frame_payload(self, payload: bytes) -> bytes:
        if self._codec == "pcm16":
            return payload
        return rtp.pcmu_encode(rtp.downsample_16k_to_8k(payload))

    def _next_rtp(self, payload: bytes) -> bytes:
        pkt = rtp.rtp_packet(self._frame_payload(payload), seq=self._seq,
                             timestamp=self._ts, ssrc=self._ssrc)
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + rtp.SAMPLES_PER_FRAME_8K) & 0xFFFFFFFF
        if self._srtp_session is not None:
            pkt = rtp.srtp_protect(pkt, session=self._srtp_session, roc=self._roc)
        return pkt


class UdpSocketLeg(_RtpMixin, MediaLeg):
    """Forward audio to a VPS over UDP (raw PCM16, RTP/G.711, or SRTP)."""

    def __init__(self, session_id: str, host: str, port: int, mode: str = "raw",
                 *, codec: str = "pcmu", srtp_key: bytes | None = None) -> None:
        self.session_id, self.host, self.port, self.mode = session_id, host, port, mode
        self.frames = self.bytes = 0
        self.active = False
        self._sock: socket.socket | None = None
        self._init_rtp(srtp_key=srtp_key, codec=codec)

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.active = True

    def write(self, payload: bytes) -> None:
        if self._sock is None:
            return
        data = self._next_rtp(payload) if self.mode == "rtp" else payload
        self._sock.sendto(data, (self.host, self.port))
        self.frames += 1
        self.bytes += len(data)

    def stats(self) -> dict:
        return {"leg": "udp", "session_id": self.session_id, "peer": f"{self.host}:{self.port}",
                "mode": self.mode, "srtp": bool(self._srtp_session), "active": self.active,
                "frames": self.frames, "bytes": self.bytes}

    def stop(self) -> dict:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self.active = False
        return self.stats()


class TcpSocketLeg(_RtpMixin, MediaLeg):
    """Forward audio to a VPS over TCP, 2-byte length-prefixed frames."""

    def __init__(self, session_id: str, host: str, port: int, mode: str = "raw",
                 *, codec: str = "pcmu") -> None:
        self.session_id, self.host, self.port, self.mode = session_id, host, port, mode
        self.frames = self.bytes = 0
        self.active = False
        self._sock: socket.socket | None = None
        self._init_rtp(codec=codec)

    def start(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_SEC)
        sock.settimeout(None)
        self._sock = sock
        self.active = True

    def write(self, payload: bytes) -> None:
        if self._sock is None:
            return
        data = self._next_rtp(payload) if self.mode == "rtp" else payload
        self._sock.sendall(struct.pack("!H", len(data)) + data)
        self.frames += 1
        self.bytes += len(data)

    def stats(self) -> dict:
        return {"leg": "tcp", "session_id": self.session_id, "peer": f"{self.host}:{self.port}",
                "mode": self.mode, "active": self.active, "frames": self.frames, "bytes": self.bytes}

    def stop(self) -> dict:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None
        self.active = False
        return self.stats()


class RecordingLeg(MediaLeg):
    """Capture the device audio to a 16 kHz mono PCM16 WAV file."""

    def __init__(self, session_id: str, path: str) -> None:
        self.session_id, self.path = session_id, path
        self.frames = self.bytes = 0
        self.active = False
        self._wav: wave.Wave_write | None = None

    def start(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(self.path, "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(protocol.SAMPLE_RATE)
        self.active = True

    def write(self, payload: bytes) -> None:
        if self._wav is None:
            return
        self._wav.writeframes(payload)
        self.frames += 1
        self.bytes += len(payload)

    def stats(self) -> dict:
        return {"leg": "recording", "session_id": self.session_id, "path": self.path,
                "active": self.active, "frames": self.frames, "bytes": self.bytes}

    def stop(self) -> dict:
        if self._wav is not None:
            self._wav.close()
            self._wav = None
        self.active = False
        return self.stats()


# --------------------------------------------------------------------------- #
# SIP signaling transport (UDP datagram, or TCP/TLS stream)
# --------------------------------------------------------------------------- #
class _SipSignaling:
    def __init__(self, host: str, port: int, transport: str, *, tls_verify: bool = True) -> None:
        self.host, self.port, self.transport = host, port, transport
        self.tls_verify = tls_verify
        self.local_ip = "127.0.0.1"
        self.local_port = 0
        self._sock: socket.socket | None = None
        self._buf = b""

    @property
    def via_token(self) -> str:
        return {"udp": "UDP", "tcp": "TCP", "tls": "TLS"}[self.transport]

    def open(self) -> None:
        if self.transport == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("", 0))
            sock.settimeout(SIP_TIMEOUT_SEC)
        else:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_SEC)
            if self.transport == "tls":
                ctx = ssl.create_default_context()
                if not self.tls_verify:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname=self.host)
            sock.settimeout(SIP_TIMEOUT_SEC)
        self.local_ip, self.local_port = sock.getsockname()[:2]
        self._sock = sock

    def send(self, msg: bytes) -> None:
        assert self._sock is not None
        if self.transport == "udp":
            self._sock.sendto(msg, (self.host, self.port))
        else:
            self._sock.sendall(msg)

    def recv(self) -> str | None:
        if self._sock is None:
            return None
        if self.transport == "udp":
            try:
                data, _ = self._sock.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                return None
            return data.decode("utf-8", "replace")
        try:
            while True:
                msg = self._take_stream_message()
                if msg is not None:
                    return msg
                chunk = self._sock.recv(4096)
                if not chunk:
                    return None
                self._buf += chunk
        except (socket.timeout, TimeoutError):
            return None

    def _take_stream_message(self) -> str | None:
        if b"\r\n\r\n" not in self._buf:
            return None
        head, rest = self._buf.split(b"\r\n\r\n", 1)
        m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
        clen = int(m.group(1)) if m else 0
        if len(rest) < clen:
            return None
        self._buf = rest[clen:]
        return (head + b"\r\n\r\n" + rest[:clen]).decode("utf-8", "replace")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()  # noqa: S324 - SIP digest (RFC 3261)


class SipTrunkLeg(_RtpMixin, MediaLeg):
    """SIP UAC: optional REGISTER, INVITE (SDP), ACK, RTP/SRTP, BYE.

    UDP keeps signaling+media on separate sockets; TCP/TLS carry signaling and
    media stays on UDP (SRTP when ``srtp=1``). This is a compact but real SIP
    client — see VOICE-FORWARDING.md for the supported subset.
    """

    def __init__(self, session_id: str, host: str, port: int = 5060, user: str | None = None,
                 transport: str = "udp", *, register: bool = False, password: str | None = None,
                 realm: str | None = None, srtp: bool = False, tls_verify: bool = True) -> None:
        self.session_id, self.host, self.port = session_id, host, port
        self.user, self.transport = user, transport
        self.register, self._password, self._realm = register, password, realm
        self.srtp, self.tls_verify = srtp, tls_verify
        self.frames = self.bytes = 0
        self.active = False
        self.remote: tuple[str, int] | None = None
        self._sig = _SipSignaling(host, port, transport, tls_verify=tls_verify)
        self._media: socket.socket | None = None
        self._media_port = 0
        self._crypto: dict | None = None
        self._call_id = f"{random.randint(0, 2 ** 32):08x}@voicebridge"
        self._from_tag = f"{random.randint(0, 2 ** 32):08x}"
        self._cseq = 0
        self._init_rtp()

    # -- message helpers ---------------------------------------------------- #
    def _to_uri(self) -> str:
        return (f"sip:{self.user}@{self.host}:{self.port}" if self.user
                else f"sip:{self.host}:{self.port}")

    def _from_uri(self) -> str:
        return f"sip:{self.user or 'voicebridge'}@{self._sig.local_ip}"

    def _contact(self) -> str:
        return f"<sip:{self.user or 'voicebridge'}@{self._sig.local_ip}:{self._sig.local_port}>"

    def _request(self, method: str, uri: str, *, body: str = "", content_type: str | None = None,
                 authorization: str | None = None, extra: dict | None = None) -> bytes:
        self._cseq += 1
        lines = [f"{method} {uri} SIP/2.0",
                 f"Via: SIP/2.0/{self._sig.via_token} {self._sig.local_ip}:{self._sig.local_port}"
                 f";branch=z9hG4bK{self._from_tag}{self._cseq}",
                 "Max-Forwards: 70",
                 f"From: <{self._from_uri()}>;tag={self._from_tag}",
                 f"To: <{self._to_uri()}>",
                 f"Call-ID: {self._call_id}",
                 f"CSeq: {self._cseq} {method}",
                 f"Contact: {self._contact()}"]
        if authorization:
            lines.append(f"Authorization: {authorization}")
        if content_type:
            lines.append(f"Content-Type: {content_type}")
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        lines.append(f"Content-Length: {len(body.encode())}")
        return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()

    def _read_final(self) -> str | None:
        while True:
            msg = self._sig.recv()
            if msg is None:
                return None
            m = re.match(r"SIP/2\.0 (\d{3})", msg)
            if m and int(m.group(1)) >= 200:
                return msg

    def _authorization(self, response: str, method: str, uri: str) -> str | None:
        m = (re.search(r"WWW-Authenticate:\s*(Digest[^\r\n]*)", response, re.I)
             or re.search(r"Proxy-Authenticate:\s*(Digest[^\r\n]*)", response, re.I))
        if not m:
            return None
        pairs = re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', m.group(1))
        params = {k: (v or w) for k, v, w in pairs}
        realm = params.get("realm", self._realm or "")
        nonce = params.get("nonce", "")
        qop = params.get("qop", "")
        user, pw = self.user or "voicebridge", self._password or ""
        ha1, ha2 = _md5(f"{user}:{realm}:{pw}"), _md5(f"{method}:{uri}")
        if qop:
            qop = qop.split(",")[0].strip()
            nc, cnonce = "00000001", f"{random.randint(0, 2 ** 32):08x}"
            resp = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                    f'uri="{uri}", response="{resp}", qop={qop}, nc={nc}, cnonce="{cnonce}"')
        resp = _md5(f"{ha1}:{nonce}:{ha2}")
        return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                f'uri="{uri}", response="{resp}"')

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        self._sig.open()
        if self.srtp:
            master = os.urandom(MASTER_KEY_LEN + MASTER_SALT_LEN)
            self._srtp_session = rtp.session_keys(master[:MASTER_KEY_LEN],
                                                  master[MASTER_KEY_LEN:])
            self._crypto = {"tag": "1", "suite": rtp.SDES_AES_CM_128_HMAC_SHA1_80,
                            "key": master}
        self._media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._media.bind(("", 0))
        self._media_port = self._media.getsockname()[1]

        if self.register:
            self._do_register()
        self._do_invite()
        self.active = True

    def _do_register(self) -> None:
        uri = f"sip:{self.host}"
        headers = {"Expires": "3600"}
        self._sig.send(self._request("REGISTER", uri, extra=headers))
        resp = self._read_final()
        if resp and re.match(r"SIP/2\.0 (401|407)", resp):
            auth = self._authorization(resp, "REGISTER", uri)
            self._sig.send(self._request("REGISTER", uri, extra=headers, authorization=auth))
            resp = self._read_final()
        if not (resp and resp.startswith("SIP/2.0 200")):
            raise EgressError("SIP REGISTER was not accepted")

    def _do_invite(self) -> None:
        uri = self._to_uri()
        body = sdp.offer(address=self._sig.local_ip, port=self._media_port, crypto=self._crypto)
        self._sig.send(self._request("INVITE", uri, body=body,
                                     content_type="application/sdp"))
        resp = self._read_final()
        if resp and re.match(r"SIP/2\.0 (401|407)", resp):
            auth = self._authorization(resp, "INVITE", uri)
            self._sig.send(self._request("INVITE", uri, body=body,
                                         content_type="application/sdp", authorization=auth))
            resp = self._read_final()
        if not (resp and resp.startswith("SIP/2.0 200")):
            raise EgressError(f"SIP INVITE rejected by {self.host}:{self.port} "
                              f"({resp.splitlines()[0] if resp else 'no response'})")
        md = sdp.parse(resp.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in resp else "")
        self.remote = (md.address, md.port)
        self._sig.send(self._request("ACK", uri))  # ACK to 200 OK (2xx is end-to-end)

    def write(self, payload: bytes) -> None:
        if self._media is None or self.remote is None:
            return
        pkt = self._next_rtp(payload)
        self._media.sendto(pkt, self.remote)
        self.frames += 1
        self.bytes += len(pkt)

    def stats(self) -> dict:
        return {"leg": "sip", "session_id": self.session_id, "trunk": f"{self.host}:{self.port}",
                "transport": self.transport, "srtp": self.srtp, "registered": self.register,
                "remote_rtp": f"{self.remote[0]}:{self.remote[1]}" if self.remote else None,
                "active": self.active, "frames": self.frames, "bytes": self.bytes}

    def stop(self) -> dict:
        if self._sig._sock is not None and self.remote is not None:
            try:
                self._sig.send(self._request("BYE", self._to_uri()))
            except OSError:
                pass
        self._sig.close()
        if self._media is not None:
            self._media.close()
            self._media = None
        self.active = False
        return self.stats()


# --------------------------------------------------------------------------- #
# Webhook / VPS leg (HTTP chunked POST, or a WebSocket)
# --------------------------------------------------------------------------- #
class _WsClient:
    """Tiny synchronous WebSocket client (RFC 6455) — text/binary + close only."""

    def __init__(self, url: str, headers: dict | None = None, *,
                 timeout: float = CONNECT_TIMEOUT_SEC, tls_verify: bool = True) -> None:
        parts = urlsplit(url)
        secure = parts.scheme in ("wss", "https")
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if secure else 80)
        self.path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self.secure = secure
        self.headers = headers or {}
        self.timeout = timeout
        self.tls_verify = tls_verify
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            ctx = ssl.create_default_context()
            if not self.tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n")
        req += "".join(f"{k}: {v}\r\n" for k, v in self.headers.items()) + "\r\n"
        sock.sendall(req.encode())
        if b" 101 " not in sock.recv(4096).split(b"\r\n", 1)[0] + b" 101 ":
            sock.close()
            raise EgressError("websocket handshake failed")
        self.sock = sock

    def send_binary(self, payload: bytes) -> None:
        assert self.sock is not None
        mask = os.urandom(4)
        header = bytearray([0x82])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(b"\x88\x80" + os.urandom(4))
            except OSError:
                pass
            self.sock.close()
            self.sock = None


class WebhookLeg(MediaLeg):
    """Stream 16 kHz PCM16 to a customer VPS.

    ``mode=chunked``: HTTP/1.1 ``Transfer-Encoding: chunked`` POST; each audio
    frame is one chunk (optionally ``;sig=<hmac-sha256>`` when a signing secret
    is set). ``mode=ws``: masked binary WebSocket frames.
    """

    def __init__(self, session_id: str, url: str, *, mode: str = "chunked",
                 signing_secret: str | None = None, headers: dict | None = None,
                 tls_verify: bool = True) -> None:
        self.session_id, self.url, self.mode = session_id, url, mode
        self.signing_secret = signing_secret
        self.headers = headers or {}
        self.tls_verify = tls_verify
        self.frames = self.bytes = 0
        self.active = False
        self._conn: http.client.HTTPConnection | None = None
        self._ws: _WsClient | None = None

    def start(self) -> None:
        if self.mode == "ws":
            self._ws = _WsClient(self.url, self.headers, tls_verify=self.tls_verify)
            self._ws.connect()
        else:
            parts = urlsplit(self.url)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            if parts.scheme == "https":
                ctx = ssl.create_default_context()
                if not self.tls_verify:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                self._conn = http.client.HTTPSConnection(parts.hostname,
                                                          parts.port or 443,
                                                          timeout=CONNECT_TIMEOUT_SEC,
                                                          context=ctx)
            else:
                self._conn = http.client.HTTPConnection(parts.hostname,
                                                        parts.port or 80,
                                                        timeout=CONNECT_TIMEOUT_SEC)
            self._conn.putrequest("POST", path)
            self._conn.putheader("Transfer-Encoding", "chunked")
            self._conn.putheader("Content-Type", "application/octet-stream")
            self._conn.putheader("X-Audio-Format", "pcm16le/16000/mono")
            for k, v in self.headers.items():
                self._conn.putheader(k, v)
            self._conn.endheaders()
        self.active = True

    def _chunk_header(self, payload: bytes) -> bytes:
        head = f"{len(payload):x}".encode()
        if self.signing_secret:
            sig = hmac.new(self.signing_secret.encode(), payload, hashlib.sha256).hexdigest()
            head += b";sig=" + sig.encode()
        return head

    def write(self, payload: bytes) -> None:
        if self._ws is not None:
            self._ws.send_binary(payload)
        elif self._conn is not None:
            self._conn.send(self._chunk_header(payload) + b"\r\n" + payload + b"\r\n")
        else:
            return
        self.frames += 1
        self.bytes += len(payload)

    def stats(self) -> dict:
        return {"leg": "webhook", "session_id": self.session_id, "url": self.url,
                "mode": self.mode, "signed": bool(self.signing_secret), "active": self.active,
                "frames": self.frames, "bytes": self.bytes}

    def stop(self) -> dict:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        elif self._conn is not None:
            try:
                self._conn.send(b"0\r\n\r\n")
                self._conn.getresponse().read()
            except (OSError, http.client.HTTPException):
                pass
            self._conn.close()
            self._conn = None
        self.active = False
        return self.stats()


def capabilities() -> dict:
    """Machine-readable description of every supported destination.

    Single source of truth for the API (``GET /v1/forwarding/capabilities``),
    the dashboard and the docs — each ``sample_uri`` is guaranteed to parse.
    """
    return {
        "reference": "docs/design/VOICE-FORWARDING.md",
        "types": {
            "sip": {"transports": ["udp", "tcp", "tls"], "srtp": True, "register": True,
                    "options": ["transport", "register", "password", "realm", "srtp", "tls_verify"],
                    "sample_uri": "sip:1000@pbx.example:5060;transport=tls;register=1;srtp=1"},
            "socket": {"transports": ["udp", "tcp"], "modes": ["rtp", "raw"],
                       "codecs": ["pcmu", "pcm16"],
                       "options": ["mode", "codec", "srtp", "key"],
                       "sample_uri": "udp://vps.example:9999?mode=rtp"},
            "pstn": {"transports": ["udp", "tcp", "tls"], "srtp": True, "register": True,
                     "options": ["carrier_host", "carrier_port", "transport", "country",
                                 "register", "password", "realm", "srtp", "tls_verify"],
                     "sample_uri": "tel:+15551234?carrier_host=sip.carrier.example:5060"},
            "webhook": {"transports": ["http", "https", "ws", "wss"], "modes": ["chunked", "ws"],
                        "options": ["mode", "signing_secret", "headers", "tls_verify"],
                        "sample_uri": "https://vps.example/audio?mode=chunked"},
            "recording": {"options": [], "sample_uri": "file:///var/lib/voicebridge/call.wav"},
            "simulated": {"options": [], "sample_uri": "simulated"},
        },
    }


def build_leg(session_id: str, target: EgressTarget) -> MediaLeg:
    """Instantiate the leg for a parsed destination (not started)."""
    if target.kind == "recording":
        return RecordingLeg(session_id, target.path or "")
    if target.kind == "socket":
        if target.transport == "udp":
            return UdpSocketLeg(session_id, target.host or "", target.port or 0, target.mode,
                                codec=target.codec, srtp_key=target.srtp_key)
        return TcpSocketLeg(session_id, target.host or "", target.port or 0, target.mode,
                            codec=target.codec)
    if target.kind in ("sip", "pstn"):
        return SipTrunkLeg(session_id, target.host or "", target.port or 5060, user=target.user,
                           transport=target.transport, register=target.register,
                           password=target.password, realm=target.realm, srtp=target.srtp,
                           tls_verify=target.tls_verify)
    if target.kind == "webhook":
        return WebhookLeg(session_id, target.uri, mode=target.mode,
                          signing_secret=target.signing_secret, headers=target.headers,
                          tls_verify=target.tls_verify)
    return SimulatedLeg(session_id)
