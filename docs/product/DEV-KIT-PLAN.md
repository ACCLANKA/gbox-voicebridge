# Selling this to software developers — what we would actually be selling

Written from the inside: what the hardware and code can do *today*, what a developer buyer would
demand, and where I would point it. Not a market forecast — I cannot judge demand from here. I
can judge the product.

## 1. The primitive, in one sentence
**The phone line you already own, as an API.** A box beside any handset joins the call over
Bluetooth HFP and streams the audio both ways over a WebSocket to software you control, with call
records, events and a console.

Why that is interesting to developers specifically:
- **No per-minute fees.** Twilio/Vapi/Retell/Bland all bill per minute. Here the minutes belong to
  the user's own SIM plan.
- **No number porting, no SIP trunk, no carrier account.** Pair a phone, call it.
- **Audio can stay local.** Point it at an LLM on the same network and nothing leaves the
  building — the one thing none of the cloud voice-agent platforms can offer.
- **Hackable.** ESP-IDF, open firmware, WebSocket + JSON: no SDK gatekeeping.

## 2. Honest capability matrix against what a developer will demand

| A dev buyer expects | Today |
|---|---|
| Audio both directions, reliably | First proven **on the bench this week**. Still ~10% of frames over a bad link; one real defect fixed today (the uplink kept the *oldest* audio — that was the latency). Not yet reliable. |
| Runs on mains power for hours | **No.** The latest reset was `reset reason: POWERON` — a PC USB port collapsing at Wi-Fi+BT peaks. Physical fix, not a code fix. |
| Works with their phone | **Unknown.** Every handset differs (peer feature bits, WBS, SCO limits, AT quirks). We have a compatibility *problem*, not a compatibility *list*. |
| Claim a device, get a key | **No.** Single-tenant console, one user. No claim flow, no per-developer API keys. |
| Documented wire protocol | **Internally understood**, not written for outsiders. No versioning policy. |
| Quickstart in 60 seconds | **No.** The honest path today is "read our firmware". |
| Call history and transcripts | **Broken** — the console reads a legacy table and shows nothing. Fix queued. |
| Firmware updates | No update channel; rollback is re-flashing by hand. |
| Regulatory | Radio module certification is the vendor's; the *telecom* use — a box that places calls — needs an acceptable-use policy or it becomes a robocall appliance. |

So: **not sellable as a developer product yet** — and the gap is not features, it is *reliability
and documentation*.

## 3. The blocker people underestimate
**Handset compatibility and RF reliability is a testing problem, not a coding problem.** Our own
logs are a catalogue of HFP quirks per phone. No amount of protocol work replaces a matrix of
documented, tested handsets and one honest statement of what is supported. This is also why I
would not launch a general "works with any phone" device: I would launch a **narrowly supported
dev kit** — named handsets, named carriers, stated plainly — and widen it with test results.

## 4. Three buyer profiles, and which I would pick first
1. **QA / testing of phone systems** — teams that regression-test IVRs and call flows, where a
   human currently dials and listens. They buy scripted calls, recordings, transcripts,
   assertions, in CI. Boring, budget-holding, and it needs almost exactly what we are already
   building. **The fastest paying wedge.**
2. **Local/private voice agents** — developers whose audio must not go to a cloud. Loudest market,
   most crowded by cloud players, but they cannot match "audio never leaves your network".
3. **Hobbyist dev-kit builders** — cheapest unit economics, heaviest support per sale. Good for
   visibility, bad for revenue.

Pick (1) for revenue, (2) for story, treat (3) as marketing.

## 5. What we must gate the launch on
1. **One clean call on mains power, both directions, twice in a row** — no resets, no 28 s dead
   stretches. Until that exists nothing else matters.
2. **One documented handset + carrier pair**, tested, with an honest limits page.
3. **A 60-second developer flow:** claim device, get key, open WebSocket, hear your own TTS. If
   that takes an afternoon, there is no product.
4. **A written wire protocol** with a version and a compatibility rule.
5. **The payload work** (mu-law at 8 kHz, then pacing) so a mediocre link degrades into gaps
   rather than scrambled fragments — see `docs/design/UPLINK-RATE.md`.

## 6. Where the money is, and where the moat is
- The **hardware is a commodity** (ESP32 + a Bluetooth module). A kit can carry a healthy margin,
  but kits do not recur and dev-kit buyers are support-hungry.
- The **recurring** value is the software: the cloud bridge, call records, the transcript
  pipeline, the SDK. It is also the only defensible part.
- So: **give the firmware away and sell the experience**, or sell the kit cheaply and charge for
  the cloud tier. Decide which one this is *before* printing a box, because it changes what we
  build next.

## 7. What I would do next, in order
1. Fix the physical setup and get one provably clean call — the gate for everything above.
2. Make the console honest and complete (call history works, transcripts show).
3. Write the developer-facing protocol and a quickstart, and publish the capability matrix with
   its limits stated plainly.
4. Put 5-10 pilot developers on it — starting with the QA/testing buyers, who pay for reliability
   rather than novelty — and let their failures write the roadmap.
