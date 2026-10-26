"""Talking bot: send a spoken clip, get a spoken Sinhala answer back.

One request does the whole loop, so any device that can POST audio and play a WAV can
hold a conversation:

    POST /v1/toy/talk        body: raw PCM16, 16 kHz, mono
    -> audio/wav (the reply) + X-Toy-Transcript / X-Toy-Reply headers (percent-encoded)

It reuses the same three services the live call agent uses - the Whisper Sinhala STT,
DeepSeek, and the VITS TTS container - so the voice you hear is the voice the hotline
uses, not a second speech stack that would drift from it.
"""
from __future__ import annotations

import json
import io
import re
import wave
import logging
import time
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/toy", tags=["toy"])

STT_URL = os.environ.get("STT_URL", "http://172.18.0.1:8896")
LLM_URL = os.environ.get("LLM_URL", "https://api.deepseek.com/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
LLM_KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY") or ""

# The container answers on /api/tts; older deployments used /tts. Try in order.
TTS_URLS = [u for u in (os.environ.get("TOY_TTS_URL"),
                        "http://tts:8766/api/tts",
                        "http://tts:8766/tts") if u]

# Rolling context for this device. The toy is a single conversation: keeping the last
# few turns lets the model follow the thread instead of answering each clip cold.
_HISTORY = []


def _remember(role: str, text: str) -> None:
    if not text:
        return
    _HISTORY.append({"role": role, "content": text})
    del _HISTORY[:-12]


def _messages(text: str):
    return [{"role": "system", "content": PERSONA}] + _HISTORY[-8:] + \
           [{"role": "user", "content": text}]


PERSONA = (
    "You are Gbox. Your name is written 'Gbox' but in Sinhala it must always be "
    "written exactly ජී බොක්ස් (never ගොබොක්ස්, ගොබි or ජීබොක්ස්) - a speech "
    "synthesiser reads it aloud and only ජී බොක්ස් sounds like G-box. "
    "You are a cheerful Sinhala-speaking friend on a small speaker. "
    "Always answer in spoken Sinhala (සිංහල). Answer fully in Sinhala - two or three sentences, up to about 40 words, never one clipped phrase, "
    "warm and a little playful - this is a fun conversation, not a call centre. "
    "Never use English words unless the person used them first. No lists, no markdown, "
    "no emoji: your text is read out loud by a speech synthesiser."
)

MIN_BYTES = 6400          # 0.2 s at 16 kHz PCM16 - shorter is not speech

# This endpoint spends real money per call (LLM + TTS). On a public host it must not be
# open: when TOY_KEY is set, callers have to present it.
TOY_KEY = os.environ.get("TOY_KEY", "")


def _post(url: str, data: bytes, headers: dict, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _stt(pcm16: bytes) -> str:
    raw = _post(STT_URL + "/transcribe", pcm16,
                {"Content-Type": "application/octet-stream"})
    try:
        return (json.loads(raw.decode()).get("text") or "").strip()
    except ValueError:
        return ""


def _reply(text: str) -> str:
    if not LLM_KEY:
        raise HTTPException(503, "no LLM key configured")
    body = json.dumps({
        "model": LLM_MODEL,
        "temperature": 0.7,
        "max_tokens": 220,
        "messages": _messages(text),
    }).encode()
    raw = _post(LLM_URL, body,
                {"Content-Type": "application/json", "Authorization": "Bearer " + LLM_KEY})
    data = json.loads(raw.decode())
    return (data["choices"][0]["message"]["content"] or "").strip()


def _tts(text: str) -> bytes:
    body = json.dumps({"text": text}).encode()
    last = None
    for url in TTS_URLS:
        try:
            return _post(url, body, {"Content-Type": "application/json"}, timeout=180)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
    raise HTTPException(502, "TTS failed: %s" % last)



_SENT = re.compile(r"(?<=[.!?])\s+")


_CHUNK_WORDS = 60        # effectively sentence-level: fragments sound clipped


def _chunk_words(part: str):
    """Break a sentence into <=8 word pieces, preferring commas."""
    words = part.split()
    if len(words) <= _CHUNK_WORDS:
        return [part]
    pieces, cur = [], []
    for w in words:
        cur.append(w)
        long_enough = len(cur) >= _CHUNK_WORDS
        if w.endswith(",") or long_enough:
            pieces.append(" ".join(cur))
            cur = []
    if cur:
        pieces.append(" ".join(cur))
    merged = []
    for piece in pieces:
        if merged and len(piece.split()) < 4:      # a 1-3 word tail starves playback
            merged[-1] = merged[-1] + " " + piece
        else:
            merged.append(piece)
    return [x for x in merged if x.strip()]


def _split_long(part: str):
    return _chunk_words(part)


def _sentences(text: str):
    parts = [x.strip() for x in _SENT.split(text) if x.strip()]
    split = []
    for part in parts:
        split.extend(_split_long(part))
    parts = split
    return parts or [text]


def _concat_wav(wavs):
    chunks, sr, ch, sw = [], None, None, None
    for w in wavs:
        with wave.open(io.BytesIO(w), "rb") as r:
            sr, ch, sw = r.getframerate(), r.getnchannels(), r.getsampwidth()
            chunks.append(r.readframes(r.getnframes()))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(ch)
        out.setsampwidth(sw)
        out.setframerate(sr)
        out.writeframes(b"".join(chunks))
    return buf.getvalue()



def _wav_header(sr: int, ch: int = 1, sw: int = 2, data_len: int = 0xFFFFFFFF) -> bytes:
    """RIFF header with unknown length, the normal convention for streamed audio."""
    import struct
    byte_rate = sr * ch * sw
    block_align = ch * sw
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, ch, sr, byte_rate, block_align, sw * 8)
            + b"data" + struct.pack("<I", data_len))


def _pcm_of(wav_bytes: bytes) -> tuple:
    """(sample_rate, channels, pcm) from a TTS WAV."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as r:
        return r.getframerate(), r.getnchannels(), r.readframes(r.getnframes())




# ---- cache pre-warm -------------------------------------------------------
_WARM_PHRASES = [
    "ආයුබෝවන්! මම ජී බොක්ස්.",
    "ඔයාට කොහොමද?",
    "මට තේරුණා.",
    "හරි, එහෙනම්.",
    "ස්තූතියි!",
    "ආයුබෝවන්! සුබ දවසක්!",
]
CACHE_HITS = 0
CACHE_MISSES = 0


def _warm_cache() -> None:
    """Synthesise the common phrases once, in the background, one at a time.

    The TTS container serves serially, so this paces itself to avoid queueing in front of
    a real request.
    """
    import time as _t
    _t.sleep(5)                     # let the app finish starting
    warmed = skipped = 0
    t0 = _t.monotonic()
    for phrase in _WARM_PHRASES:
        if os.path.exists(_cache_path(_tts_text(phrase))):
            skipped += 1
            continue
        try:
            pcm = _tts_cached(_tts_text(phrase))
            if pcm:
                warmed += 1
        except Exception as exc:    # noqa: BLE001
            print("toy warm failed: %s" % str(exc)[:80], flush=True)
        _t.sleep(1.0)
    print("toy warm: %d synthesised, %d already cached in %.1fs"
          % (warmed, skipped, _t.monotonic() - t0), flush=True)


def _start_warmer() -> None:
    import threading
    threading.Thread(target=_warm_cache, name="toy-warm", daemon=True).start()



# ---- TTS: same strategy as the call agent ---------------------------------
TTS_CACHE_DIR = os.environ.get("TTS_CACHE_DIR", "/app/data/tts_cache")
TOY_RATE = 16000
SHORT_REPLY_CHARS = 60


def _cache_path(text: str) -> str:
    import hashlib
    key = hashlib.sha1(("toy:" + text).encode("utf-8")).hexdigest()[:20]
    return os.path.join(TTS_CACHE_DIR, key + ".pcm")


def _to_16k(wav_bytes: bytes) -> bytes:
    import array
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        return b""
    a = array.array("h")
    a.frombytes(raw)
    if ch == 2:
        a = array.array("h", [(a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2)])
    if sr != TOY_RATE and len(a):
        step = sr / float(TOY_RATE)
        n_out = max(1, int(len(a) / step))
        out = array.array("h", bytes(2 * n_out))
        for i in range(n_out):
            pos = i * step
            j = int(pos)
            frac = pos - j
            s0 = a[j] if j < len(a) else 0
            s1 = a[j + 1] if j + 1 < len(a) else s0
            out[i] = int(s0 + (s1 - s0) * frac)
        a = out
    return a.tobytes()


def _tts_cached(text: str) -> bytes:
    """16 kHz PCM16 for this text, from the shared disk cache when possible."""
    global CACHE_HITS, CACHE_MISSES
    path = _cache_path(text)
    try:
        with open(path, "rb") as fh:
            CACHE_HITS += 1
            return fh.read()
    except OSError:
        pass
    CACHE_MISSES += 1
    pcm = _to_16k(_tts(text))
    try:
        os.makedirs(TTS_CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(pcm)
        os.replace(tmp, path)
    except OSError as exc:
        print("toy: cache write failed: %s" % str(exc)[:80], flush=True)
    return pcm


_ONE_CALL_CHARS = 400          # ~60 words; below this, one request is fastest
_GROUP_CHARS = 200             # group long replies into ~200-char requests


def _pieces(text: str):
    """One TTS call for a normal reply; group only when the text is very long."""
    t = text.strip()
    if len(t) <= _ONE_CALL_CHARS:
        return [t]
    sents = [x.strip() for x in re.split(r"(?<=[.!?\u0964;])\s+", t) if x.strip()]
    if len(sents) <= 1:
        return [t]
    out, cur = [], ""
    for one in sents:
        if cur and len(cur) + len(one) + 1 > _GROUP_CHARS:
            out.append(cur)
            cur = one
        else:
            cur = (cur + " " + one).strip()
    if cur:
        out.append(cur)
    return out


def _tts_text(text: str) -> str:
    """Ensure a terminal mark: TTS clips the last syllable without one."""
    t = text.strip()
    if t and t[-1] not in ".!?\u0964":
        t += "."
    return t


def _trim_silence(pcm: bytes, sr: int, thresh: int = 300) -> bytes:
    """Strip leading/trailing near-silence, keeping 100 ms of margin."""
    import array
    a = array.array("h", pcm)
    n = len(a)
    i, j = 0, n
    while i < n and abs(a[i]) < thresh:
        i += 1
    while j > i and abs(a[j - 1]) < thresh:
        j -= 1
    if j <= i:
        return pcm
    margin = int(sr * 0.10)
    i = max(0, i - margin)
    j = min(n, j + margin)
    return a[i:j].tobytes()


def _save_last_reply(pcm: bytes, sr: int) -> None:
    """Dev aid: keep the last reply so it can be transcribed back and compared."""
    try:
        import os
        tmp = "/tmp/toy_last_%s.wav" % os.getpid()
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm)
    except Exception as e:
        print("toy: could not save last reply: %s" % e, flush=True)


def _stream_wav(text: str):
    """16 kHz WAV header then PCM, piece by piece (call-agent strategy)."""
    parts = _pieces(text)
    header_sent = False
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    futures = [pool.submit(_tts_cached, _tts_text(x)) for x in parts]
    for i, fut in enumerate(futures):
        t_one = __import__("time").monotonic()
        try:
            pcm = fut.result()
        except HTTPException as e:
            print("toy stream: piece %d failed: %s" % (i, e.detail), flush=True)
            continue
        if not pcm:
            continue
        pcm = _trim_silence(pcm, TOY_RATE)
        _save_last_reply(pcm, TOY_RATE)
        if not header_sent:
            yield _wav_header(TOY_RATE, 1)
            header_sent = True
        print("toy tts: piece %d/%d synth=%.2fs audio=%.2fs cache=%d/%d"
              % (i + 1, len(parts), __import__("time").monotonic() - t_one,
                 len(pcm) / 2.0 / TOY_RATE, CACHE_HITS,
                 CACHE_HITS + CACHE_MISSES), flush=True)
        yield pcm
    pool.shutdown(wait=False)
    if not header_sent:
        yield _wav_header(TOY_RATE, 1)


@router.get("/health")
def toy_health() -> dict:
    """Cheap probe: TCP-reachable TTS and STT (synthesising here would be slow)."""
    import socket

    def reachable(url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        try:
            with socket.create_connection((parts.hostname, parts.port or 80), timeout=3):
                return True
        except OSError:
            return False

    return {"stt": STT_URL, "stt_up": reachable(STT_URL),
            "tts": TTS_URLS[0], "tts_up": reachable(TTS_URLS[0]),
            "llm": LLM_MODEL, "llm_key": bool(LLM_KEY),
            "cache_dir": TTS_CACHE_DIR}


@router.post("/nudge")
async def nudge(request: Request) -> Response:
    """No audio: the board asks "say something" because the user stayed silent."""
    if TOY_KEY and request.headers.get("X-Toy-Key") != TOY_KEY:
        raise HTTPException(401, "missing or wrong X-Toy-Key")
    text = _continue_reply()
    if not text:
        raise HTTPException(502, "the model returned nothing")
    _remember("assistant", text)
    print("toy nudge: %s" % text[:120], flush=True)
    return StreamingResponse(_stream_wav(text), media_type="audio/wav")


@router.post("/talk")
async def talk(request: Request) -> Response:
    if TOY_KEY and request.headers.get("X-Toy-Key") != TOY_KEY:
        raise HTTPException(401, "missing or wrong X-Toy-Key")
    pcm = await request.body()
    if len(pcm) < MIN_BYTES:
        raise HTTPException(422, "send raw PCM16 16 kHz mono; that clip is too short")

    t0 = time.monotonic()
    heard = _stt(pcm)
    if not heard:
        raise HTTPException(422, "could not make out any speech in that clip")

    t1 = time.monotonic()
    reply = _reply(heard)
    if not reply:
        raise HTTPException(502, "the model returned nothing")

    _remember("user", heard)
    _remember("assistant", reply)
    t2 = time.monotonic()
    t3 = time.monotonic()
    print("toy timing: stt=%.2fs llm=%.2fs total=%.2fs in=%.1fs out_words=%d (streamed)"
          % (t1 - t0, t2 - t1, t3 - t0, len(pcm) / 32000.0, len(reply.split())), flush=True)

    return StreamingResponse(_stream_wav(reply), media_type="audio/wav", headers={
        "X-Toy-Transcript": urllib.parse.quote(heard),
        "X-Toy-Reply": urllib.parse.quote(reply),
    })


_start_warmer()
