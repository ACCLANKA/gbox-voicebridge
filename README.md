# GBox Voice Bridge

Programmable voice bridge by Gigies (Pvt) Ltd. A hardware handset carries a real phone
call; this service receives the audio, transcribes it, decides a reply and speaks it back.
Developers point a device at their own endpoint and build on top.

Sinhala-first, fully self-hosted: local Whisper (CTranslate2), DeepSeek for the reply,
and a VITS-family TTS container. No cloud speech services are used.

## What's in here

| Path | Purpose |
|---|---|
| `api/main.py` | FastAPI app, UDP audio plane (`GBX1` datagrams), device WebSocket edge |
| `api/agent.py` | The call loop: VAD → STT → LLM → TTS → downlink, plus the TTS disk cache |
| `api/routers/` | REST surface: auth, devices, projects, routes, webhooks, toy |
| `api/services/` | Egress legs (SIP/UDP/TCP/HTTP), webhooks, audio sink, RTP/SRTP, detection |
| `api/protocol.py` | Wire format shared with the firmware (`GBX1` up, `GBD1` down) |
| `docs/` | Developer documentation served alongside the API |
| `scripts/` | Operator tooling |
| `config/settings.example.yaml` | Configuration shape (no secrets) |

## Running it

```bash
pip install -r requirements.txt
python -m uvicorn api.main:app --host 0.0.0.0 --port 8080
```

Or with Docker:

```bash
docker build -t gbox .
docker run -p 8080:8080 -v gbox-data:/app/data -e GBOX_SMTP_HOST=... gbox
```

Required environment (all optional at import time, features degrade without them):

| Variable | Used for |
|---|---|
| `DEEPSEEK_API_KEY` / `LLM_API_KEY` | the reply model |
| `STT_URL` | Whisper service, `POST /transcribe` with raw PCM16 |
| `TTS_URLS` | speech synthesis container, `{"text": ...}` → WAV |
| `GBOX_DB_PATH` | SQLite location (default `/app/data/voicebridge.db`) |
| `GBOX_SMTP_*` | password reset and verification mail |
| `TOY_KEY` | shared key for the toy endpoints |

`GET /health` returns `{"status":"ok"}`. Interactive API docs at `/docs`.

## Device protocol

Uplink datagram: `GBX1` + serial (12 bytes) + sequence (u16 LE) + kind (1 byte) + payload.
Audio is 20 ms frames; kind `0x01` is PCM16 at 16 kHz mono (640-byte payload).

Downlink: `GBD1` + kind + PCM16.

Devices also hold a WebSocket to `/v1/device/ws?serial=…&token=…` for control events
(`hello`, `call.ringing`, `call.answered`, `call.ended`, telemetry).

## Measured behaviour (this hardware)

These numbers drove the design and are worth keeping in mind when tuning:

- Whisper-small int8 on 6 vCPUs decodes at roughly **1.2–1.5× realtime**; `STT_THREADS=3`
  is the sweet spot (1 thread 6.8 s, 3 → 3.8 s, 6 → 10.0 s on the same clip).
- The TTS container serves **one request at a time** and bursts to every core, so parallel
  synthesis does not help. One call for a whole reply beat five per-piece calls by **61 %**
  (9.30 s vs 23.59 s).
- A disk cache of synthesised PCM makes repeat phrases nearly free (6.7 s → 0.006 s), but
  it only hits on identical text — generated replies rarely repeat.
- Streaming playback only works when synthesis is faster than realtime; otherwise the
  player starves and the listener hears gaps. Buffer the whole reply when in doubt.

## License

Proprietary. © Gigies (Pvt) Ltd.
