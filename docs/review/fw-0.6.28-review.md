# Firmware 0.6.28 — review copy

Two changes, both from hardware feedback. The diff is at the bottom of this file; the files
themselves are `firmware/main/vb_bt.c` (both changes) and `firmware/main/main.c` (version only).

## 1. Bluetooth does not reconnect after the phone drops the link

**Symptom:** "bluetooth disconnected from phone, modify to connect when connection loss".

**Cause:** nothing ever asked it back. `hf_event_cb()`'s `..._DISCONNECTED` branch cleared
`s_have_peer` and the peer BDA, so the address needed to page the phone again was thrown away —
a reconnect was impossible by construction. (The one existing retry, `VB_HF_SCO_RETRY`, only
re-opens the SCO **audio** leg during a call; it cannot restore a lost HF link.)

**Change:**
- `s_have_peer` / `s_peer_bda` are now **kept** across a disconnect; a new `s_link_up` tracks the
  SLC (HF link), which is distinct from `s_audio_up` (the SCO audio leg).
- `hf_maint_task` (already looping every 500 ms) re-pages the peer with
  `esp_hf_client_connect(s_peer_bda)` when `!s_link_up && s_have_peer`, at most once per
  `VB_HF_RECONNECT_MS` (5000). Every attempt is logged with the BDA and the return code.
- `s_link_retries` resets on a successful SLC connect.

**Review questions:** is 5 s the right cadence (a phone that is simply out of range gets paged
every 5 s indefinitely — acceptable, or should it back off)? Does re-paging need to be
suppressed while a call is up (a lost SLC ends the call anyway, so reconnecting immediately is
what we want — does anything below assume otherwise)?

## 2. "Some beeps get wrong sound" — the pip is cut mid-cycle

**Symptom:** Nuwan, listening during bring-up.

**Cause:** `tone_fill()` writes a 1 kHz sine into the caller's buffer, and the pip gate in
`hf_send_fill()` closes the instant a downlink frame arrives (`pip = tone_active() && in_n == 0`).
So a pip normally **ends between two samples** — the sine just stops at a non-zero value, which
is a step discontinuity: an audible click. Measured rates are correct (16000 on the 16 kHz
buffer, 8000 after the CVSD decimation), so this is not a pitch error; it is an edge artefact.
Note this is a bring-up diagnostic tone (`VB_LOCAL_PIP`), not product audio.

**Change:**
- `tone_fill(..., bool sounding)` now applies a per-sample envelope: ramps 0→1 while sounding and
  1→0 when not, over `TONE_RAMP_SAMPLES` (24 ≈ 3 ms at 8 kHz). No step at either edge.
- New `tone_tail_add()` emits the fading tail when the gate has just cut a pip short. It is called
  *after* the downlink is written into the buffer, because the caller overwrites those samples.
- In the `VB_LOCAL_PIP_MIX` build the pip flag became `tone_active() || s_tone_env > 0.0` so fills
  continue while the tail fades; `tone_active()` is passed as `sounding` so it fades out.

**Review questions:** is 24 samples (~3 ms) the right ramp (too short = still a click, too long =
the pip sounds soft)? Integer/fixed-point conversion for a target without `sin()`/floating point
in the SCO callback path — worth it? Is the tail correct when the downlink buffer is *partially*
filled (the `got < len` case in the 16 kHz path)?

## Not changed, and why

- **Latency.** The board's downlink stream buffer is `DL_BYTES` 4096 = 128 ms at 16 kHz PCM16, and
  the pip only plays when that buffer is empty. 128 ms is a deliberate jitter allowance, not a
  bug — but if the perceived delay is ours rather than the phone's/network's, this is the number
  to argue about. Not touched without a measurement.
- **`cb_slow`** counts SCO callbacks over 2 ms of *wall* time, so it includes the BT task being
  preempted by Wi-Fi — it measures CPU pressure, not audio-path health.
- **`reboot debt`** (0.6.27) and the console-side items are separate.

## Diff

```diff
diff --git a/firmware/main/main.c b/firmware/main/main.c
index 1668f0f..929cd69 100644
--- a/firmware/main/main.c
+++ b/firmware/main/main.c
@@ -41,7 +41,7 @@
 #include "voice_protocol.h"
 
 #ifndef FW_VERSION
-#define FW_VERSION "0.6.27"
+#define FW_VERSION "0.6.28"
 #endif
 
 static const char *TAG = "vb";
diff --git a/firmware/main/vb_bt.c b/firmware/main/vb_bt.c
index 75b2c74..a51d8b7 100644
--- a/firmware/main/vb_bt.c
+++ b/firmware/main/vb_bt.c
@@ -94,6 +94,10 @@ static volatile uint32_t s_sco_in_cbs;   /* app-level inbound SCO deliveries */
 #define VB_PIP_PERIOD_MS 1000
 static int64_t s_tone_until_ms;
 static double s_tone_phase;
+/* Envelope, 0..1. A pip cut mid-cycle is a click, and the gate closes the instant a
+ * downlink frame arrives, so that is the common case rather than the rare one. */
+static double s_tone_env;
+#define TONE_RAMP_SAMPLES 24        /* ~3 ms at 8 kHz: short enough to stay crisp */
 static int64_t s_pip_start_ms;      /* set when the SCO audio link comes up */
 static uint32_t s_pip_logged = 0xffffffffu;
 static int64_t s_audio_up_ms;
@@ -185,13 +189,30 @@ static void pip_tick(void)
  * harmonics make a rate error (samples handed over at the wrong cadence) sound like a
  * buzzier tone rather than a clean shift, which is exactly the ambiguity Hermes asked us to
  * remove. The phase carries across calls, so a gap in the fills is audible as a gap. */
-static void tone_fill(int16_t *out, uint32_t n, uint32_t rate)
+static void tone_fill(int16_t *out, uint32_t n, uint32_t rate, bool sounding)
 {
     const double two_pi = 6.283185307179586;
     const double step = two_pi * 1000.0 / (double)rate;
     const double amp = (double)VB_TONE_AMPLITUDE;   /* level standard, see voice_protocol.h */
+    const double d_env = 1.0 / (double)TONE_RAMP_SAMPLES;
     for (uint32_t i = 0; i < n; i++) {
-        out[i] = (int16_t)(amp * sin(s_tone_phase));
+        /* Ramp rather than switch: the pip gate closes on the first downlink frame, so a
+         * pip normally ENDS mid-cycle and the old code simply stopped the sine between two
+         * samples - an audible click, and the "some beeps sound wrong" report. */
+        if (sounding) {
+            if (s_tone_env < 1.0) {
+                s_tone_env += d_env;
+                if (s_tone_env > 1.0) {
+                    s_tone_env = 1.0;
+                }
+            }
+        } else if (s_tone_env > 0.0) {
+            s_tone_env -= d_env;
+            if (s_tone_env < 0.0) {
+                s_tone_env = 0.0;
+            }
+        }
+        out[i] = (int16_t)(amp * s_tone_env * sin(s_tone_phase));
         s_tone_phase += step;
         if (s_tone_phase > two_pi) {
             s_tone_phase -= two_pi;
@@ -199,6 +220,21 @@ static void tone_fill(int16_t *out, uint32_t n, uint32_t rate)
     }
 }
 
+/* Add the fading tail of a pip the gate has just cut short. Called after the downlink has
+ * been written into the buffer, because the caller overwrites those samples. */
+static void tone_tail_add(int16_t *out, uint32_t out_n, uint32_t rate)
+{
+    int16_t tail[VB_FRAME_SAMPLES];
+    if (s_tone_env <= 0.0 || out_n > sizeof tail / sizeof tail[0]) {
+        return;
+    }
+    tone_fill(tail, out_n, rate, false);
+    for (uint32_t i = 0; i < out_n; i++) {
+        int v = (int)out[i] + (int)tail[i];
+        out[i] = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
+    }
+}
+
 /* Demand-driven SCO feed. esp_hf_client_outgoing_data_ready() is the only thing
  * that makes Bluedroid pull PCM from hf_send_cb(). Calling it once per downlink
  * arrival slaved the feed to the network's cadence (choppy audio); calling it at
@@ -225,6 +261,12 @@ static void tone_fill(int16_t *out, uint32_t n, uint32_t rate)
  * BDA, and the SLC event is the last place it is handed to us, so remember it. */
 static esp_bd_addr_t s_peer_bda;
 static volatile bool s_have_peer;
+/* SLC (the HF link to the phone) - distinct from s_audio_up, which is the SCO audio leg.
+ * The link can be down while the audio leg still looks up, and reconnecting needs the link. */
+static volatile bool s_link_up;
+static uint32_t s_link_retries;
+static int64_t s_link_retry_us;
+#define VB_HF_RECONNECT_MS 5000     /* re-page the phone this often while it is gone */
 static volatile bool s_sco_pull_pending;
 static volatile uint32_t s_sco_pull_stuck;
 static SemaphoreHandle_t s_sco_tick;
@@ -397,7 +439,7 @@ static uint32_t hf_send_fill(uint8_t *buf, uint32_t len)
     if (s_rate >= 16000) {
         size_t got = xStreamBufferReceive(s_dl, buf, len, 0);
 #if VB_LOCAL_PIP_MIX
-        bool pip = tone_active();
+        bool pip = tone_active() || s_tone_env > 0.0;   /* keep filling while the tail fades */
 #else
         bool pip = tone_active() && got == 0;
 #endif
@@ -406,11 +448,14 @@ static uint32_t hf_send_fill(uint8_t *buf, uint32_t len)
             memset(buf + got, 0, len - got);   /* the mix below adds the pip */
 #else
             if (pip && got == 0) {
-                tone_fill((int16_t *)buf, len / 2, 16000);
+                tone_fill((int16_t *)buf, len / 2, 16000, true);
                 s_pip_samples += len / 2;
             } else {
                 memset(buf + got, 0, len - got);
             }
+            if (!pip) {
+                tone_tail_add((int16_t *)buf, len / 2, 16000);
+            }
 #endif
         }
 #if VB_LOCAL_PIP_MIX
@@ -419,7 +464,7 @@ static uint32_t hf_send_fill(uint8_t *buf, uint32_t len)
             uint32_t n = len / 2;
             if (n <= sizeof mix / sizeof mix[0]) {
                 int16_t *o = (int16_t *)buf;
-                tone_fill(mix, n, 16000);
+                tone_fill(mix, n, 16000, tone_active());
                 s_pip_samples += n;
                 for (uint32_t i = 0; i < n; i++) {
                     int v = (int)o[i] + (int)mix[i];
@@ -442,12 +487,12 @@ static uint32_t hf_send_fill(uint8_t *buf, uint32_t len)
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
+        tone_fill(out, out_n, 8000, true);   /* nothing from the cloud: play the bench tone */
         s_pip_samples += out_n;
         return len;
     }
@@ -460,11 +505,17 @@ static uint32_t hf_send_fill(uint8_t *buf, uint32_t len)
             out[i] = 0;
         }
     }
+    /* The gate closed mid-pip (a downlink frame arrived): fade the tone out over the next
+     * few ms instead of cutting the sine where it stands, which is the click that made
+     * "some beeps sound wrong". */
+    if (!pip) {
+        tone_tail_add(out, out_n, 8000);
+    }
 #if VB_LOCAL_PIP_MIX
     if (pip) {
         int16_t mix[VB_FRAME_SAMPLES_8K];
         if (out_n <= sizeof mix / sizeof mix[0]) {
-            tone_fill(mix, out_n, 8000);
+            tone_fill(mix, out_n, 8000, tone_active());
             s_pip_samples += out_n;
             for (uint32_t i = 0; i < out_n; i++) {
                 int v = (int)out[i] + (int)mix[i];
@@ -618,6 +669,8 @@ static void hf_event_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t
             hf_set_volumes("SLC up");
             memcpy(s_peer_bda, p->conn_stat.remote_bda, sizeof(s_peer_bda));
             s_have_peer = true;
+            s_link_up = true;
+            s_link_retries = 0;
             vb_report_call_event("bt_slc", "{}");
         } else if (p->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
             s_n = 0;
@@ -627,7 +680,11 @@ static void hf_event_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t
             s_call = 0;
             s_call_setup = 0;
             s_audio_up = false;
-            s_have_peer = false;
+            s_link_up = false;
+            /* s_peer_bda/s_have_peer are deliberately KEPT: they are the address we page to
+             * get the link back. Clearing them here is what made a reconnect impossible -
+             * hf_maint_task reconnects with them. */
+            s_link_retry_us = esp_timer_get_time() + (int64_t)VB_HF_RECONNECT_MS * 1000;
             set_call_radio_bias(false);
             vb_report_call_event("bt_disconnected", "{}");
         }
@@ -791,6 +848,24 @@ static void hf_maint_task(void *arg)
     for (;;) {
         vTaskDelay(pdMS_TO_TICKS(500));
         set_call_radio_bias(call_is_active());
+        /* Page the phone again when the HF link is gone. Nothing here ever asked it back -
+         * the disconnect handler even cleared the peer BDA - so once the handset dropped the
+         * SLC (walked away, slept, or the board rebooted under it) the board sat silent until
+         * someone re-paired it by hand. The cadence is slow enough not to hammer a phone that
+         * is out of range, and quick enough that walking back into range reconnects on its
+         * own. */
+        {
+            int64_t link_now = esp_timer_get_time();
+            if (!s_link_up && s_have_peer && link_now >= s_link_retry_us) {
+                esp_err_t rc = esp_hf_client_connect(s_peer_bda);
+                s_link_retries++;
+                ESP_LOGW(TAG, "HF link down - reconnect attempt %u"
+                              " (%02x:%02x:%02x:%02x:%02x:%02x) -> %s",
+                         (unsigned)s_link_retries, s_peer_bda[0], s_peer_bda[1], s_peer_bda[2],
+                         s_peer_bda[3], s_peer_bda[4], s_peer_bda[5], esp_err_to_name(rc));
+                s_link_retry_us = link_now + (int64_t)VB_HF_RECONNECT_MS * 1000;
+            }
+        }
         /* Per-second in-call probe, so a stalled receive path or a sliding heap is
          * visible instead of inferred. `sco_in` must CLIMB while the AG is talking
          * (~133/s for CVSD 60-byte packets): flat means the stack is not delivering
```
