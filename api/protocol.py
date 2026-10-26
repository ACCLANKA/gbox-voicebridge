"""Voice Bridge device <-> cloud wire protocol (docs/design/DEVICE-PROTOCOL.md).

Control traffic is JSON in WebSocket text frames; audio is binary frames with a
one-byte kind header. Transport-agnostic so the ingest edge and the firmware
host tests share one definition.
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from typing import Any

AUDIO_PCM16 = 0x01
AUDIO_OPUS = 0x02
# Narrowband PCM16: what a CVSD (8 kHz) SCO link actually carries. The phone's microphone
# has nothing above 4 kHz, so resampling uplink audio to 16 kHz doubled the bytes the board
# pushed at a radio that could not spare them, for no information gain. Downlink stays 16 kHz
# (the brain/TTS is wideband and the device decimates for CVSD).
AUDIO_PCM16_8K = 0x03
# G.711 mu-law: 1 byte/sample, half of PCM16 for the same audio. Mirrors VB_AUDIO_ULAW_8K in
# firmware/main/voice_protocol.h. The kind byte travels in every frame, so a board switching to
# mu-law needs no negotiation - the far end either understands 0x04 or it does not.
AUDIO_ULAW_8K = 0x04
AUDIO_KIND_LEGACY_PCM16 = 0x00  # bench firmware stamps uplink 0x00; treated as 8 kHz PCM16
AUDIO_KINDS = (AUDIO_PCM16, AUDIO_OPUS, AUDIO_PCM16_8K, AUDIO_ULAW_8K)

# Sample rate implied by the kind, for duration/level arithmetic. Frame length is the other
# half of the story: 20 ms is 320 samples at 16 kHz and 160 at 8 kHz.
SAMPLE_RATE = 16000
SAMPLE_RATE_FOR_KIND = {AUDIO_PCM16: 16000, AUDIO_OPUS: 16000, AUDIO_PCM16_8K: 8000,
                     AUDIO_ULAW_8K: 8000}
FRAME_MS = 20
BYTES_PER_SAMPLE = 2
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000      # 320
FRAME_BYTES = FRAME_SAMPLES * BYTES_PER_SAMPLE      # 640

# --- Audio level standard ----------------------------------------------------------
# Mirrors firmware/main/voice_protocol.h; tests/unit/test_audio_levels.py parses that
# header and fails if the two sides drift, because a level that means one thing on the
# board and another in the log is worse than no convention at all.
#   full scale   PCM16, signed, +/-32768, 20 ms frames.
#   no AGC       the board neither normalises nor limits - the phone's own AGC owns
#                speech level.
#   test tone    -6 dBFS peak: 6 dB of headroom for CVSD's edges, loud enough that a
#                dropped pip is obvious.
#   HFP volumes  AT+VGS/AT+VGM, 0..15.
DBFS_FULL_SCALE = 32768.0
# Nominal, as a human says it. The amplitude below is the exact half-scale value, which is
# -6.02 dBFS: a power-of-two shift is exact and stable in both languages, whereas rounding
# -6.00 dBFS to an integer would land on 16422 and differ by one on a different libm.
TEST_TONE_DBFS = -6
TEST_TONE_AMPLITUDE = 16384         # half scale == TEST_TONE_DBFS to within 0.03 dB
HFP_SPK_VOLUME = 15
HFP_MIC_VOLUME = 15


def amplitude_for_dbfs(dbfs: float) -> int:
    """PCM16 amplitude for a peak level in dBFS (0 dBFS -> full scale)."""
    return int(round(DBFS_FULL_SCALE * 10.0 ** (dbfs / 20.0)))


def tone_pcm16(hz: float = 1000.0, ms: int = FRAME_MS, dbfs: float = TEST_TONE_DBFS,
               rate: int = SAMPLE_RATE) -> bytes:
    """One frame of a standard-level test tone.

    The server-side generator, at the same level as the board's bench pip, so a tone
    pushed from here and a pip heard from there are directly comparable.
    """
    n = rate * ms // 1000
    amp = amplitude_for_dbfs(dbfs)
    return struct.pack(f"<{n}h",
                       *[int(round(amp * math.sin(2 * math.pi * hz * i / rate))) for i in range(n)])


def tone_frame(hz: float = 1000.0, ms: int = FRAME_MS, dbfs: float = TEST_TONE_DBFS,
               rate: int = SAMPLE_RATE, kind: int = AUDIO_PCM16) -> bytes:
    """A standard-level downlink tone frame, ready to push to a device."""
    return encode_audio(tone_pcm16(hz, ms, dbfs, rate), kind)


class ProtocolError(ValueError):
    """Malformed control or audio frame."""


@dataclass(frozen=True)
class AudioFrame:
    kind: int
    payload: bytes

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE_FOR_KIND.get(self.kind, SAMPLE_RATE)

    @property
    def samples(self) -> int:
        return len(self.payload) // BYTES_PER_SAMPLE

    @property
    def duration_ms(self) -> int:
        return self.samples * 1000 // self.sample_rate


def encode_audio(payload: bytes, kind: int = AUDIO_PCM16) -> bytes:
    if kind not in AUDIO_KINDS:
        raise ProtocolError(f"unknown audio kind {kind}")
    return bytes([kind]) + payload


def to_downlink_16k(frame: AudioFrame) -> tuple[bytes, int]:
    """Present an uplink frame as a downlink payload the board plays at the right speed.

    The board's downlink path predates narrowband uplink and assumes 16 kHz PCM16: it
    decimates by two for a CVSD call and copies straight through for mSBC, and it does not
    read the frame's kind at all. So echoing our own 8 kHz uplink back unchanged makes the
    board decimate a second time - the voice returns at half speed, which is heard as "no
    repeat" rather than as an echo. Converting here keeps one board code path and needs no
    firmware change.
    """
    if frame.kind == AUDIO_KIND_LEGACY_PCM16:
        # The bench firmware stamps uplink frames with 0x00 rather than AUDIO_PCM16_8K. The payload is
        # 8 kHz PCM16 (even length, SCO cadence), so treat it as such instead of echoing a kind the board
        # rejects. Without this the board logs "downlink: bad kind 0x00" and drops the audio.
        try:
            frame = frame._replace(kind=AUDIO_PCM16_8K)
        except AttributeError:
            import dataclasses
            frame = dataclasses.replace(frame, kind=AUDIO_PCM16_8K)
    if frame.kind != AUDIO_PCM16_8K:
        return frame.payload, frame.kind
    src = struct.unpack(f"<{frame.samples}h", frame.payload)
    up: list[int] = []
    for i, s in enumerate(src):
        up.append(s)
        up.append((s + src[i + 1]) // 2 if i + 1 < len(src) else s)
    return struct.pack(f"<{len(up)}h", *up), AUDIO_PCM16


def _ulaw_decode_sample(u: int) -> int:
    """G.711 mu-law expansion - the inverse of the firmware's encoder."""
    u = (~u) & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    t -= 0x84
    return -t if (u & 0x80) else t


def ulaw_encode_pcm16(pcm: bytes) -> bytes:
    """Encode PCM16 to mu-law. Present so the server can be tested against the same standard the
    board implements, and so a future downlink can use it."""
    out = bytearray()
    for s in struct.unpack(f"<{len(pcm) // 2}h", pcm):
        sign = (s >> 8) & 0x80
        mag = -s if s < 0 else s
        mag = min(mag, 32635) + 0x84
        exponent, mask = 7, 0x4000
        while exponent > 0 and not (mag & mask):
            mask >>= 1
            exponent -= 1
        out.append((~(sign | (exponent << 4) | ((mag >> (exponent + 3)) & 0x0F))) & 0xFF)
    return bytes(out)


def decode_audio(frame: bytes) -> AudioFrame:
    if not frame:
        raise ProtocolError("empty audio frame")
    kind, payload = frame[0], frame[1:]
    if kind == AUDIO_KIND_LEGACY_PCM16:
        # Observed from the bench firmware: uplink frames arrive stamped 0x00 instead of
        # AUDIO_PCM16_8K. Rejecting them made device_ws skip every frame (except ProtocolError:
        # continue), so the cloud leg carried no audio at all. The payload is 8 kHz PCM16 - even
        # length, SCO cadence - so accept it rather than losing the call.
        kind = AUDIO_PCM16_8K
    if kind not in AUDIO_KINDS:
        raise ProtocolError(f"unknown audio kind {kind}")
    if kind == AUDIO_PCM16 and len(payload) % BYTES_PER_SAMPLE:
        raise ProtocolError("PCM payload must be an even number of bytes")
    if kind == AUDIO_ULAW_8K:
        # Expanded to the narrowband PCM16 the rest of the pipeline already speaks. Which codec
        # the radio could afford is a transport detail, and nothing downstream should have to
        # branch on it - the echo path, the level meter and the recordings all stay unchanged.
        if not payload:
            raise ProtocolError("empty mu-law payload")
        pcm = b"".join(struct.pack("<h", _ulaw_decode_sample(b)) for b in payload)
        return AudioFrame(kind=AUDIO_PCM16_8K, payload=pcm)
    return AudioFrame(kind=kind, payload=payload)


def level_dbfs(payload: bytes) -> float:
    """RMS level of a PCM16 payload in dBFS (``-inf`` for silence/short input)."""
    count = len(payload) // BYTES_PER_SAMPLE
    if count == 0:
        return float("-inf")
    samples = struct.unpack(f"<{count}h", payload[:count * BYTES_PER_SAMPLE])
    mean_sq = sum(s * s for s in samples) / count
    if mean_sq <= 0:
        return float("-inf")
    return round(20 * math.log10((mean_sq ** 0.5) / 32768.0), 1)


def parse_control(text: str) -> dict[str, Any]:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON control frame: {exc}") from exc
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("control frame must be a JSON object with a 'type'")
    return msg


def ack(seq: int | None, *, session_id: str | None = None, action: str = "none") -> str:
    return json.dumps({"type": "ack", "seq": seq, "call_session_id": session_id,
                       "action": action})


def control(action: str, **extra: Any) -> str:
    return json.dumps({"type": "control", "action": action, **extra})


def pong() -> str:
    return json.dumps({"type": "pong"})
