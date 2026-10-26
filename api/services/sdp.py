"""Minimal SDP (RFC 4566) offer/answer with SDES crypto (RFC 4568).

Only what the egress legs need: one audio stream, PCMU, optional SRTP crypto
attribute. Parsing is deliberately tolerant — it reads the ``c=`` / ``m=`` /
``a=crypto:`` lines and ignores the rest.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field


@dataclass
class MediaDescription:
    address: str = "0.0.0.0"
    port: int = 0
    payloads: list[int] = field(default_factory=list)
    proto: str = "RTP/AVP"
    crypto: dict | None = None  # {"tag", "suite", "key" (bytes)}

    @property
    def secure(self) -> bool:
        return self.proto in ("RTP/SAVP", "RTP/SAVPF")


def offer(*, address: str, port: int, payloads: tuple[int, ...] = (0,),
          crypto: dict | None = None, session_name: str = "voicebridge") -> str:
    """Build an SDP offer. Pass ``crypto`` to advertise SRTP (RTP/SAVP)."""
    proto = "RTP/SAVP" if crypto else "RTP/AVP"
    lines = ["v=0", f"o=- 0 0 IN IP4 {address}", f"s={session_name}",
             f"c=IN IP4 {address}", "t=0 0",
             f"m=audio {port} {proto} " + " ".join(str(p) for p in payloads)]
    if 0 in payloads:
        lines.append("a=rtpmap:0 PCMU/8000")
    if crypto:
        b64 = base64.b64encode(crypto["key"]).decode()
        lines.append(f"a=crypto:{crypto['tag']} {crypto['suite']} inline:{b64}")
    return "\r\n".join(lines) + "\r\n"


def parse(body: str) -> MediaDescription:
    """Parse the first ``m=audio`` stream; raises ``ValueError`` if absent."""
    md = MediaDescription()
    found = False
    for raw in body.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("c=IN IP4 "):
            md.address = line[len("c=IN IP4 "):].strip()
        elif line.startswith("m=audio ") and not found:
            parts = line.split()
            md.port = int(parts[1])
            md.proto = parts[2] if len(parts) > 2 else "RTP/AVP"
            md.payloads = [int(p) for p in parts[3:] if p.isdigit()]
            found = True
        elif line.startswith("a=crypto:"):
            fields = line[len("a=crypto:"):].strip().split(None, 2)
            key = None
            if len(fields) > 2 and "inline:" in fields[2]:
                b64 = fields[2].split("inline:", 1)[1].split("|")[0].strip()
                key = base64.b64decode(b64)
            md.crypto = {"tag": fields[0],
                         "suite": fields[1] if len(fields) > 1 else "",
                         "key": key}
    if not found:
        raise ValueError("no m=audio line in SDP")
    return md
