#!/usr/bin/env python3
"""Real-destination smoke test for the egress plane.

Drives an actual call leg (SIP trunk, VPS socket, webhook, or file) with a 440 Hz
tone and prints the resulting stats. Use it to verify a customer PBX/VPS accepts
our media before wiring a device to it.

Examples::

    # SIP/PBX, plain RTP
    python scripts/pbx-smoke.py --type sip --uri sip:1000@pbx.example:5060 --seconds 3

    # SIP with REGISTER + digest auth + SRTP (SDES)
    python scripts/pbx-smoke.py --type sip --uri sips:1000@pbx.example \
        --option register=1 --option password=s3cret --option srtp=1

    # VPS UDP socket, RTP/G.711
    python scripts/pbx-smoke.py --type socket --uri 'udp://vps.example:9999?mode=rtp'

    # Customer webhook (HTTP chunked + HMAC signing)
    python scripts/pbx-smoke.py --type webhook --uri https://vps.example/audio \
        --option mode=chunked --option signing_secret=s3cret

    # Just validate a destination without sending anything
    python scripts/pbx-smoke.py --type socket --uri udp://vps:9999 --dry-run
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import time

sys.path.insert(0, ".")

from api import protocol                      # noqa: E402
from api.services import egress               # noqa: E402


def _tone(seconds: float) -> bytes:
    """16 kHz mono PCM16 440 Hz tone."""
    total = int(protocol.SAMPLE_RATE * seconds)
    return struct.pack(f"<{total}h",
                       *(int(12000 * math.sin(2 * math.pi * 440 * i / protocol.SAMPLE_RATE))
                         for i in range(total)))


def _parse_options(pairs: list[str]) -> dict:
    out: dict = {}
    for item in pairs:
        key, _, value = item.partition("=")
        out[key.strip()] = value if value else "1"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Egress-plane smoke test")
    ap.add_argument("--type", required=True,
                    choices=["sip", "socket", "webhook", "recording", "simulated"])
    ap.add_argument("--uri", required=True, help="destination URI (see --help / capabilities)")
    ap.add_argument("--option", action="append", default=[], metavar="k=v",
                    help="protocol option (repeatable), e.g. --option srtp=1")
    ap.add_argument("--seconds", type=float, default=3.0, help="tone duration (default 3s)")
    ap.add_argument("--dry-run", action="store_true", help="parse only, send nothing")
    args = ap.parse_args()

    options = _parse_options(args.option)
    try:
        target = egress.parse_destination(args.type, args.uri, options)
    except egress.EgressError as exc:
        print(f"✗ invalid destination: {exc}")
        return 2
    print(f"→ {target.as_dict()}")
    if args.dry_run:
        print("✓ parses (dry run)")
        return 0

    leg = egress.build_leg("smoke", target)
    print(f"starting {type(leg).__name__} …")
    try:
        leg.start()
    except Exception as exc:  # noqa: BLE001 - surface any transport failure
        print(f"✗ could not open leg: {exc}")
        return 3

    pcm = _tone(args.seconds)
    frame = protocol.FRAME_BYTES
    try:
        for offset in range(0, len(pcm), frame):
            leg.write(pcm[offset:offset + frame])
            time.sleep(0.02)  # 20 ms pacing
    finally:
        stats = leg.stop()
    print("✓ completed:", stats)
    print(f"  frames={stats.get('frames')} bytes={stats.get('bytes')}")
    return 0 if stats.get("frames") else 4


if __name__ == "__main__":
    raise SystemExit(main())
