# What should the product send when the link only carries ~10%?

Answer to the question from the third call (106.9 s, 585 frames delivered against 4980 dropped).

## Put the measurement in bytes first
The board offers **16 kB/s**: 8 kHz PCM16 (`vb_bt_uplink_kind()` already halves it for CVSD),
320 B per 20 ms frame, 50/s. Ten percent of that is **~1.6 kB/s ≈ 13 kbps**. A CVSD call is
**64 kbps = 8 kB/s**. So we are asking a ~13 kbps link to carry twice what the codec itself
needs, and offering every frame whether or not it can go.

## The ladder, best first
1. **8-bit companded (µ-law/A-law) at 8 kHz -> 8 kB/s.** Halves the payload and costs *no
   quality at all*: the call is CVSD 8 kHz, so µ-law at the same rate carries the same speech
   bandwidth the SCO link already has. This is the one change that is purely arithmetic.
2. **Pace to the achieved rate, and make gaps *silent* rather than *scattered*.** Today ~10% of
   frames survive at random, so the far end receives fragments of words - which is why Nuwan
   cannot make out his own echo. One clean 20 ms frame followed by a known gap is
   intelligible-ish; six scattered frames out of sixty are not. This needs a sequence/timestamp
   on audio frames so the receiver can insert the right amount of silence.
3. **ADPCM 4-bit at 8 kHz -> 4 kB/s** if 8 kB/s still does not fit. ~30 lines, no dependency.
   (Opus is the right answer below 16 kbps but costs ~100 KB of the 201 KB we just freed.)
4. **Both directions.** The downlink is 16 kHz PCM16 = 32 kB/s today; the echo arrives as 4-18
   frames. 8 kHz µ-law there too, for the same 4x cut.

## What I would NOT do
Sending every 2nd or 3rd **raw** frame. The receiver treats frames as contiguous audio, so that
is time-compressed, disjointed speech - worse than a gap, not better than a codec. The *goal*
(send less, arrive intact) is right; the mechanism has to be 1-3.

## The prerequisite no payload change can substitute for
`reset reason: POWERON` with no panic, assert or backtrace means the supply rail collapsed - a PC
USB port at Wi-Fi+BT peaks. And at -79..-90 dBm with SCO coexistence, no payload trick makes a
64 kbps voice call good. Mains supply, then antenna/position, before anyone claims the audio is
"working".

## Already changed in 0.6.31 (small, and it is a defect not a tuning knob)
The uplink queue kept the **oldest** frames when the writer fell behind (`xQueueSend` with a full
queue drops the newest). Combined with congestion that means the board keeps transmitting the
*past*: latency that never recovers even after the link does - Nuwan's "voice latency". The
uplink now keeps the **newest** (the downlink already did), counted as `ul_ovw` in the 5 s
`dl:` line. The scratch frame is static because that path runs in the SCO receive callback on the
BTC task's small stack.

## The reality check: fractions, not codecs (bench, 2026-10-15)

Measured on hardware, not modelled: under SCO this radio delivers roughly **10-15%** of what the
uplink offers (~16 kB/s offered, ~1.5-2.4 kB/s through, in bursts). The congestion backoff (0.6.33)
stopped that starvation from killing the audio feed; it cannot make the audio intelligible, because
what reaches the far end is fragments of every frame rather than all of a few.

The number that decides intelligibility is not the codec rate. It is the fraction:

    delivered fraction = link rate / offered rate

That re-orders the ladder below by what each step does to the fraction rather than by how many bytes
it saves:

1. **mu-law at 8 kHz** (8 kB/s offered): halves the offer, therefore *doubles* the fraction. Free -
   no quality loss, pure arithmetic. Do it regardless of anything else.
2. **Pacing** (offer no more than the link actually carries): the step that makes audio
   *contiguous*, because it stops spending the link on the 85% of frames that will never arrive.
   Without it, any bitrate sounds like fragments; with it, a lower rate sounds continuous, which is
   what intelligibility actually is.
3. **A real codec at the achievable rate** (4-bit ADPCM: 8 kHz -> 4 kB/s, or 4 kHz -> 2 kB/s): what
   fits inside ~1.5-2.4 kB/s without fragments.

**The order that follows is not the order this document originally implied.** The physical variable
moves the fraction more than any codec can: moving the board beside the router could take 15% to
60-80%, and at that point mu-law alone (8 kB/s offered, 5-6 kB/s delivered) is already enough for
voice. So:

    placement test -> mu-law -> pacing -> ADPCM, stopping as soon as a call is intelligible.

Building ADPCM before the placement result exists would be tuning a payload we may not need. The
"done" bar must therefore be stated in delivered bytes/s (or in a listener's verdict), not in codec
choice - otherwise it is not falsifiable.

## The partial-write teardown (0.6.35)

One partial-write teardown per call was measured on 0.6.33: `Error write payload (0/321 bytes,
budget 0 ms left)` -> `esp_transport_write() returned -1` -> `abort_connection`, costing a ~3.4 s
reconnect. The trigger was ours: a **0 ms** send budget makes tcp_transport poll with no wait, so a
merely *busy* socket is reported as a transport failure.

The requested fix - "keep the session, drop the frame, and count it separately" - is **not safely
available**, and the code says why: the transport returns -1 with the **byte count lost**, so after
a failure nothing can tell whether a partial frame reached the wire, and a half-written frame
desynchronises the WebSocket stream. That is worse than a reconnect, which is why the library
aborts and why we do not patch it.

What 0.6.35 does instead: a **20 ms** send budget (~2 audio frames), so a busy socket becomes
writable and a failure then means the socket is genuinely stuck - at which point the reconnect IS
the honest recovery. Failures are counted apart: `noconn=` (never attempted) and `short=` (the
abort case). `short` should sit at ~0; if it does not, the socket really is dead.
