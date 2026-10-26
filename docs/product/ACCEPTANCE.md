# Acceptance: what "done" means, measurably

Agreed with Hermes on the bench. Each line is falsifiable; "it sounded fine" is not a result until
it is written next to a number.

## The call that closes the project

One call, all of the following true at once:

1. **Board on stable supply** (mains or a powered hub), **beside the router** - not on the desk at
   -86 dBm. The supply change is what makes the POWERON resets interpretable.
2. **The far end hears continuous intelligible speech**, and the caller hears the reply. The
   measurable half: the delivered rate is contiguous at roughly **2 kB/s or better**, which is what
   4-bit ADPCM at 4 kHz or mu-law at a good link both provide.
3. **No POWERON reset during the call** - or, if one occurs, a named cause from the reset reason
   plus the telemetry timeline (which is now persisted, so this needs no USB).
4. **The counters agree with the ear**: no unexplained `gap_max_ms` excursion, `short` ~0, and
   `skip`/`boffmax` showing the backoff working rather than the socket being hammered.

The listener's verdict is the final word. The numbers exist to explain it, not to overrule it.

## What is already proven, so a regression is visible

| property | evidence |
|---|---|
| phone ↔ board path carries audio both ways | echoloop: `gap_max_ms` 0,0,0,0,0 across 25 s |
| the audio starvation was ours, not the radio | lock-spins 2443 -> 58 after the backoff; SCO healthy throughout (pulls 134-140/s, cb_us max 1.9 ms) |
| cloud control path | sessions, lifecycle events, durable delivery across a dead socket, verified in the DB |
| payload codec switch is transparent | mu-law 0x04 decodes to narrowband PCM16; echo path, level meter and recordings unchanged |

## Known-open, with the decision already made

* **Uplink throughput**: ~10-15% of offered frames under SCO. Order of attack is
  placement → mu-law → a codec that fits → and pacing as a waste measure, not an intelligibility
  measure (`docs/design/UPLINK-RATE.md`). The far-end complement is a jitter buffer with
  packet-loss concealment, which is how VoIP sounds continuous over a lossy path.
* **One partial-write teardown per call** on 0.6.33: caused by our own 0 ms send budget, fixed in
  0.6.35 by a 20 ms budget. It is not fixable by "keep the session, drop the frame" - the transport
  loses the byte count, so a half-written frame cannot be distinguished from a clean failure, and
  desyncing the WebSocket stream is worse than a reconnect.
* **Console call history** read the retired `calls` table; fixed to read `call_sessions`. Both
  telemetry history tables had been demo-seeded only - which is why the console's history was never
  real. Fixed.
