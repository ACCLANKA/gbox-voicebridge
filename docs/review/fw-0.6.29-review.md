# Firmware 0.6.29 — root cause, review response, test plan

Deliverable for the playback investigation. Cumulative patch 0.6.26 -> 0.6.29: `docs/review/fw-0.6.29.patch`.

## 1. Root cause: confirmed defects vs hypotheses

Confirmed and fixed (each verified in the code before editing):
1. **Reboot at call teardown** (0.6.27). `vb_reboot_allowed()` deferred the reboot but both failure
   counters kept climbing under the call; the moment it ended the banked debt fired
   `esp_restart()`. Evidence: `uptime_s=0` exactly at teardown, twice, serial re-enumerating.
2. **Bluetooth never reconnected.** The `..._DISCONNECTED` branch cleared the peer BDA, so the
   address needed to page the phone was thrown away. Evidence: code + "bluetooth disconnected
   from phone, modify to connect when connection loss".
3. **Watchdog froze the socket restart escalation during a call** (my 0.6.28 regression).
   `s_ws_down_since` drives BOTH the 30 s stop/start escalation and the reboot, and I reset it for
   the whole call: a wedged socket was never restarted until the call ended.
4. **16 kHz tone tail skipped for full frames** (my 0.6.28 regression). The call sat inside
   `if (got < len)`, so it ran only for a *partial* read — the opposite of the common case.
5. **Peer lost across a reboot.** `s_peer_bda`/`s_have_peer` are RAM, so the 0.6.28 reconnect could
   not fire after a reboot — the exact case its comment claimed it handled.
6. **SCO retry could target a dead link**, because (2) changed what `s_have_peer` means.
7. **The beep click.** The pip/tone is cut mid-cycle when downlink audio arrives: a step
   discontinuity, not a pitch error (rates checked: 16000 on the 16 kHz buffer, 8000 after CVSD
   decimation). In this image the audible beep is the 3 s SCO-up tone.
8. **`double sin()` inside the SCO callback.** The FPU is single precision, so every call is
   software-emulated, ~133/s in a real-time path.

Hypotheses NOT confirmed, and deliberately NOT "fixed":
- **Buffer starvation / incomplete-frame handling as the cause.** The producer and consumer agree:
  16 kHz PCM16 in, `need = len * 2` out, pair-average decimation to 8 kHz for CVSD, non-blocking
  receive (0 ticks) so the callback never waits, drop-oldest on overflow (counted), zeros only for
  the genuine shortfall. No size, rate or byte-count mismatch found. Instrumented instead of
  guessed — see the `dl:` line.
- **Latency.** `DL_BYTES` 4096 = 128 ms at 16 kHz is a deliberate jitter allowance, and the local
  pip sounds only when the queue is empty. Not touched without a measurement.
- **Resampling.** The 16k->8k path already averages sample pairs: the correct ratio with a crude
  (2-tap) anti-alias. A half-band FIR would sound better; that is a quality change, not a defect,
  and is queued behind the measurement that justifies it.

## 2. Response to the external review

| # | Point | Verdict | Action |
|---|-------|---------|--------|
| 1 | Watchdog keeps a dead WS dead | **Correct** — the diff alone could not show that the reset hit the down clock as well as the counter | Counter only; clock re-based on the call->no-call edge |
| 2 | Reconnect peer does not survive a reboot | **Correct** | Recovered from the bond list at init (`esp_bt_gap_get_bond_device_list`, verified at esp_gap_bt_api.h:763) |
| 3 | 16 kHz tail inside `if (got < len)` | **Correct** | Moved out; both paths now one `tone_render(..., add)` |
| 4 | `s_link_retry_us` int64 can tear | **Correct** | `TickType_t`, wrap-safe compare |
| 5 | `s_tone_env`/`s_tone_phase` concurrent | **Checked: single context** — only `tone_render`, only from `hf_send_fill` (the SCO send callback) | No lock; invariant documented instead (they are deliberately NOT volatile) |
| 6 | `s_have_peer` no longer means connected | **Correct** | All 6 uses audited; SCO retry now needs `s_link_up` too |
| 7 | Overlapping reconnect, no backoff | **Correct** | Tracks the stack's CONNECTING state; 5/10/30 s backoff |
| 8 | `tone_tail_add` buffer size / silent skip | **Correct** | Scratch buffer removed entirely: adds in place, no guard to trip |
| 9 | Envelope stale if audio drops | **Correct** | Reset on link loss; counters reset per call |
| 10 | `sin()` on doubles | **Correct** | `float` + `sinf` |
| 11 | `ESP_LOGW()` format string with `+` | **No `+` present** — adjacent string literals only, which is valid C (the build confirms) | none |
| — | `vb_bt_call_active()` declared where main.c can see it | Already called from main.c before this change | verified by the build |

## 3. Files inspected
- `firmware/main/vb_bt.c` — whole audio path: `vb_bt_feed_downlink` (producer), `hf_send_fill`
  (consumer), tone path, HFP event handler, `hf_maint_task`.
- `firmware/main/main.c` — `ws_watchdog_task`, `vb_reboot_allowed`.
- `firmware/main/voice_protocol.h` — frame sizes and rates.
- `api/protocol.py`, `api/services/detection.py`, `api/routers/device_ws.py` — server format,
  framing, call session lifecycle.
- `idf53/.../esp_gap_bt_api.h` — bonded-list API availability.

## 4. Files changed
`firmware/main/main.c` (watchdog, version) and `firmware/main/vb_bt.c` (tone path, reconnect,
bonded-peer recovery, diagnostics). Diff: `docs/review/fw-0.6.29.patch`.

## 5. Server audio format (confirmed, unchanged)
`SAMPLE_RATE 16000`, mono signed PCM16 little-endian, one kind byte (0x01 PCM16, 0x03 PCM16_8K),
`FRAME_SAMPLES 320` (20 ms), transmitted per frame as it is produced. The board reads `s_dl` as
16 kHz and decimates x2 for CVSD — the ratio is right. No server change made: no measurement
implicates the server yet, and `gap_max_ms` is the instrument that would say so.

## 6. Build
```
. /home/openhands/workspace/idf53/export.sh
bash scripts/build-variant.sh benchwifi VB_CALL_COEX_PREFER=2
```
Result: exit 0, `voicebridge-app-cvsd-benchwifi-0.6.29.bin`, version string confirmed in the
image. Two pre-existing warnings (`s_pip_start_ms`/`s_pip_logged` unused because `VB_LOCAL_PIP`
is off in this variant). **The app partition is 97% full** — a real constraint on further growth.

## 7. Test plan (needs the board)
One call each, watching the new lines:
- **short speech**, **long speech** (60 s+), **silence**, **rapid responses** back to back.
- **Wi-Fi interruption**: pull the AP / walk away mid-call; expect WS restarts to keep escalating
  *during* the call, and no reset at the moment the call ends.
- **Bluetooth reconnect**: walk the phone out of range and back; expect
  `HF link down - reconnect attempt N ... next in 5000/10000/30000 ms` and recovery without
  re-pairing. Then **power-cycle the board** and confirm `bonded peer recovered: ...` and that the
  link comes back unprompted.
- Read at the end of each call: `dl: fills/partial/empty/hwm/gap_max_ms` — `empty` climbing means
  starvation (ours), `gap_max_ms` large means the data was late (not ours), `hwm` near
  `4096` means the queue was running full (latency, not loss).

## 8. Flash and rollback
Flash `voicebridge-app-cvsd-benchwifi-0.6.29.bin` at 0x0 with the project's usual esptool
invocation (`-cvsd-benchwifi` image). Rollback: re-flash the previous image (0.6.28 or 0.6.26) —
the change is firmware-only, no NVS or server migration, so rollback is a flash.

## 9. Risks and what is NOT verified
- **Nothing here has run on hardware.** Every fix is verified by reading the code and by a clean
  build; none is verified by listening.
- Whether a phone that dropped the SLC itself will accept a page from the AG side is unknown
  (some handsets only accept the AG-initiated connection once).
- "Most recently bonded device" assumes one phone; with several, it could pick the wrong one.
- The counters are free-running `uint32_t`: fine for hours, they wrap far beyond a call.
- The 2-tap decimation's aliasing is real and unmeasured.
diff --git a/firmware/main/main.c b/firmware/main/main.c
index cae5414..371e04e 100644
--- a/firmware/main/main.c
+++ b/firmware/main/main.c
@@ -41,7 +41,7 @@
 #include "voice_protocol.h"
 
 #ifndef FW_VERSION
-#define FW_VERSION "0.6.26"
+#define FW_VERSION "0.6.29"
 #endif
 
 static const char *TAG = "vb";
@@ -70,6 +70,9 @@ static EventGroupHandle_t s_wifi_events;
 static volatile bool s_ws_up;
 static volatile TickType_t s_ws_down_since;
 static volatile int s_ws_restarts;
+/* Last tick's call state, so the watchdog can tell "a call just ended" from "no call has
+ * been running" - see the reboot-debt comment in ws_watchdog_task. */
+static volatile bool s_ws_call_was_active;
 static volatile uint32_t s_tx_drops;
 static volatile uint32_t s_tx_ok;           /* frames that actually left the board */
 static volatile uint32_t s_tx_fail_streak;  /* consecutive failed sends */
@@ -931,6 +934,25 @@ static void ws_watchdog_task(void *arg)
         bool conn = esp_websocket_client_is_connected(s_ws);
         uint32_t idle_ms = (uint32_t)((now - s_ws_last_ok) * portTICK_PERIOD_MS);
 
+        /* A reboot debt must be earned after a call, never banked during one: these counters
+         * keep running while a call is up (only the reboot itself is deferred by
+         * vb_reboot_allowed), so a flaky link during a call rebooted the board the instant
+         * the call ended - uptime_s=0 exactly at teardown, serial re-enumerating under the
+         * capture.
+         *
+         * Only the COUNTER is neutralised, never the down clock: the down clock drives the
+         * stop/start escalation as well as the reboot, and freezing it left a wedged socket
+         * un-restarted for the whole call - a dead link stays dead until the call ends. The
+         * clock is re-based on the call's last tick instead, so the grace period starts when
+         * the call does end rather than expiring underneath it. */
+        bool call_active = vb_bt_call_active();
+        if (call_active) {
+            s_ws_restarts = 0;
+        } else if (s_ws_call_was_active) {
+            s_ws_down_since = now;
+        }
+        s_ws_call_was_active = call_active;
+
         if (conn && idle_ms < WS_STALE_MS) {
             s_ws_up = true;             /* healthy */
             s_ws_down_since = now;      /* keep the down clock fresh */
diff --git a/firmware/main/vb_bt.c b/firmware/main/vb_bt.c
index 75b2c74..25b8efd 100644
--- a/firmware/main/vb_bt.c
+++ b/firmware/main/vb_bt.c
@@ -93,7 +93,13 @@ static volatile uint32_t s_sco_in_cbs;   /* app-level inbound SCO deliveries */
 #define VB_PIP_MS 250
 #define VB_PIP_PERIOD_MS 1000
 static int64_t s_tone_until_ms;
-static double s_tone_phase;
+static float s_tone_phase;
+/* Envelope, 0..1. A pip cut mid-cycle is a click, and the gate closes the instant a
+ * downlink frame arrives, so that is the common case rather than the rare one.
+ * Both of these are touched only from the SCO send callback (tone_render's only caller),
+ * which is a single task, so they need no lock - keep it that way. */
+static float s_tone_env;
+#define TONE_RAMP_SAMPLES 24        /* ~3 ms at 8 kHz, ~1.5 ms at 16 kHz */
 static int64_t s_pip_start_ms;      /* set when the SCO audio link comes up */
 static uint32_t s_pip_logged = 0xffffffffu;
 static int64_t s_audio_up_ms;
@@ -181,17 +187,47 @@ static void pip_tick(void)
 #endif
 }
 
-/* Fill n PCM16 samples with a 1 kHz sine. Deliberately a sine and not a square: a square's
- * harmonics make a rate error (samples handed over at the wrong cadence) sound like a
- * buzzier tone rather than a clean shift, which is exactly the ambiguity Hermes asked us to
- * remove. The phase carries across calls, so a gap in the fills is audible as a gap. */
-static void tone_fill(int16_t *out, uint32_t n, uint32_t rate)
+/* Render n samples of the 1 kHz pip into out[] (`add` false) or on top of what the caller
+ * already wrote there (`add` true). One implementation for both, because the two drifted
+ * apart the moment there were two - the 16 kHz tail was skipped for full frames.
+ *
+ * Deliberately a sine and not a square: a square's harmonics make a rate error (samples
+ * handed over at the wrong cadence) sound like a buzzier tone rather than a clean shift.
+ * The phase carries across calls, so a gap in the fills is audible as a gap.
+ *
+ * float/sinf, NOT double/sin: this board's FPU is single precision, so every double sin()
+ * here is software-emulated - and this runs inside the SCO send callback, ~133 times a
+ * second, twice per sample while a tail fades. Nothing here needs double precision. */
+static void tone_render(int16_t *out, uint32_t n, uint32_t rate, bool sounding, bool add)
 {
-    const double two_pi = 6.283185307179586;
-    const double step = two_pi * 1000.0 / (double)rate;
-    const double amp = (double)VB_TONE_AMPLITUDE;   /* level standard, see voice_protocol.h */
+    const float two_pi = 6.2831853f;
+    const float step = two_pi * 1000.0f / (float)rate;
+    const float amp = (float)VB_TONE_AMPLITUDE;   /* level standard, see voice_protocol.h */
+    const float d_env = 1.0f / (float)TONE_RAMP_SAMPLES;
     for (uint32_t i = 0; i < n; i++) {
-        out[i] = (int16_t)(amp * sin(s_tone_phase));
+        /* Ramp rather than switch: the pip gate closes on the first downlink frame, so a
+         * pip normally ENDS mid-cycle and the old code simply stopped the sine between two
+         * samples - an audible click, and the "some beeps sound wrong" report. */
+        if (sounding) {
+            if (s_tone_env < 1.0f) {
+                s_tone_env += d_env;
+                if (s_tone_env > 1.0f) {
+                    s_tone_env = 1.0f;
+                }
+            }
+        } else if (s_tone_env > 0.0f) {
+            s_tone_env -= d_env;
+            if (s_tone_env < 0.0f) {
+                s_tone_env = 0.0f;
+            }
+        }
+        const float s = amp * s_tone_env * sinf(s_tone_phase);
+        if (add) {
+            int v = (int)out[i] + (int)s;
+            out[i] = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
+        } else {
+            out[i] = (int16_t)s;
+        }
         s_tone_phase += step;
         if (s_tone_phase > two_pi) {
             s_tone_phase -= two_pi;
@@ -199,6 +235,17 @@ static void tone_fill(int16_t *out, uint32_t n, uint32_t rate)
     }
 }
 
+/* Fade out a pip the gate has just cut short, on top of the downlink the caller has already
+ * written into the buffer. No scratch buffer on the SCO callback's stack: tone_render adds
+ * in place, so there is also no size guard that can silently skip the fade. */
+static void tone_tail_add(int16_t *out, uint32_t out_n, uint32_t rate)
+{
+    if (s_tone_env <= 0.0f) {
+        return;
+    }
+    tone_render(out, out_n, rate, false, true);
+}
+
 /* Demand-driven SCO feed. esp_hf_client_outgoing_data_ready() is the only thing
  * that makes Bluedroid pull PCM from hf_send_cb(). Calling it once per downlink
  * arrival slaved the feed to the network's cadence (choppy audio); calling it at
@@ -225,17 +272,54 @@ static void tone_fill(int16_t *out, uint32_t n, uint32_t rate)
  * BDA, and the SLC event is the last place it is handed to us, so remember it. */
 static esp_bd_addr_t s_peer_bda;
 static volatile bool s_have_peer;
+/* SLC (the HF link to the phone) - distinct from s_audio_up, which is the SCO audio leg.
+ * The link can be down while the audio leg still looks up, and reconnecting needs the link. */
+static volatile bool s_link_up;
+/* A page is in flight. esp_hf_client_connect() has a page timeout of about 5 s, so a fixed
+ * 5 s cadence can start the next attempt on top of the previous one - track the state the
+ * stack reports instead of guessing from the clock. */
+static volatile bool s_link_connecting;
+static uint32_t s_link_retries;
+/* TickType_t (32-bit here), not int64: this is written by the BT event callback and read by
+ * hf_maint_task, and a 64-bit read can tear on a 32-bit core. Compared wrap-safe. */
+static TickType_t s_link_retry_tick;
+#define VB_HF_RECONNECT_MS 5000         /* first re-page */
+#define VB_HF_RECONNECT_MAX_MS 30000    /* cap once the phone is clearly not answering */
+#define VB_MAX_BONDED 4
 static volatile bool s_sco_pull_pending;
 static volatile uint32_t s_sco_pull_stuck;
 static SemaphoreHandle_t s_sco_tick;
 static esp_timer_handle_t s_sco_timer;
 
+/* Playback-path diagnostics. Counters only, reported every 5 s by hf_maint_task - never per
+ * frame. They exist to separate the three things that all sound like "the audio drags":
+ * data that never arrived (gap_max_ms), data that arrived but was dropped for space
+ * (dl_drop_*), and the SCO asking for samples the queue did not have (partial/empty). */
+static uint32_t s_fill_calls;      /* SCO fills served */
+static uint32_t s_fill_partial;    /* ...where the queue held less than the SCO asked for */
+static uint32_t s_fill_empty;      /* ...where it held nothing at all */
+static uint32_t s_dl_hwm;          /* deepest the queue ever got, against DL_BYTES */
+static uint32_t s_dl_gap_max_ms;   /* longest gap between downlink arrivals */
+static int64_t s_dl_last_us;
+
 void vb_bt_feed_downlink(const uint8_t *pcm16, size_t bytes)
 {
     if (!s_dl || !pcm16 || !bytes) {
         return;
     }
     s_dl_offered++;
+    /* An arrival gap is a server or network gap, not a playback gap: measuring it here is
+     * what tells the two apart before anyone touches a buffer size. */
+    {
+        int64_t now_us = esp_timer_get_time();
+        if (s_dl_last_us) {
+            uint32_t gap_ms = (uint32_t)((now_us - s_dl_last_us) / 1000);
+            if (gap_ms > s_dl_gap_max_ms) {
+                s_dl_gap_max_ms = gap_ms;
+            }
+        }
+        s_dl_last_us = now_us;
+    }
     if (!s_audio_up) {
         s_dl_nolink++;
         return;
@@ -396,8 +480,21 @@ static uint32_t hf_send_fill(uint8_t *buf, uint32_t len)
     }
     if (s_rate >= 16000) {
         size_t got = xStreamBufferReceive(s_dl, buf, len, 0);
+        s_fill_calls++;
+        if (got < len) {
+            s_fill_partial++;
+        }
+        if (got == 0) {
+            s_fill_empty++;
+        }
+        {
+            size_t q = xStreamBufferBytesAvailable(s_dl);
+            if (q > s_dl_hwm) {
+                s_dl_hwm = (uint32_t)q;
+            }
+        }
 #if VB_LOCAL_PIP_MIX
-        bool pip = tone_active();
+        bool pip = tone_active() || s_tone_env > 0.0;   /* keep filling while the tail fades */
 #else
         bool pip = tone_active() && got == 0;
 #endif
@@ -406,26 +503,25 @@ static uint32_t hf_send_fill(uint8_t *buf, uint32_t len)
             memset(buf + got, 0, len - got);   /* the mix below adds the pip */
 #else
             if (pip && got == 0) {
-                tone_fill((int16_t *)buf, len / 2, 16000);
+                tone_render((int16_t *)buf, len / 2, 16000, true, false);
                 s_pip_samples += len / 2;
             } else {
                 memset(buf + got, 0, len - got);
             }
 #endif
         }
+        /* Outside the got < len guard: the gate cuts a pip short when a FULL frame arrives
+         * too, which is the common case, and an inside-the-guard tail is what made the
+         * 16 kHz path click where the 8 kHz path did not. */
+        if (!pip) {
+            tone_tail_add((int16_t *)buf, len / 2, 16000);
+        }
 #if VB_LOCAL_PIP_MIX
         if (pip) {
-            int16_t mix[VB_FRAME_SAMPLES];
+            int16_t *o = (int16_t *)buf;
             uint32_t n = len / 2;
-            if (n <= sizeof mix / sizeof mix[0]) {
-                int16_t *o = (int16_t *)buf;
-                tone_fill(mix, n, 16000);
-                s_pip_samples += n;
-                for (uint32_t i = 0; i < n; i++) {
-                    int v = (int)o[i] + (int)mix[i];
-                    o[i] = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
-                }
-            }
+            tone_render(o, n, 16000, tone_active(), true);
+            s_pip_samples += n;
         }
 #endif
         return len;
@@ -437,17 +533,30 @@ static uint32_t hf_send_fill(uint8_t *buf, uint32_t len)
         return len;
     }
     size_t got = xStreamBufferReceive(s_dl, src, need, 0);
+    s_fill_calls++;
+    if (got < need) {
+        s_fill_partial++;
+    }
+    if (got == 0) {
+        s_fill_empty++;
+    }
+    {
+        size_t q = xStreamBufferBytesAvailable(s_dl);
+        if (q > s_dl_hwm) {
+            s_dl_hwm = (uint32_t)q;
+        }
+    }
     size_t in_n = got / 2;          /* samples available at 16 kHz */
     int16_t *out = (int16_t *)buf;
     const int16_t *in = (const int16_t *)src;
     uint32_t out_n = len / 2;
 #if VB_LOCAL_PIP_MIX
-    bool pip = tone_active();
+    bool pip = tone_active() || s_tone_env > 0.0;   /* keep filling while the tail fades */
 #else
     bool pip = tone_active() && in_n == 0;
 #endif
     if (pip && in_n == 0) {
-        tone_fill(out, out_n, 8000);   /* nothing from the cloud: play the bench tone */
+        tone_render(out, out_n, 8000, true, false);   /* nothing from the cloud: bench tone */
         s_pip_samples += out_n;
         return len;
     }
@@ -460,17 +569,16 @@ static uint32_t hf_send_fill(uint8_t *buf, uint32_t len)
             out[i] = 0;
         }
     }
+    /* The gate closed mid-pip (a downlink frame arrived): fade the tone out over the next
+     * few ms instead of cutting the sine where it stands, which is the click that made
+     * "some beeps sound wrong". Unconditional, exactly as the 16 kHz path now is. */
+    if (!pip) {
+        tone_tail_add(out, out_n, 8000);
+    }
 #if VB_LOCAL_PIP_MIX
     if (pip) {
-        int16_t mix[VB_FRAME_SAMPLES_8K];
-        if (out_n <= sizeof mix / sizeof mix[0]) {
-            tone_fill(mix, out_n, 8000);
-            s_pip_samples += out_n;
-            for (uint32_t i = 0; i < out_n; i++) {
-                int v = (int)out[i] + (int)mix[i];
-                out[i] = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
-            }
-        }
+        tone_render(out, out_n, 8000, tone_active(), true);
+        s_pip_samples += out_n;
     }
 #endif
     return len;
@@ -618,6 +726,9 @@ static void hf_event_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t
             hf_set_volumes("SLC up");
             memcpy(s_peer_bda, p->conn_stat.remote_bda, sizeof(s_peer_bda));
             s_have_peer = true;
+            s_link_up = true;
+            s_link_connecting = false;
+            s_link_retries = 0;
             vb_report_call_event("bt_slc", "{}");
         } else if (p->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
             s_n = 0;
@@ -627,9 +738,20 @@ static void hf_event_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t
             s_call = 0;
             s_call_setup = 0;
             s_audio_up = false;
-            s_have_peer = false;
+            s_link_up = false;
+            s_link_connecting = false;
+            /* The envelope belongs to the audio leg that just went away: leaving it part-way
+             * up makes the next pip start loud. */
+            s_tone_env = 0.0f;
+            /* s_peer_bda/s_have_peer are deliberately KEPT: they are the address we page to
+             * get the link back. Clearing them here is what made a reconnect impossible -
+             * hf_maint_task reconnects with them, and vb_bt_load_bonded_peer() restores them
+             * after a reboot, where RAM does not survive either. */
+            s_link_retry_tick = xTaskGetTickCount() + pdMS_TO_TICKS(VB_HF_RECONNECT_MS);
             set_call_radio_bias(false);
             vb_report_call_event("bt_disconnected", "{}");
+        } else if (p->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_CONNECTING) {
+            s_link_connecting = true;   /* hold off hf_maint_task's next page */
         }
         break;
     case ESP_HF_CLIENT_AUDIO_STATE_EVT:
@@ -657,6 +779,12 @@ static void hf_event_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t
         }
         if (s_audio_up) {
             s_audio_up_ms = (int64_t)xTaskGetTickCount() * portTICK_PERIOD_MS;
+            /* Per-call diagnostics: cumulative counters make the 5 s line unreadable after a
+             * long session when the question is always "what did THIS call do". */
+            s_fill_calls = s_fill_partial = s_fill_empty = 0;
+            s_dl_hwm = 0;
+            s_dl_gap_max_ms = 0;
+            s_dl_last_us = 0;
             s_first_in_logged = false;
 #if VB_LOCAL_PIP
             s_pip_start_ms = s_audio_up_ms;
@@ -784,6 +912,32 @@ static bool s_listen_only_logged = false;
  * completion carries 0x0A, which is why it read like a peer refusal. The working firmware
  * sets it to 1. Both handsets failed identically because the limit was zero for every peer.
  * 0.6.12 sets 1; the retry / listen-only levers below are diagnostics only. */
+/* Recover the phone we were last talking to after a reboot. s_peer_bda lives in RAM, so
+ * without this a board that has just rebooted has nobody to re-page and the reconnect above
+ * can never fire - which is precisely the case it exists for (the board rebooting under a
+ * call, or being moved and power-cycled). The bond is the durable record; this board pairs
+ * with one phone in practice, so the most recently bonded device is the peer. */
+static void vb_bt_load_bonded_peer(void)
+{
+    int num = esp_bt_gap_get_bond_device_num();
+    if (num <= 0) {
+        ESP_LOGW(TAG, "no bonded device yet - pair a phone and the link will reconnect itself");
+        return;
+    }
+    esp_bd_addr_t list[VB_MAX_BONDED];
+    int n = (num < (int)(sizeof list / sizeof list[0])) ? num : (int)(sizeof list / sizeof list[0]);
+    if (esp_bt_gap_get_bond_device_list(&n, list) != ESP_OK || n <= 0) {
+        ESP_LOGW(TAG, "bonded device list unreadable (%d entries)", num);
+        return;
+    }
+    memcpy(s_peer_bda, list[n - 1], sizeof(s_peer_bda));
+    s_have_peer = true;
+    ESP_LOGW(TAG, "bonded peer recovered: %02x:%02x:%02x:%02x:%02x:%02x "
+                  "(%d bonded) - re-paged if the link is down",
+             s_peer_bda[0], s_peer_bda[1], s_peer_bda[2],
+             s_peer_bda[3], s_peer_bda[4], s_peer_bda[5], num);
+}
+
 static void hf_maint_task(void *arg)
 {
     int call_up_ms = 0;
@@ -791,12 +945,51 @@ static void hf_maint_task(void *arg)
     for (;;) {
         vTaskDelay(pdMS_TO_TICKS(500));
         set_call_radio_bias(call_is_active());
+        /* Page the phone again when the HF link is gone. Nothing here ever asked it back -
+         * the disconnect handler even cleared the peer BDA - so once the handset dropped the
+         * SLC (walked away, slept, or the board rebooted under it) the board sat silent until
+         * someone re-paired it by hand.
+         *
+         * One attempt at a time, with a backoff: a page takes up to ~5 s to time out, so a
+         * fixed cadence starts the next on top of the last, and a phone that is simply out of
+         * range should not be paged forever at full rate. */
+        {
+            TickType_t now_t = xTaskGetTickCount();
+            if (!s_link_up && !s_link_connecting && s_have_peer &&
+                    (int32_t)(now_t - s_link_retry_tick) >= 0) {
+                esp_err_t rc = esp_hf_client_connect(s_peer_bda);
+                s_link_retries++;
+                uint32_t wait_ms = VB_HF_RECONNECT_MS;
+                if (s_link_retries >= 3) {
+                    wait_ms = VB_HF_RECONNECT_MAX_MS;
+                } else if (s_link_retries == 2) {
+                    wait_ms = VB_HF_RECONNECT_MS * 2;
+                }
+                ESP_LOGW(TAG, "HF link down - reconnect attempt %u"
+                              " (%02x:%02x:%02x:%02x:%02x:%02x) -> %s, next in %u ms",
+                         (unsigned)s_link_retries, s_peer_bda[0], s_peer_bda[1], s_peer_bda[2],
+                         s_peer_bda[3], s_peer_bda[4], s_peer_bda[5], esp_err_to_name(rc),
+                         (unsigned)wait_ms);
+                s_link_retry_tick = now_t + pdMS_TO_TICKS(wait_ms);
+            }
+        }
         /* Per-second in-call probe, so a stalled receive path or a sliding heap is
          * visible instead of inferred. `sco_in` must CLIMB while the AG is talking
          * (~133/s for CVSD 60-byte packets): flat means the stack is not delivering
          * inbound SCO data to the app at all, and a declining heap means a leak. */
         static int64_t next_probe_us;
         int64_t now_us = esp_timer_get_time();
+        /* Playback-path report, every 5 s: `empty` climbing at ~133 fills/s means the audio
+         * is starving, `partial` that the queue is running at the edge, `hwm` how deep it
+         * ever got against DL_BYTES, and gap_max_ms whether the data was late rather than
+         * lost. One line per 5 s, never per frame. */
+        static int64_t next_dl_probe_us;
+        if (call_is_active() && now_us >= next_dl_probe_us) {
+            next_dl_probe_us = now_us + 5000000;
+            ESP_LOGW(TAG, "dl: fills=%u partial=%u empty=%u hwm=%u/%u gap_max_ms=%u",
+                     (unsigned)s_fill_calls, (unsigned)s_fill_partial, (unsigned)s_fill_empty,
+                     (unsigned)s_dl_hwm, (unsigned)DL_BYTES, (unsigned)s_dl_gap_max_ms);
+        }
         if (call_is_active() && now_us >= next_probe_us) {
             next_probe_us = now_us + 1000000;
             static uint32_t pulls_prev;
@@ -837,7 +1030,10 @@ static void hf_maint_task(void *arg)
              * sco_create(TRUE)) instead of asking the AG with AT+BCC, so this line is what
              * sends HCI Setup Synchronous Connection. The stack logs the command and its
              * result (VB_HF: lines, including the command-status discriminator). */
-            if (s_have_peer) {
+            /* s_have_peer now means "we know a peer address", which is true even while the
+             * link is down, so the audio leg must also require the link to be up - otherwise
+             * this retries connect_audio() against a dead HF link. */
+            if (s_have_peer && s_link_up) {
                 esp_err_t ca = esp_hf_client_connect_audio(s_peer_bda);
                 ESP_LOGW(TAG, "call up %d ms, no SCO -> connect_audio attempt %d/%d"
                               "(%02x:%02x:%02x:%02x:%02x:%02x) -> %s",
@@ -1032,6 +1228,7 @@ esp_err_t vb_bt_init(const vb_bt_cfg_t *cfg, QueueHandle_t audio_q)
                         "hf data cb failed");
 
     s_ready = true;
+    vb_bt_load_bonded_peer();
     xTaskCreate(hf_maint_task, "hf_maint", 2560, NULL, 3, NULL);
 
     /* The feed pump blocks on this tick, so create it before the task or the task
