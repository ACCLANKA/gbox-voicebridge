"""Conversational loop for Gbox: VAD -> Sinhala STT -> LLM -> TTS -> device.

Latency rules learned on the bench:
  * the reply is streamed: the LLM's first sentence goes to TTS immediately, so playback starts
    while the rest of the answer is still being generated;
  * TTS is split at commas as well as full stops, because a whole sentence costs ~1.2 s of
    synthesis before a single word is heard;
  * barge-in ignores the first 400 ms of our own playback and needs 300 ms of speech, because
    the handset echoes us back into the uplink and would otherwise cut the agent off instantly.
"""
from __future__ import annotations

import array
import collections
import json
import logging
import math
import os
import queue
import re
import threading
import time
import urllib.request

from . import transcript
from .glo_brain import GloBrain, fix_stt, GREETING as GLO_GREETING
from .realtime import OmniBridge, resample_16k_to_24k, resample_24k_to_16k

log = logging.getLogger("agent")

STT_URL = os.environ.get("STT_URL", "http://172.18.0.1:8896")
TTS_URL = os.environ.get("TTS_URL", "http://tts:8766/tts")
LLM_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
LLM_URL = os.environ.get("LLM_URL", "https://api.deepseek.com/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
SERIAL = os.environ.get("BRIDGE_SERIAL", "GBX-BENCH01")

FRAME_MS = 20
RATE = 16000
SPEECH_RMS = float(os.environ.get("AGENT_SPEECH_RMS", "0.012"))
END_SIL_MS = int(os.environ.get("AGENT_END_SIL_MS", "750"))
MIN_SPEECH_MS = 280   # a single spoken word is a valid answer
MAX_UTTER_MS = 8000

CALL_GAP_S = float(os.environ.get("AGENT_CALL_GAP_S", "8.0"))
GREETING_COOLDOWN_S = float(os.environ.get("AGENT_GREET_COOLDOWN_S", "30.0"))
BACKEND = os.environ.get("AGENT_BACKEND", "pipeline")        # "realtime" | "pipeline"
REALTIME_URL = os.environ.get(
    "AGENT_REALTIME_URL",
    "wss://token-plan.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime"
    "?model=qwen-audio-3.0-realtime-plus")
REALTIME_VOICE = os.environ.get("AGENT_REALTIME_VOICE", "longanqian")
FILLER_TEXT = os.environ.get("AGENT_FILLER", "එක මොහොතක් රැඳී සිටින්න.")
FILLER_DELAY_S = float(os.environ.get("AGENT_FILLER_DELAY_S", "1.3"))
UPLINK_STALL_S = float(os.environ.get("AGENT_UPLINK_STALL_S", "6.0"))
# Half-duplex: our own voice comes back through the handset mic. Ignore the uplink while speaking
# and for this tail (the phone's playback and the mic path both lag the socket).
SELF_MUTE_TAIL_S = float(os.environ.get("AGENT_SELF_MUTE_TAIL_S", "0.6"))
BARGE_ENABLED = os.environ.get("AGENT_BARGE", "0") in ("1", "true", "yes")
BARGE_FACTOR = float(os.environ.get("AGENT_BARGE_FACTOR", "2.4"))
BARGE_MIN_RMS = float(os.environ.get("AGENT_BARGE_MIN_RMS", "0.035"))
BARGE_CONFIRM_MS = int(os.environ.get("AGENT_BARGE_CONFIRM_MS", "500"))
LISTEN_WHILE_SPEAKING = os.environ.get("AGENT_LISTEN_WHILE_SPEAKING", "1") in ("1", "true", "yes")
ECHO_WORDS_KEPT = 0.6          # share of words that must match our own speech to call it echo
GREET_DELAY_S = float(os.environ.get("AGENT_GREET_DELAY_S", "2.6"))
def _short(text: str, limit: int = 140) -> str:
    """First sentences up to `limit` characters - a greeting, not a brochure."""
    import re as _re
    parts = [p for p in _re.split(r"(?<=[.!?])\s+", (text or "").strip()) if p]
    out = ""
    for p in parts:
        if out and len(out) + len(p) > limit:
            break
        out = (out + " " + p).strip()
    return out or (text or "").strip()


GREETING = os.environ.get("AGENT_GREETING") or _short(
    GLO_GREETING, int(os.environ.get("AGENT_GREETING_CHARS", "230")))

BARGE_GUARD_MS = 400      # our own echo lives here; do not believe speech this early
BARGE_CONFIRM_MS = 300    # sustained speech needed to interrupt

SYSTEM = (
    "You are Gbox, a friendly assistant answering a phone call in Sri Lanka. "
    "ALWAYS reply in Sinhala, spoken register (not literary) - even when the caller speaks "
    "English, or mixes English words into Sinhala. Never answer in English; English input still "
    "gets a Sinhala answer. "
    "Answer in one or two short sentences, like a real phone conversation - never longer. "
    "Start with the answer itself, no preamble. "
    "If you do not know something, say so briefly and offer to help otherwise."
)


def _rms(pcm16: bytes) -> float:
    a = array.array("h")
    a.frombytes(pcm16[: len(pcm16) // 2 * 2])
    if not a:
        return 0.0
    return math.sqrt(sum(v * v for v in a) / len(a)) / 32768.0


def _looks_like_hallucination(text: str, audio_s: float) -> bool:
    """Reject only physically impossible transcripts: a sentence cannot fit in 0.4 s.

    Deliberately conservative - real English from the caller must survive, and script/duration
    alone cannot separate the two (avg_logprob does not either).
    """
    words = len((text or "").split())
    if not words:
        return False
    if audio_s < 0.5 and words >= 3:
        return True
    if audio_s < 1.0 and words >= 6:
        return True
    return False


def _trim_utterance(pcm16: bytes) -> bytes:
    """Drop the silence we would otherwise pay to transcribe.

    Absolute floor (0.006) instead of a relative one: in a quiet room the old threshold sat
    below the line noise, so the tail still reached the recogniser. Leading context is kept
    (100 ms) because a clipped consonant start hurts accuracy; trailing silence is dropped.
    """
    step = 640                       # 20 ms at 16 kHz mono PCM16
    frames = [pcm16[i:i + step] for i in range(0, len(pcm16) - step + 1, step)]
    if not frames:
        return pcm16
    floor = max(0.006, SPEECH_RMS * 0.5)
    keep = [i for i, f in enumerate(frames) if _rms(f) >= floor]
    if not keep:
        return b""                    # nothing voiced at all
    lo = max(0, keep[0] - 5)
    hi = keep[-1] + 1                 # no trailing context
    return b"".join(frames[lo:hi])


def _is_echo(heard: str, saying: str) -> bool:
    """True when a transcription is mostly the sentence we are speaking right now."""
    heard_words = [w for w in re.split(r"\W+", (heard or "").lower()) if len(w) > 2]
    said_words = set(w for w in re.split(r"\W+", (saying or "").lower()) if len(w) > 2)
    if len(heard_words) < 3 or not said_words:
        return False
    hit = sum(1 for w in heard_words if w in said_words)
    return hit / len(heard_words) >= ECHO_WORDS_KEPT


def _chunks(text: str) -> list[str]:
    """Split for speech: short pieces start playing sooner than whole sentences."""
    parts = re.split(r"(?<=[.!?।;])\s+|(?<=,)\s+", text)
    out: list[str] = []
    for p in (x.strip() for x in parts):
        if not p:
            continue
        if len(p) > 140:                     # one very long clause: break at spaces
            words, cur = p.split(), ""
            for w in words:
                if len(cur) + len(w) > 110:
                    out.append(cur)
                    cur = w
                else:
                    cur = (cur + " " + w).strip()
            if cur:
                out.append(cur)
        else:
            out.append(p)
    return out or ([text.strip()] if text.strip() else [])


# Call state reported by the device over the control WebSocket. Audio arrives before the call is
# answered, so packets alone cannot tell us a call started.
_CALL_LIVE: dict[str, bool] = {}
_CALL_EVENTS_SEEN = False


def note_call_event(serial: str, event: str) -> None:
    """Called from the control path; keeps the agent honest about when a call is really up."""
    global _CALL_EVENTS_SEEN
    _CALL_EVENTS_SEEN = True
    if event in ("call.answered", "call.audio_link_up"):
        _CALL_LIVE[serial] = True
    elif event in ("call.ended", "call.audio_link_down"):
        _CALL_LIVE[serial] = False
    log.warning("call state %s: %s -> %s", serial, event, _CALL_LIVE.get(serial))


class Agent:
    CURRENT: "Agent | None" = None

    def __init__(self, send_downlink) -> None:
        Agent.CURRENT = self
        self._send_dl = send_downlink
        self.enabled = bool(LLM_KEY)
        self.frames = collections.deque(maxlen=2000)
        self.lock = threading.Lock()
        self.history: list[dict] = []
        self.busy = False
        self.stats = {"turns": 0, "stt_fail": 0, "llm_fail": 0, "spoken_s": 0.0,
                      "sessions": 0}
        self.last = {"heard": "", "said": ""}
        self.brain = GloBrain()
        self.in_call = False
        self.last_audio = 0.0
        self.spoken_this_call = False
        self.session_id = 0
        self.speaking = False
        self.barge_in = False
        self.last_greeting_ts = 0.0
        self._play_started = 0.0
        self._spoke_until = 0.0
        self._saying = ""
        self._cut = False
        self._self_level = 0.0
        self._barge_ms = 0
        self.omni: OmniBridge | None = None
        self._dl_q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=4000)
        self._voiced_run_ms = 0

    # ---- hot path ----
    def on_uplink(self, serial: str, pcm16: bytes) -> None:
        if not self.enabled or serial != SERIAL:
            return
        self.last_audio = time.time()
        if BACKEND == "realtime":
            # Straight to the model: no buffering, no VAD, no recogniser in parallel.
            if self.omni is not None and self.omni.live:
                # 16 kHz handset -> 24 kHz, the rate this API's "pcm16" means
                self.omni.feed(resample_16k_to_24k(pcm16))
            return
        with self.lock:
            self.frames.append(pcm16)

    def start(self) -> None:
        if not self.enabled:
            log.warning("agent disabled: DEEPSEEK_API_KEY is empty")
            return
        threading.Thread(target=self._loop, name="agent", daemon=True).start()
        if FILLER_TEXT:
            threading.Thread(target=warm_tts_cache, args=(FILLER_TEXT,),
                             name="tts-warm-filler", daemon=True).start()
        if GREETING:
            threading.Thread(target=warm_tts_cache, args=(GREETING,), name="tts-warm",
                             daemon=True).start()
            log.warning("greeting warming into the TTS cache")
        log.warning("agent loop started (stt=%s llm=%s)", STT_URL, LLM_MODEL)

    # ---- turn segmentation ----
    def _loop(self) -> None:
        silence_ms = 0
        utter: list[bytes] = []
        while True:
            time.sleep(FRAME_MS / 1000.0)
            with self.lock:
                pending = list(self.frames)
                self.frames.clear()
            self._session_tick(bool(pending))
            self_muted = (not LISTEN_WHILE_SPEAKING) and (
                self.speaking or (time.time() < self._spoke_until + SELF_MUTE_TAIL_S))
            for frame in pending:
                if self_muted:
                    continue          # it's our own voice coming back
                if self.speaking:
                    # Our playback and the caller are mixed here: unusable for STT. Watch the
                    # level against our own output and stop talking if someone talks over us.
                    level = _rms(frame)
                    if level >= max(BARGE_MIN_RMS, self._self_level * BARGE_FACTOR):
                        self._barge_ms += FRAME_MS
                        if self._barge_ms >= BARGE_CONFIRM_MS and not self._cut:
                            self._cut = True
                            log.warning("barge-in: caller at %.3f vs our %.3f - stopping playback",
                                        level, self._self_level)
                            utter, silence_ms = [], 0
                    else:
                        self._barge_ms = 0
                    continue
                voiced = _rms(frame) >= SPEECH_RMS
                if voiced:
                    utter.append(frame)
                    silence_ms = 0
                    self._voiced_run_ms += FRAME_MS
                    if (BARGE_ENABLED and self.speaking and not self.barge_in
                            and (time.time() - self._play_started) * 1000 > BARGE_GUARD_MS
                            and self._voiced_run_ms >= BARGE_CONFIRM_MS):
                        self.barge_in = True
                        log.warning("barge-in: caller spoke over us, stopping playback")
                else:
                    self._voiced_run_ms = 0
                    if utter:
                        silence_ms += FRAME_MS
                        utter.append(frame)
                if utter and (silence_ms >= END_SIL_MS or len(utter) * FRAME_MS >= MAX_UTTER_MS):
                    speech_ms = len(utter) * FRAME_MS - silence_ms
                    if speech_ms < MIN_SPEECH_MS and len(utter) * FRAME_MS < MAX_UTTER_MS:
                        continue          # too short to be a sentence: keep it and wait
                    if speech_ms >= MIN_SPEECH_MS and not self.busy:
                        audio = b"".join(utter)
                        utter = []
                        silence_ms = 0
                        threading.Thread(target=self._turn, args=(audio,),
                                         daemon=True).start()
                    elif self.busy:
                        # Busy with the previous turn: keep only the most recent 2 s so the buffer
                        # cannot grow without bound (measured: 40 s utterances, 20 s decodes).
                        utter = utter[-100:]
                        silence_ms = 0
                    else:
                        utter = []
                        silence_ms = 0

    def _session_tick(self, got_audio: bool) -> None:
        now = time.time()
        if self.in_call and now - self.last_audio > CALL_GAP_S:
            self.in_call = False
            log.warning("SESSION %d ended (no audio for %.1fs, %d turns total)",
                        self.session_id, now - self.last_audio, self.stats["turns"])
        if got_audio and not self.in_call:
            self.in_call = True
            self.history = []
            self.brain.reset()
            self.spoken_this_call = False
            self.session_id += 1
            self.stats["sessions"] += 1
            self.barge_in = False
            self._cut = False
            self._barge_ms = 0
            log.warning("SESSION %d start: history cleared", self.session_id)
            recent = (now - self.last_greeting_ts) < GREETING_COOLDOWN_S
            if recent:
                log.warning("SESSION %d: no greeting (%.0fs since the last one - treating it as "
                            "a reconnect of the same call)", self.session_id,
                            now - self.last_greeting_ts)
            elif GREETING:
                threading.Thread(target=self._greet, args=(self.session_id,),
                                 daemon=True).start()

    # ---- realtime backend -------------------------------------------------
    def _dl_pump(self) -> None:
        while True:
            chunk = self._dl_q.get()
            if chunk is None:
                return
            if self._uplink_dead():
                continue                      # the line is gone; drop instead of queueing
            self._self_level = 0.7 * self._self_level + 0.3 * _rms(chunk)
            self._send_dl(SERIAL, chunk)
            time.sleep(0.02)

    def _omni_audio(self, pcm24: bytes) -> None:
        """Model speech arriving at 24 kHz -> 16 kHz -> 20 ms downlink chunks."""
        pcm16 = resample_24k_to_16k(pcm24)
        for i in range(0, len(pcm16), 640):
            try:
                self._dl_q.put_nowait(pcm16[i:i + 640])
            except queue.Full:
                return

    def _omni_start(self) -> None:
        if BACKEND != "realtime" or self.omni is not None:
            return
        from .glo_brain import system_prompt
        instructions = system_prompt()
        self.omni = OmniBridge(api_key=LLM_KEY, instructions=instructions,
                               on_audio=self._omni_audio, url=REALTIME_URL,
                               voice=REALTIME_VOICE, greet="menu")
        threading.Thread(target=self._dl_pump, name="omni-dl", daemon=True).start()
        ok = self.omni.start()
        log.warning("realtime backend %s (voice=%s)", "connected" if ok else "FAILED",
                    REALTIME_VOICE)
        if ok:
            self.spoken_this_call = True      # the model greets, not our TTS

    def on_call_end(self) -> None:
        if self.omni is not None:
            self.omni.stop()
            self.omni = None
            log.warning("realtime backend closed")

    def on_call_live(self) -> None:
        """The device reports the call is answered: greet now, once."""
        if self.in_call and time.time() - self.last_audio > CALL_GAP_S:
            # The previous call already ended; do not report it as this one.
            self.in_call = False
        if not self.in_call:
            self.in_call = True
            self.history = []
            self.brain.reset()
            self.spoken_this_call = False
            self.session_id += 1
            log.warning("SESSION %d start (call answered)", self.session_id)
        if BACKEND == "realtime":
            self._omni_start()
            return
        if not self.spoken_this_call and not self.busy:
            threading.Thread(target=self._greet, args=(self.session_id,), daemon=True).start()

    def _greet(self, sid: int) -> None:
        time.sleep(GREET_DELAY_S)
        if self.session_id != sid or self.spoken_this_call or self.busy:
            return
        # A barge-in or cut from the previous call must never silence this greeting: the flag is
        # per-turn state and the greeting does not pass through _turn.
        if _CALL_EVENTS_SEEN and not _CALL_LIVE.get(SERIAL, False):
            # Audio arrived before the call was answered: do not greet into a dead line.
            log.warning("greeting deferred: call not answered yet")
            self.spoken_this_call = False
            return
        if self.barge_in or self._cut:
            log.warning("greeting: cleared stale playback flags (barge=%s cut=%s)",
                        self.barge_in, self._cut)
        self.barge_in = False
        self._cut = False
        self._barge_ms = 0
        self.spoken_this_call = True
        self.last_greeting_ts = time.time()
        log.warning("SESSION %d greeting (text %d chars, uplink age %.1fs)",
                    sid, len(GREETING), time.time() - self.last_audio)
        try:
            self._speak(GREETING)
        except Exception as exc:  # noqa: BLE001
            log.warning("greeting failed: %s", str(exc)[:120])

    # ---- one turn ----
    def _filler_if_slow(self, started: float) -> None:
        """One short phrase if the turn has produced nothing after FILLER_DELAY_S.

        Dead air on a phone line reads as "the line dropped"; the call bot covers it with a
        pre-synthesised phrase, and this is the same trick with the TTS cache behind it.
        """
        time.sleep(FILLER_DELAY_S)
        if self.barge_in or self.speaking or not self.busy:
            return
        try:
            pcm = _tts_16k(FILLER_TEXT)
            if not pcm:
                return
            self._play_raw(pcm)
            log.warning("filler spoken (turn was slow)")
        except Exception as exc:  # noqa: BLE001
            log.warning("filler failed: %s", str(exc)[:100])

    def _uplink_dead(self) -> bool:
        """True when the device has stopped sending: the link is gone, not just quiet."""
        return (time.time() - self.last_audio) > UPLINK_STALL_S

    def _play_raw(self, pcm: bytes) -> None:
        step = 640
        for i in range(0, len(pcm) - step + 1, step):
            if self.barge_in or self._cut:
                log.warning("playback cut (barge=%s cut=%s)", self.barge_in, self._cut)
                return
            if self._uplink_dead():
                log.warning("playback aborted: uplink silent for >%.0fs", UPLINK_STALL_S)
                return
            chunk = pcm[i:i + step]
            self._speak_packets = getattr(self, "_speak_packets", 0) + 1
            self._self_level = 0.7 * self._self_level + 0.3 * _rms(chunk)
            if not self._send_dl(SERIAL, chunk) and not self._dl_fail_logged:
                # send_downlink returns False when the device's UDP address is not known yet
                self._dl_fail_logged = True
                log.warning("downlink NOT accepted: device address unknown (audio dropped)")
            time.sleep(0.02)

    def _turn(self, pcm16: bytes) -> None:
        self.busy = True
        self._cut = False
        if FILLER_TEXT:
            threading.Thread(target=self._filler_if_slow, args=(time.time(),),
                             daemon=True).start()
        # A barge-in flag left set from an earlier interruption must not silence this turn.
        self.barge_in = False
        t0 = time.time()
        try:
            trimmed = _trim_utterance(pcm16)
            audio_s = len(trimmed) / 32000.0
            if audio_s < 0.2:
                log.warning("turn skipped: %.2fs of voice after trimming (no STT call)", audio_s)
                return
            text = self._stt(trimmed)
            if _looks_like_hallucination(text, audio_s):
                log.warning("ignored a hallucination from %.1fs of audio: %s", audio_s, text[:60])
                return
            if not text:
                return
            text = fix_stt(text)
            if self.speaking and _is_echo(text, self._saying):
                log.warning("echo ignored (our own sentence came back): %s", text[:60])
                return
            if self.speaking:
                # A real new utterance: stop the current reply, then answer this one from its
                # first sentence (the streaming path already speaks clause by clause).
                self._cut = True
                log.warning("cut current reply for a new utterance")
            self.last["heard"] = text
            log.warning("AGENT heard (%.2fs stt, %.1fs audio): %s",
                        time.time() - t0, audio_s, text)
            transcript.record(SERIAL, "heard", text, self.session_id)
            spoken: list[str] = []
            first = True
            for piece in self.brain.answer_stream(text, self.history):
                if self.barge_in:
                    break
                if first:
                    log.warning("AGENT first words after %.2fs", time.time() - t0)
                    first = False
                spoken.append(piece)
                self._speak(piece)
            reply = " ".join(spoken).strip()
            if reply:
                self.last["said"] = reply
                log.warning("AGENT says: %s", reply)
                transcript.record(SERIAL, "said", reply, self.session_id)
                self.stats["turns"] += 1
                self.spoken_this_call = True
                self.history += [{"role": "user", "content": text},
                                 {"role": "assistant", "content": reply}]
                self.history = self.history[-12:]
        except Exception as exc:  # noqa: BLE001
            log.warning("agent turn failed: %s: %s", type(exc).__name__, str(exc)[:150])
        finally:
            self.busy = False

    def _stt(self, pcm16: bytes) -> str:
        try:
            req = urllib.request.Request(STT_URL + "/transcribe", data=pcm16,
                                         headers={"Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return (json.loads(resp.read().decode()).get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001
            self.stats["stt_fail"] += 1
            log.warning("stt failed: %s: %s", type(exc).__name__, str(exc)[:120])
            return ""

    def _llm_stream(self, text: str):
        """Yield speakable pieces as the model produces them."""
        msgs = [{"role": "system", "content": SYSTEM}] + self.history[-8:] + [
            {"role": "user", "content": text}]
        body = json.dumps({"model": LLM_MODEL, "messages": msgs, "temperature": 0.5,
                           "max_tokens": 200, "stream": True}).encode()
        req = urllib.request.Request(LLM_URL, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + LLM_KEY})
        buf = ""
        lines = 0
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0]["delta"].get("content") or ""
                    except Exception:  # noqa: BLE001
                        continue
                    lines += 1
                    buf += delta
                    while True:
                        m = re.search(r"[.!?।;]\s+|,\s+", buf)
                        if not m:
                            break
                        piece = buf[: m.end()].strip()
                        buf = buf[m.end():]
                        if piece:
                            yield piece
            if buf.strip():
                yield buf.strip()
            elif lines == 0:
                log.warning("llm returned nothing usable (no content deltas)")
        except Exception as exc:  # noqa: BLE001
            self.stats["llm_fail"] += 1
            log.warning("llm stream failed: %s: %s", type(exc).__name__, str(exc)[:120])

    def _speak(self, text: str) -> None:
        """Play one piece; every 20 ms is a chance for the caller to interrupt."""
        step = 640
        self._saying = text
        self._dl_fail_logged = False
        if len(text) > 60:
            log.warning("speaking %d chars as %d piece(s)", len(text), len(_chunks(text)))
        pieces = [text.strip()] if len(text.strip()) <= 60 else _chunks(text)
        for piece in pieces:
            if self.barge_in:
                log.warning("playback stopped mid-reply")
                return
            pcm = _tts_16k(piece)
            if not pcm:
                continue
            if not self.speaking:
                self.speaking = True
                self._play_started = time.time()
            for i in range(0, len(pcm) - step + 1, step):
                if self.barge_in or self._cut:
                    log.warning("playback stopped mid-reply")
                    self.speaking = False
                    return
                if self._uplink_dead():
                    log.warning("playback aborted: uplink silent for >%.0fs", UPLINK_STALL_S)
                    self.speaking = False
                    return
                self._send_dl(SERIAL, pcm[i:i + step])
                time.sleep(0.02)
            self.stats["spoken_s"] += len(pcm) / 32000.0
        self.speaking = False
        self._spoke_until = time.time()
        if getattr(self, "_speak_packets", 0):
            log.warning("playback done: %d packets sent", self._speak_packets)
            self._speak_packets = 0
        self._self_level = 0.0
        self._barge_ms = 0


TTS_CACHE_DIR = os.environ.get("TTS_CACHE_DIR", "/app/data/tts_cache")


def _cache_file(text: str) -> str:
    import hashlib
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
    return os.path.join(TTS_CACHE_DIR, key + ".pcm")


def warm_tts_cache(text: str) -> bool:
    """Synthesise one phrase now so no caller pays for it later."""
    try:
        pcm = _tts_16k(text)
        return bool(pcm)
    except Exception as exc:  # noqa: BLE001
        log.warning("tts warm failed: %s", str(exc)[:120])
        return False


def _tts_16k(text: str) -> bytes:
    """TTS container -> 16 kHz PCM16 mono (plain-Python resample; no audioop on 3.13)."""
    import io
    import wave

    path = _cache_file(text)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        pass

    req = urllib.request.Request(TTS_URL, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        return b""
    a = array.array("h")
    a.frombytes(raw)
    if ch == 2:
        a = array.array("h", [(a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2)])
    if sr != RATE and len(a):
        step = sr / float(RATE)
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
    pcm = a.tobytes()
    try:
        os.makedirs(TTS_CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(pcm)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("tts cache write failed: %s", str(exc)[:80])
    return pcm
