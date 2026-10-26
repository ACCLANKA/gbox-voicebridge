# fw 0.6.20 - pip probe, honest drop counters, the transport fix, and the ws:// answer

## 1. The pip probe (Nuwan's preferred test) - `voicebridge-app-cvsd-bench.bin`

Bench mode, cloud-free: a **250 ms pip every 1000 ms for as long as `HF audio state 2` holds**, and the
first pip goes out immediately. Each pip is logged with its index and the time since link-up:

```
W vb_bt: BENCH: 250 ms pip every 1000 ms from now (no cloud involved)
W vb_bt: BENCH: pip #1 at +0 ms since link-up
W vb_bt: BENCH: pip #2 at +1000 ms since link-up
```

So the cadence is both audible and countable - if the log says `+1000, +2000, +3000` and Nuwan hears an
even rhythm, the downlink is continuous; any missing pip index or a stutter is a measurement, not an
impression. This image has the one-shot 3 s tone **off** so there is no ambiguity about what he hears.

## 2. Why a 3 s tone sounds like 2 s

Your hypothesis (a), and it is not our feed. The AG opens its audio route *after* the eSCO link is up, and
we start feeding the moment we see `HF audio state 2`. The signal that actually says "the route is open"
is the **first inbound SCO packet** - the AG cannot send before it can receive. 0.6.20 logs it:

```
W vb_bt: BENCH: first inbound SCO +NNN ms after audio up (route open)
```

That delta *is* the answer, and it is the number to compare against any caller-side observation. There is
no specified delay after `btm_sco_connected` - the AG chooses when to open the route, which is exactly why
the local tone was worth adding. In pip mode it no longer matters: pips continue for the whole call, so
the first one being late costs nothing.

## 3. "2436 downlink drops" - my counter was lying, and it is fixed

It counted **64-byte chunks drained from the stream buffer**, so one discarded 20 ms frame reported as ~10
drops. Two 2.5 s tones therefore produced ~2400 "-drops" that were really on the order of a couple of
hundred frames' worth of dropped bytes. It is now stated in the units that matter:

```
W vb_bt: in-call: heap=... dl_offered=%u dl_drop_frames=%u dl_drop_bytes=%u dl_nolink=%u dl_lost=%u
```

* `dl_offered` - frames the cloud handed us (**expected: 50/s**, one per 20 ms).
* `dl_drop_frames` / `dl_drop_bytes` - buffer was full, oldest audio discarded to keep latency bounded.
* `dl_nolink` - offered with **no SCO up** (the "fed with no link" case you suspected). These are now
  dropped immediately instead of being buffered for a link that is not there.
* `dl_lost` - this frame lost even after draining (previously invisible).

The rates line up, which is the reassurance you wanted for the audio path: the cloud offers 20 ms frames
of 16 kHz PCM16 (640 B, 32 KB/s) and the CVSD path consumes `len * 2` of it per pull, 133 pulls/s = the
same 32 KB/s in and 16 KB/s of 8 kHz PCM16 out to the SCO. A sustained `dl_offered` far above 50/s, or
`dl_nolink` growing during a call, is now visible for what it is - previously both were invisible.

## 4. The cloud link - your asks 1-5, all in this build

**The plain ws:// listener already exists and needs no firmware change.** `make-device-config.py`
*defaults* to `ws://` (`--tls` opts in to wss), and the firmware derives the scheme from `s_cfg.tls` and
only attaches the CA bundle for wss. So:

```
python scripts/make-device-config.py --ssid "<bench-ssid>" --password "<pw>" \
    --host <plain-host> --port <port> --serial GBX-BENCH01 --key <vb_...> --out nvs-plain.bin
```

flash that blob and the board talks plain ws:// with no TLS handshake, no SNTP, no cert. Point
`--host`/`--port` at whatever you expose - a second port on the bench container is a one-line change on
your side and exactly the experiment you ranked first.

**Coex BALANCE during calls** is in this image too: `coex: balance during call (VB_CALL_COEX_PREFER=0)`.
The pip probe is cloud-free, so this capture answers both questions at once without them confounding:
whether the audio path is continuous, and whether the socket survives when Wi-Fi is no longer
deprioritised.

**Do not abort on `write() returned 0` with errno 0 - fixed, but not where you suggested.** I traced it:
`_ws_write()` in `components/tcp_transport/transport_ws.c` calls `esp_transport_poll_write()`, and a
`poll == 0` (select timed out - normal under coexistence pressure) is logged as "Error transport_poll_write"
and returned as 0, which surfaces as `esp_transport_write() returned 0`. **Patch 0009** retries the poll in
50 ms slices until the caller's own timeout budget is spent. I deliberately did *not* retry in the WS
client: `_ws_write` writes the frame **header before the payload**, so a retry up there would re-send a
header and corrupt the stream. Nothing is written before the poll, so this retry is safe.
* Make sure I have this right, because it is the one change that touches a layer you have not seen: if
  the poll times out, we now wait rather than give up.

**Cheaper retries**: the wss connect timeout is 10000 -> **6000** ms (`VB_WS_NET_TIMEOUT_MS`), so a
handshake that cannot complete under airtime pressure fails in 6 s instead of 13-20.

**Your point 5 (garbage fields)**: fixed exactly as you read it - `type=0` is the write path and those
three fields were uninitialised. They are now printed only when plausible; the always-safe part
(`type`, `errno`, free/largest) is on the first line.

## Artifacts

| artifact | policy | bytes | sha256 |
| --- | --- | --- | --- |
| `voicebridge-app.bin` | originate | 1478320 | `c74a67f26cab3f407dc15b5abe71f6f9e776a9f0c2cf8d80867c1bd59a89f7e7` |
| `voicebridge-app-cvsd.bin` | originate | 1474800 | `aaaf590b14abb6a3f060317a8fea32da38f667b87c2823aae218e3766f6f69d2` |
| `voicebridge-app-cvsd-listen.bin` | listen-only, 3 s tone | 1474544 | `9e663cb3474509cb64a54912ecfac26ad4ac3c4ad9ee5d0524945e7daf436e8b` |
| **`voicebridge-app-cvsd-bench.bin`** | **listen + pips + coex BALANCE** | 1474832 | **`dc41803051348add36ecb78face0ed90a5706047cb65f343e0d2e4862c533808`** |

Flash **`-cvsd-bench` = dc418030**.
A build-tooling note, because it cost a cycle: the first bench image came out byte-identical to the
listen one, version string aside - `build-variant.sh` was injecting bench defines into the bt component,
which `main/vb_bt.c` never sees. It now patches the `#define` sites and fails loudly if one is missing;
the markers above were checked in the binary, not assumed.

## WHAT TO REPORT

 1. Pip cadence: even, or gaps? Which pip indices are missing, and is `pip #1 at +0 ms` the first one he
    hears.
 2. `BENCH: first inbound SCO +NNN ms after audio up` - one number, and it settles the 2 s question.
 3. Does the socket survive the call (`coex: balance during call`, no `transport_ws` write failure)? If it
    does not, the new `websocket error:` line verbatim.
 4. A full `in-call:` line with the `dl_*` breakdown so we can see 50 frames/s on the wire.
