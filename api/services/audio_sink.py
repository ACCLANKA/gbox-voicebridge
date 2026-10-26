"""Continuous audio sink for the dedicated UDP audio channel.

Each packet is one 20 ms frame. Packets are independent, so a lost one is simply missing time:
the sink pads with silence at the right place, keeping the file's timeline equal to the call's.
That is what makes the recording sound like a stream rather than stitched fragments, and it is
also the shape a SIP/RTP leg consumes.
"""
import array
import logging
import math
import os
import sys
import threading
import time

logger = logging.getLogger("audio_sink")

RATE = 16000
PACKET_MS = 20
BYTES_PER_MS = RATE * 2 // 1000

# A 20 ms frame quieter than this is silence, not speech (-44 dBFS).
SPEECH_RMS = 0.006

# Measured on this rig: a working call runs 13-22% speech, a call whose audio never
# reaches the headset runs 2-3%. 5% separates the two with room on both sides.
SPEECH_WARN_PCT = 5.0
SPEECH_WARN_MIN_S = 5.0


def is_silent_session(frames: int, active: int, audio_s: float) -> bool:
    """True when a session carried essentially no speech for long enough to matter."""
    if frames <= 0 or audio_s < SPEECH_WARN_MIN_S:
        return False
    return (100.0 * active / frames) < SPEECH_WARN_PCT


def frame_rms(payload: bytes) -> float:
    """RMS of one PCM16 frame, 0..1. Used to tell a silent link from a live one."""
    n = len(payload) // 2
    if not n:
        return 0.0
    a = array.array("h")
    a.frombytes(payload[: n * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return math.sqrt(sum(v * v for v in a) / n) / 32768.0


class Session:
    def __init__(self, serial: str):
        self.serial = serial
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.path = "/app/data/stt/%s-udp-%s.pcm" % (serial, stamp)
        self.f = open(self.path, "wb")
        self.last_seq = None
        self.last_seen = time.time()
        self.frames = 0
        self.bytes = 0
        self.pad_bytes = 0
        self.gaps = 0
        self.max_gap_ms = 0
        self.active = 0          # frames that actually carried speech

    def write(self, seq: int, payload: bytes) -> None:
        if self.last_seq is not None:
            delta = (seq - self.last_seq) & 0xFFFF
            if 1 < delta < 500:
                missing = delta - 1
                self.gaps += 1
                self.max_gap_ms = max(self.max_gap_ms, missing * PACKET_MS)
                pad = b"\x00" * (missing * PACKET_MS * BYTES_PER_MS)
                self.f.write(pad)
                self.pad_bytes += len(pad)
        self.last_seq = seq
        self.f.write(payload)
        self.bytes += len(payload)
        self.frames += 1
        if frame_rms(payload) >= SPEECH_RMS:
            self.active += 1

    def close(self) -> None:
        try:
            self.f.close()
        except Exception:
            return
        audio_s = self.bytes / (RATE * 2)
        speech_pct = (100.0 * self.active / self.frames) if self.frames else 0.0
        logger.warning(
            "UDPAUDIO %s frames=%d bytes=%d audio_s=%.2f pad_s=%.2f gaps=%d max_gap_ms=%d "
            "speech=%.1f%% file=%s",
            self.serial, self.frames, self.bytes, audio_s, self.pad_bytes / (RATE * 2),
            self.gaps, self.max_gap_ms, speech_pct, self.path)
        if is_silent_session(self.frames, self.active, audio_s):
            # A healthy call is 10-25% speech. Near-zero means the device streamed
            # digital silence: the phone never routed the call into the headset link,
            # so no amount of server-side work can transcribe it.
            logger.error(
                "UDPAUDIO %s received no call audio: %.1f%% speech over %.1f s. The phone is "
                "not routing this call to the headset. On a WhatsApp/VoIP call, open the "
                "in-call audio route and choose the Bluetooth headset; a SIM call uses it "
                "automatically.", self.serial, speech_pct, audio_s)


_sessions = {}
_lock = threading.Lock()


def feed(serial: str, seq: int, kind: int, payload: bytes) -> None:
    if not serial or not payload:
        return
    with _lock:
        sess = _sessions.get(serial)
        now = time.time()
        if sess is None or (now - sess.last_seen) > 5.0:
            if sess is not None:
                sess.close()
            sess = Session(serial)
            _sessions[serial] = sess
        sess.last_seen = now
        sess.write(seq, payload)


def close_idle(max_idle: float = 15.0) -> None:
    with _lock:
        now = time.time()
        for serial, sess in list(_sessions.items()):
            if now - sess.last_seen > max_idle:
                sess.close()
                del _sessions[serial]
