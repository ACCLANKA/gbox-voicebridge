0.6.32 - BEEP OFF, AND TWO WAYS TO HEAR YOURSELF. Three variant images attached; all are CVSD,
same partition table, same offsets, flash at the app offset as usual. Each is self-identifying in
its version string, so the serial log and the console both tell you which one is running.

THE BEEP: it was VB_LOCAL_TONE_SEC=3 - a 1 kHz tone for three seconds at SCO-up. It was a bring-up
aid to prove the audio path end to end, and it has done its job; it is now just noise in the ear.

1) voicebridge-app-cvsd-quiet-0.6.32.bin  -> "0.6.32-quiet"
   No beep. Everything else as normal (cloud audio, no echo). Use this for ordinary calls.

2) voicebridge-app-cvsd-echoloop-0.6.32.bin -> "0.6.32-echoloop"
   No beep, and the BOARD repeats your voice: microphone straight to the earpiece, no cloud in the
   path at all. This answers "does the SCO path carry audio in AND out" on its own.
   Two things to know before you read the logs from this one:
     - it deliberately IGNORES the cloud downlink, so the "dl: fills=... empty=..." line will sit
       at zero. That is the design, not a fault.
     - only the 8 kHz (CVSD) path has the loopback, which is what these variants force anyway.

3) voicebridge-app-cvsd-wsecho-0.6.32.bin -> "0.6.32-wsecho"
   No beep, and the SYSTEM repeats your voice: this appends &echo=1 to the WebSocket URL, and the
   server's bench echo sends the uplink straight back down. This is the end-to-end test.

THE LADDER - and this is why there are two, because we have been guessing which half is at fault:
  flash echoloop -> if you hear yourself clearly, the board is fine and the SCO path is alive both
                    ways. Clean, instant, no network.
  flash wsecho   -> if THAT is choppy while the loopback was clean, the problem is the link, and
                    the dl: line plus the tx counters will say how bad. If both are clean, the
                    audio path is genuinely working for the first time.
Say which one you are on when you report, because they answer different questions.

A BUG I NEARLY SHIPPED, worth knowing since you flash these: VB_LOCAL_ECHO is compiled in main/,
and build-variant.sh only patches main-component knobs that are on its list. Mine was not, so it
fell through to the branch that injects defines into the bt component - which main/ never receives.
The image built cleanly, carried the right version string, and contained NO echo code at all. The
give-away was that it came out byte-identical in size to the no-echo build. The script is fixed and
the knob now lands; but if a bench image ever behaves exactly like the previous one, check the
size before trusting the label.

Note for the record: these are bench variants, not the product image. The product image should
keep the tone OFF and echo OFF, and get its downlink from the brain as designed.
