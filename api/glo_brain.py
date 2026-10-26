"""glo.lk phone assistant on the Gbox platform.

Ported from the WhatsApp call bot: same persona (Sanjaya, glo.lk), same FAQ set, same 85 STT
corrections, same MCP booking tools (real availability and real appointments). What is different
is the transport: audio arrives over UDP from the device instead of PulseAudio, so this module
only has to produce text and let agent.py speak it.

Speed rules:
  * general chat streams (first clause spoken while the rest is generated);
  * booking turns need a tool call first, so they run non-streamed but only when the caller's
    words actually look like a booking/availability/cancel request - everything else stays fast.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.request

log = logging.getLogger("glo")

GLO_DIR = os.environ.get("GLO_DIR", "/app/glo")
if GLO_DIR not in sys.path:
    sys.path.insert(0, GLO_DIR)

LLM_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
LLM_URL = os.environ.get("LLM_URL", "https://api.deepseek.com/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
NO_THINK = os.environ.get("LLM_NO_THINK", "1") == "1"
FALLBACK_URL = os.environ.get("LLM_FALLBACK_URL", "https://api.deepseek.com/chat/completions")
FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "deepseek-chat")
FALLBACK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")


def _is_qwen() -> bool:
    return "aliyuncs" in LLM_URL or LLM_MODEL.startswith("qwen")
# A phone answer, not a brochure. Measured: the TTS speaks Sinhala at ~9 chars/sec, so 260 chars
# is ~28 s of audio - already long; 200 is a comfortable two-sentence reply.
REPLY_MAX_CHARS = int(os.environ.get("GLO_REPLY_MAX_CHARS", "200"))
CLOSER = os.environ.get("GLO_CLOSER", " තව විස්තර ඕන නම් කියන්න.")
MENU_TEXT = ("මට හොඳට ඇහුණේ නැහැ. කෙටියෙන් කියන්න: මිල, ලිපිනය, දොස්තරගේ වේලාවන්, "
             "නැත්නම් වෙන්කිරීමක් - මොකක්ද ඕන?")
GIVEUP_TEXT = ("කණගාටුයි, මට ඔබේ හඬ හොඳට ඇහෙන්නේ නැහැ. කරුණාකර WhatsApp ඔස්සේ "
               "පණිවිඩයක් එවන්න, නැත්නම් පසුව නැවත අමතන්න.")


def _is_apology(text: str) -> bool:
    t = text or ""
    return ("සමාවෙන්න" in t or "තේරුණේ නැ" in t or "ඇහුණේ නැ" in t
            or "ඇහෙන්නේ නැ" in t or "පැහැදිලිව නැ" in t)


def _read(name: str, default: str = "") -> str:
    try:
        with open(os.path.join(GLO_DIR, name), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return default


PROMPT = _read("prompt.txt")
try:
    FAQ = json.loads(_read("faq.json", "[]"))
except ValueError:
    FAQ = []
try:
    LOCALE = json.loads(_read("locale.json", "{}"))
except ValueError:
    LOCALE = {}
FIXES = LOCALE.get("stt_fixes") or {}
GREETING = LOCALE.get("greeting") or (
    "ආයුබෝවන්, ග්ලො ඩොට් එල්කෙ. මම සංජය.")

# longest keys first so "ටී ෂර්ට්" wins over "ෂර්ට්"
_FIX_KEYS = sorted(FIXES, key=len, reverse=True)

BOOKING_WORDS = (
    "book", "booking", "appointment", "available", "availability", "slot", "cancel",
    "reschedule", "doctor", "clinic", "visit",
    "වෙලාව", "දින", "දවස", "හම්බ", "බුක්", "අවුල්", "රෝහල", "ඩොක්ටර්", "පොඩ්ඩක්",
)


def fix_stt(text: str) -> str:
    """Apply the clinic's own corrections (T-shirt -> P-Shot and 84 more)."""
    if not text:
        return text
    out = text
    for wrong in _FIX_KEYS:
        if wrong and wrong in out:
            out = out.replace(wrong, FIXES[wrong])
    return out


SI_DIGITS = {
    "බින්දුව": "0", "බින්දු": "0", "ශුන්‍ය": "0",
    "එක": "1", "එකයි": "1", "දෙක": "2", "දෙකයි": "2", "තුන": "3", "තුනයි": "3",
    "හතර": "4", "හතරයි": "4", "පහ": "5", "පහයි": "5", "හය": "6", "හයයි": "6",
    "හත": "7", "හතයි": "7", "අට": "8", "අටයි": "8", "නවය": "9", "නවයයි": "9",
}
EN_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def _digits_of(text: str) -> str:
    """Pull a digit string out of speech, however the recogniser spelled it."""
    low = (text or "").lower()
    for word, digit in list(SI_DIGITS.items()) + list(EN_DIGITS.items()):
        low = re.sub(r"(?<![\w])%s(?![\w])" % re.escape(word), digit, low)
    return re.sub(r"\D", "", low)


def extract_phone(text: str) -> str:
    """A Sri Lankan mobile number as spoken: 10 digits starting 07 (or 9 without the 0)."""
    d = _digits_of(text)
    m = re.search(r"0?7\d{8}", d)
    if m:
        num = m.group(0)
        return num if num.startswith("0") else "0" + num
    return ""


def _chunks_local(text: str):
    return [p for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]


def want_booking(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in BOOKING_WORDS)


def system_prompt() -> str:
    today = time.strftime("%Y-%m-%d (%A)")
    faq = "\n".join("- %s: %s" % (e.get("id"), (e.get("answer") or "").strip())
                    for e in FAQ if e.get("answer"))
    return (PROMPT.strip()
            + "\n\n=== TODAY ===\n" + today
            + "\n\n=== APPROVED ANSWERS (use these facts verbatim when asked) ===\n" + faq
            + "\n\n=== CALL RULES ===\n"
              "- Reply in Sinhala script only, spoken register, at most two short sentences.\n"
              "- NEVER state availability, slots or appointment times from memory: call\n"
              "  check_availability with the date in YYYY-MM-DD whenever a day is mentioned.\n"
              "- NEVER confirm an appointment without calling create_booking first, and read\n"
              "  the reference back to the caller.\n"
              "- If the caller gives a name, phone, date or time, confirm it back once.")


TOOLS = [
    {"type": "function", "function": {
        "name": "check_availability", "description": "Free appointment slots for a date (YYYY-MM-DD).",
        "parameters": {"type": "object", "properties": {"date": {"type": "string"}},
                       "required": ["date"]}}},
    {"type": "function", "function": {
        "name": "create_booking",
        "description": "Create a real appointment once name, phone, date and time are known.",
        "parameters": {"type": "object",
                       "properties": {"name": {"type": "string"}, "phone": {"type": "string"},
                                      "date": {"type": "string"}, "start_time": {"type": "string"}},
                       "required": ["name", "phone", "date", "start_time"]}}},
    {"type": "function", "function": {
        "name": "get_booking", "description": "Look up a booking by reference or phone.",
        "parameters": {"type": "object",
                       "properties": {"ref": {"type": "string"}, "phone": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "cancel_booking", "description": "Cancel a booking by reference.",
        "parameters": {"type": "object", "properties": {"ref": {"type": "string"}},
                       "required": ["ref"]}}},
]


def _run_tool(name: str, args: dict) -> str:
    try:
        import mcp_glo as m
        if name == "check_availability":
            rows = m.free_slots(args.get("date", ""))
            return json.dumps(rows[:8], ensure_ascii=False)
        if name == "create_booking":
            res = m.create_booking(args["name"], args["phone"], args["date"],
                                   args["start_time"])
            return json.dumps(res, ensure_ascii=False)
        if name == "get_booking":
            return json.dumps(m.get_booking(ref=args.get("ref"), phone=args.get("phone")),
                              ensure_ascii=False)
        if name == "cancel_booking":
            return json.dumps(m.cancel_booking(args["ref"]), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("tool %s failed: %s: %s", name, type(exc).__name__, str(exc)[:120])
        return json.dumps({"error": str(exc)[:160]})
    return json.dumps({"error": "unknown tool"})


class GloBrain:
    def __init__(self) -> None:
        self.history: list[dict] = []
        self.slots: dict[str, str] = {}
        self.booking_mode = False
        self.fails = 0

    # ---------------------------------------------------------------- public
    def _call_memory(self, history: list[dict]) -> str:
        """Compress the earlier part of the call into a few lines.

        The last two exchanges stay verbatim in the message list; everything before them becomes
        a gist plus the slots, which is what the model actually needs to stay consistent.
        """
        turns = len(history) // 2
        if turns <= 2:
            return ""
        early = [m.get("content", "") for m in history[:-4] if m.get("role") == "user"]
        gist = " / ".join(early[-4:])[:280]
        facts = ", ".join("%s=%s" % (k, v) for k, v in self.slots.items() if v)
        out = "=== EARLIER IN THIS CALL (%d turns so far) ===" % turns
        if gist:
            out += "\ncaller said: " + gist
        if facts:
            out += "\ncaptured: " + facts
        return out

    def answer_stream(self, text: str, history: list[dict]):
        """Yield speakable pieces. Falls back to a plain reply if anything fails."""
        text = fix_stt(text)
        phone = extract_phone(text)
        if phone and self.slots.get("phone") != phone:
            self.slots["phone"] = phone
            log.warning("phone captured: %s", phone)
        sys_prompt = system_prompt()
        if self.slots.get("phone"):
            sys_prompt += "\n\n=== CAPTURED THIS CALL ===\nphone: %s\n" % self.slots["phone"]
        msgs = [{"role": "system", "content": sys_prompt}]
        mem = self._call_memory(history)
        if mem:
            msgs.append({"role": "system", "content": mem})
        msgs += history[-4:] + [{"role": "user", "content": text}]
        self.booking_mode = True
        yield from self._guarded(self._with_tools(msgs), text)

    def reset(self) -> None:
        """New call: forget the slots and the booking context."""
        self.slots.clear()
        self.booking_mode = False
        self.fails = 0

    def _guarded(self, gen, heard: str):
        """Speak the reply, but decide on its opening first.

        * an apology is replaced by the menu (first failure) or the give-up line (second), never
          spoken alongside it;
        * a claimed phone number with nothing captured is corrected before it is believed.
        """
        buf: list[str] = []
        decided = False
        said: list[str] = []

        for piece in gen:
            if decided:
                said.append(piece)
                yield piece
                continue
            buf.append(piece)
            opening = " ".join(buf)
            if len(opening) < 45 and len(buf) < 3:
                continue                      # too little to judge
            decided = True
            if _is_apology(opening):
                self.fails += 1
                log.warning("understanding failed (%d in a row) - apology replaced", self.fails)
                text = MENU_TEXT if self.fails == 1 else GIVEUP_TEXT
                for p in _chunks_local(text):
                    yield p
                return
            for p in buf:
                said.append(p)
                yield p
            buf = []

        if not decided and buf:                # generator ended before we could judge
            opening = " ".join(buf)
            if _is_apology(opening):
                self.fails += 1
                log.warning("understanding failed (%d in a row) - apology replaced", self.fails)
                text = MENU_TEXT if self.fails == 1 else GIVEUP_TEXT
                for p in _chunks_local(text):
                    yield p
                return
            for p in buf:
                said.append(p)
                yield p

        reply = " ".join(said)
        if reply and not _is_apology(reply):
            self.fails = 0

        claimed = ("අංකය" in reply and ("තේරුණා" in reply or "සුරකින්" in reply))
        if claimed and not self.slots.get("phone"):
            log.warning("guard: model claimed the number but none was captured - correcting")
            for p in _chunks_local(
                    "සමාවෙන්න, අංකය තවම හරියට ඇහුණේ නැහැ. "
                    "දුරකථන අංකය ඉලක්කම් එකින් එක වෙන් කරලා කියන්න."):
                yield p

    # ---------------------------------------------------------------- paths
    def _with_tools(self, msgs):
        try:
            _t0 = time.time()
            out = self._call(msgs, tools=TOOLS, stream=False, max_tokens=220)
            log.warning("llm: %d msgs, %d chars, %.2fs, tools=on",
                        len(msgs), sum(len(m.get("content") or "") for m in msgs),
                        time.time() - _t0)
        except Exception as exc:  # noqa: BLE001
            log.warning("tool round failed: %s", str(exc)[:120])
            yield from self._stream(msgs)
            return
        calls = out.get("tool_calls") or []
        if not calls:
            content = (out.get("content") or "").strip()
            if content:
                yield content
            return
        msgs.append({"role": "assistant", "content": "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            log.warning("tool call: %s %s", name, json.dumps(args, ensure_ascii=False)[:120])
            result = _run_tool(name, args)
            msgs.append({"role": "tool", "tool_call_id": call.get("id", name),
                         "content": result})
        yield from self._stream(msgs)

    def _stream(self, msgs):
        _body = {"model": LLM_MODEL, "messages": msgs, "temperature": 0.6,
                 "max_tokens": 220, "stream": True}
        if _is_qwen() and NO_THINK:
            _body["enable_thinking"] = False
        body = json.dumps(_body).encode()
        req = urllib.request.Request(LLM_URL, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + LLM_KEY})
        buf = ""
        produced = 0
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
                buf += delta
                if produced + len(buf) > REPLY_MAX_CHARS:
                    # cut at the last sentence boundary we already have
                    keep = buf
                    for sep in (". ", "? ", "! ", ", "):
                        idx = keep.rfind(sep)
                        if idx > 0:
                            keep = keep[:idx + len(sep)]
                            break
                    if keep.strip():
                        yield keep.strip() + CLOSER
                    return
                while True:
                    m = re.search(r"[.!?।;]\s+|,\s+", buf)
                    if not m:
                        break
                    piece = buf[:m.end()].strip()
                    buf = buf[m.end():]
                    if piece:
                        produced += len(piece)
                        yield piece
        if buf.strip() and produced < REPLY_MAX_CHARS:
            room = REPLY_MAX_CHARS - produced
            piece = buf.strip()[:max(0, room)]
            cut = piece.rfind(" ")
            if cut > 20:
                piece = piece[:cut]
            if piece:
                yield piece

    def _call(self, msgs, tools=None, stream=False, max_tokens=220) -> dict:
        body = {"model": LLM_MODEL, "messages": msgs, "temperature": 0.0,
                "max_tokens": max_tokens, "stream": False}
        if tools:
            body["tools"] = tools
        if _is_qwen() and NO_THINK:
            body["enable_thinking"] = False       # without this: 10-13 s per turn
        try:
            req = urllib.request.Request(LLM_URL, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + LLM_KEY})
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            if not FALLBACK_KEY or LLM_URL == FALLBACK_URL:
                raise
            log.warning("primary llm failed (%s) - falling back to %s",
                        str(exc)[:80], FALLBACK_MODEL)
            body["model"] = FALLBACK_MODEL
            body.pop("enable_thinking", None)
            req = urllib.request.Request(FALLBACK_URL, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + FALLBACK_KEY})
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode())
        msg = data["choices"][0]["message"]
        return {"content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}
