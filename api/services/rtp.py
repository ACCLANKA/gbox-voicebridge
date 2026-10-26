"""RTP and SRTP (SDES) primitives for the egress legs — RFC 3550 / 3711.

Kept transport-free and small so the legs stay readable and the crypto can be
unit-tested on its own (including the RFC 3711 Appendix B.3 KDF vector).
"""
from __future__ import annotations

import hashlib
import hmac
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

RTP_HEADER_LEN = 12
PT_PCMU = 0
PT_TELEPHONE_EVENT = 101
CLOCK_8K = 8000
SAMPLES_PER_FRAME_8K = 160  # 20 ms

# SDES protection profile -> (tag length, auth key length)
SDES_AES_CM_128_HMAC_SHA1_80 = "AES_CM_128_HMAC_SHA1_80"
SDES_AES_CM_128_HMAC_SHA1_32 = "AES_CM_128_HMAC_SHA1_32"
SDES_PROFILES = {
    SDES_AES_CM_128_HMAC_SHA1_80: (10, 20),
    SDES_AES_CM_128_HMAC_SHA1_32: (4, 20),
}

_KDF_LABELS = {"enc": 0x00, "auth": 0x01, "salt": 0x02}


# --------------------------------------------------------------------------- #
# RTP packet
# --------------------------------------------------------------------------- #
def rtp_packet(payload: bytes, *, seq: int, timestamp: int, ssrc: int,
               payload_type: int = PT_PCMU, marker: bool = False) -> bytes:
    header = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | payload_type,
                         seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return header + payload


def header_length(packet: bytes) -> int:
    """RTP header size including CSRC list (no extension header)."""
    return RTP_HEADER_LEN + 4 * (packet[0] & 0x0F)


# --------------------------------------------------------------------------- #
# G.711 / resampling (16 kHz PCM16 -> 8 kHz telephony)
# --------------------------------------------------------------------------- #
def downsample_16k_to_8k(payload: bytes) -> bytes:
    count = len(payload) // 2
    samples = struct.unpack(f"<{count}h", payload[:count * 2])
    kept = samples[::2]
    return struct.pack(f"<{len(kept)}h", *kept)


def _ulaw(sample: int) -> int:
    bias, clip = 0x84, 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, clip) + bias
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def pcmu_encode(payload: bytes) -> bytes:
    count = len(payload) // 2
    samples = struct.unpack(f"<{count}h", payload[:count * 2])
    return bytes(_ulaw(s) for s in samples)


# --------------------------------------------------------------------------- #
# AES-CM counter mode (RFC 3711 §4.1.1): low 16 bits are the block counter
# --------------------------------------------------------------------------- #
def _aes_cm(key: bytes, iv: bytes, length: int) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    blocks = (length + 15) // 16
    counter = int.from_bytes(iv[14:16], "big")
    out = bytearray()
    for j in range(blocks):
        block = iv[:14] + ((counter + j) & 0xFFFF).to_bytes(2, "big")
        out += encryptor.update(block)
    encryptor.finalize()
    return bytes(out[:length])


def kdf(master_key: bytes, master_salt: bytes, label: str, length: int) -> bytes:
    """RFC 3711 §4.3 key derivation for ``key_derivation_rate = 0``.

    ``label`` is one of ``enc`` / ``auth`` / ``salt``. Validated against the
    RFC 3711 Appendix B.3 vector in ``tests/test_rtp.py``.
    """
    iv = bytearray(master_salt + b"\x00\x00")   # 112-bit salt, low 16 = counter
    iv[7] ^= _KDF_LABELS[label]                 # key_id
    return _aes_cm(master_key, bytes(iv), length)


def session_keys(master_key: bytes, master_salt: bytes,
                 profile: str = SDES_AES_CM_128_HMAC_SHA1_80) -> tuple[bytes, bytes, bytes]:
    """Return ``(encryption_key, auth_key, salt)`` for a master key/salt pair."""
    tag_len, auth_len = SDES_PROFILES[profile]
    return (kdf(master_key, master_salt, "enc", 16),
            kdf(master_key, master_salt, "auth", auth_len),
            kdf(master_key, master_salt, "salt", 14))


def srtp_protect(rtp: bytes, *, session: tuple[bytes, bytes, bytes], roc: int = 0,
                 profile: str = SDES_AES_CM_128_HMAC_SHA1_80) -> bytes:
    """Encrypt+authenticate an RTP packet (no header encryption, no MKI)."""
    enc_key, auth_key, salt = session
    tag_len, _ = SDES_PROFILES[profile]
    hlen = header_length(rtp)
    header, payload = rtp[:hlen], rtp[hlen:]
    ssrc = struct.unpack("!I", rtp[8:12])[0]
    seq = struct.unpack("!H", rtp[2:4])[0]
    index = (roc << 16) | seq
    iv = ((int.from_bytes(salt, "big") << 16) ^ (ssrc << 64) ^ (index << 16))
    keystream = _aes_cm(enc_key, iv.to_bytes(16, "big"), len(payload))
    enc_payload = bytes(a ^ b for a, b in zip(payload, keystream))
    tag = hmac.new(auth_key, header + enc_payload + struct.pack("!I", roc),
                   hashlib.sha1).digest()[:tag_len]
    return header + enc_payload + tag


def srtp_unprotect(srtp: bytes, *, session: tuple[bytes, bytes, bytes], roc: int = 0,
                   profile: str = SDES_AES_CM_128_HMAC_SHA1_80) -> bytes:
    """Inverse of :func:`srtp_protect` (used by tests and, later, ingest)."""
    enc_key, auth_key, salt = session
    tag_len, _ = SDES_PROFILES[profile]
    hlen = header_length(srtp)
    header, body = srtp[:hlen], srtp[hlen:]
    enc_payload, tag = body[:-tag_len], body[-tag_len:]
    expected = hmac.new(auth_key, header + enc_payload + struct.pack("!I", roc),
                        hashlib.sha1).digest()[:tag_len]
    if not hmac.compare_digest(tag, expected):
        raise ValueError("SRTP auth tag mismatch")
    ssrc = struct.unpack("!I", srtp[8:12])[0]
    seq = struct.unpack("!H", srtp[2:4])[0]
    index = (roc << 16) | seq
    iv = ((int.from_bytes(salt, "big") << 16) ^ (ssrc << 64) ^ (index << 16))
    keystream = _aes_cm(enc_key, iv.to_bytes(16, "big"), len(enc_payload))
    return header + bytes(a ^ b for a, b in zip(enc_payload, keystream))
