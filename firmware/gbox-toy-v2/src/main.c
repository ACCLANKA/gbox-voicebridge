/* GBox I2S toy - ESP32-S3 + INMP441 + MAX98357A, Sinhala talking bot.
 *
 * Flow: I2S mic -> VAD -> POST the utterance to /v1/toy/talk -> play the answer.
 * While the reply is being fetched (12-30 s on CPU TTS) a recorded "one moment"
 * filler plays from flash, concurrently, so the wait is not silence.
 *
 *   mic  WS=4  BCLK=5  DIN=6     (I2S_NUM_0, 32-bit slots, L/R -> GND)
 *   spk  LRC=16 BCLK=15 DOUT=7   (I2S_NUM_1, 16-bit stereo, L=R)
 */
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"

#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "driver/i2s_std.h"

#if __has_include("secrets.h")
#include "secrets.h"         /* VB_TOY_KEY - generated locally, never committed */
#endif
#include "filler.h"          /* 2.45 s "one moment" clip, same voice as the bot */

static const char *TAG = "gbox-toy";

#define MIC_WS 4
#define MIC_BCLK 5
#define MIC_DIN 6
#define SPK_LRC 16
#define SPK_BCLK 15
#define SPK_DOUT 7

#define RATE 16000
#define FRAME 320                 /* 20 ms */
#define FRAME_BYTES 640
#define PREROLL_FRAMES 15         /* 300 ms kept before speech is detected */
#define MAX_UTTER_S 4
#define MAX_UTTER_FRAMES (MAX_UTTER_S * 50)
#define SILENCE_STOP_FRAMES 40    /* 800 ms of quiet ends the utterance */
#define VAD_RMS 0.030f            /* measured after MIC_GAIN */
#define MIN_SPEECH_FRAMES 15      /* 300 ms of speech before it counts */
#define MIC_GAIN 4.0f             /* the INMP441 reads ~20 dB quiet on this board */
#define PLAY_GAIN 2.0f            /* digital volume (~+6 dB); 3.0 was too loud */
#define HPF_A 0.96f               /* one-pole DC blocker, ~100 Hz at 16 kHz */

/* Wait for the whole reply: the TTS runs at roughly realtime, so a player that starts
 * early always catches up and runs dry (measured 20-25 starve events per reply). */
#define PREBUF_MS 60000

#ifndef VB_TOY_URL
#define VB_TOY_URL "https://vb.site.gigies.lk/v1/toy/talk"
#endif
#ifndef VB_TOY_NUDGE_URL
#define VB_TOY_NUDGE_URL "https://vb.site.gigies.lk/v1/toy/nudge"
#endif
#ifndef VB_TOY_KEY
#define VB_TOY_KEY ""
#endif

static i2s_chan_handle_t s_rx, s_tx;
static EventGroupHandle_t s_net;
#define GOT_IP BIT0

static char s_ssid[33], s_pass[65];
static char s_heard[256];
static SemaphoreHandle_t s_spk;    /* one owner of the speaker at a time */
static SemaphoreHandle_t s_turn;   /* signalled when a reply has finished playing */
static int16_t *s_buf;
static int16_t *s_preroll;
static int s_pre_idx, s_pre_count;

/* ---- config ------------------------------------------------------------- */
static void load_cfg(void)
{
    nvs_handle_t h;
    if (nvs_open("vb", NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    size_t n = sizeof s_ssid;
    if (nvs_get_str(h, "ssid", s_ssid, &n) != ESP_OK) s_ssid[0] = 0;
    n = sizeof s_pass;
    if (nvs_get_str(h, "pass", s_pass, &n) != ESP_OK) s_pass[0] = 0;
    nvs_close(h);
    ESP_LOGI(TAG, "ssid=%s toy=%s", s_ssid, VB_TOY_URL);
}

/* ---- wifi --------------------------------------------------------------- */
static void on_ev(void *a, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "wifi lost, retrying");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "ip " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_net, GOT_IP);
    }
}

static void wifi_start(void)
{
    s_net = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t c = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&c));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_ev, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_ev, NULL));

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, s_ssid, sizeof wc.sta.ssid);
    strlcpy((char *)wc.sta.password, s_pass, sizeof wc.sta.password);
    wc.sta.threshold.authmode = WIFI_AUTH_OPEN;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_start());
}

/* ---- i2s ---------------------------------------------------------------- */
static void i2s_start(void)
{
    i2s_chan_config_t rc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    rc.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&rc, NULL, &s_rx));
    i2s_std_config_t rs = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                        I2S_SLOT_MODE_MONO),
        .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = MIC_BCLK, .ws = MIC_WS,
                     .dout = I2S_GPIO_UNUSED, .din = MIC_DIN,
                     .invert_flags = {false, false, false}},
    };
    rs.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &rs));
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx));

    i2s_chan_config_t tc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    tc.auto_clear = true;
    /* 8 x 20 ms of DMA cushion: without it every socket read starved the amp. */
    tc.dma_desc_num = 8;
    tc.dma_frame_num = FRAME;
    ESP_ERROR_CHECK(i2s_new_channel(&tc, &s_tx, NULL));
    i2s_std_config_t ts = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                        I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = SPK_BCLK, .ws = SPK_LRC,
                     .dout = SPK_DOUT, .din = I2S_GPIO_UNUSED,
                     .invert_flags = {false, false, false}},
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &ts));
    /* left disabled: a running clock into the amp produced an audible tone at idle */
}

/* ---- audio helpers ------------------------------------------------------ */
static float hpf_x1, hpf_y1;

static void mic_read(int16_t *out, int n)
{
    static int32_t raw[FRAME];
    size_t got = 0;
    i2s_channel_read(s_rx, raw, (size_t)n * 4, &got, portMAX_DELAY);
    int m = got / 4;
    for (int i = 0; i < n; i++) {
        float x = (i < m) ? (float)(raw[i] >> 16) / 32768.0f : 0.0f;
        /* y = x - x1 + a*y1 : DC blocker. A naive y = a*(y1+x) has DC gain ~24 and
         * pinned the VAD at full scale on the INMP441 offset. */
        float y = x - hpf_x1 + HPF_A * hpf_y1;
        hpf_x1 = x;
        hpf_y1 = y;
        y *= MIC_GAIN;
        if (y > 1.0f) y = 1.0f;
        if (y < -1.0f) y = -1.0f;
        out[i] = (int16_t)(y * 32767.0f);
    }
}

static float rms_of(const int16_t *s, int n)
{
    float acc = 0;
    for (int i = 0; i < n; i++) {
        float v = (float)s[i] / 32768.0f;
        acc += v * v;
    }
    return sqrtf(acc / (float)n);
}

/* the amp may pick either channel, so every mono buffer is sent as L=R */
static esp_err_t spk_write(const int16_t *mono, int n, int *written)
{
    static int16_t st[2 * FRAME];
    size_t w = 0;
    for (int i = 0; i < n; i++) {
        st[2 * i] = mono[i];
        st[2 * i + 1] = mono[i];
    }
    esp_err_t e = i2s_channel_write(s_tx, st, (size_t)n * 4, &w, portMAX_DELAY);
    if (written) *written = (int)(w / 4);
    return e;
}

/* ---- HTTP --------------------------------------------------------------- */
static void log_hdr(esp_http_client_handle_t c, const char *name);

static int wav_hdr_len(const uint8_t *w, int n, int *sr, int *ch, int *bits)
{
    if (n < 12 || memcmp(w, "RIFF", 4) != 0) return -1;
    for (int i = 12; i + 8 <= n; ) {
        uint32_t sz = w[i + 4] | (w[i + 5] << 8) | (w[i + 6] << 16) | (w[i + 7] << 24);
        if (memcmp(w + i, "fmt ", 4) == 0 && i + 16 <= n) {
            *ch = w[i + 10] | (w[i + 11] << 8);
            *sr = w[i + 12] | (w[i + 13] << 8) | (w[i + 14] << 16) | (w[i + 15] << 24);
            *bits = w[i + 22] | (w[i + 23] << 8);
        } else if (memcmp(w + i, "data", 4) == 0) {
            return i + 8;
        }
        i += 8 + sz + (sz & 1);
    }
    return -1;
}

/* percent-decode so Sinhala is readable in the log */
static void log_hdr(esp_http_client_handle_t c, const char *name)
{
    char *val = NULL;
    if (esp_http_client_get_header(c, name, &val) != ESP_OK || !val || !val[0]) {
        return;
    }
    const char *raw = val;
    char out[512];
    int o = 0;
    for (int i = 0; raw[i] && o < (int)sizeof out - 1; i++) {
        if (raw[i] == '%' && raw[i + 1] && raw[i + 2]) {
            char hex[3] = {raw[i + 1], raw[i + 2], 0};
            out[o++] = (char)strtol(hex, NULL, 16);
            i += 2;
        } else if (raw[i] == '+') {
            out[o++] = ' ';
        } else {
            out[o++] = raw[i];
        }
    }
    out[o] = 0;
    if (strcmp(name, "X-Toy-Transcript") == 0) {
        strlcpy(s_heard, out, sizeof s_heard);
    }
    ESP_LOGI(TAG, "%s: %s", name, out);
}

/* ---- streaming buffer: reader task + player ----------------------------- */
typedef struct {
    uint8_t *buf;
    int cap;
    volatile int have;
    volatile bool done;
    volatile bool exited;
    SemaphoreHandle_t mu;
    SemaphoreHandle_t more;
    esp_http_client_handle_t c;
} stream_t;

static int stream_have(stream_t *s)
{
    xSemaphoreTake(s->mu, portMAX_DELAY);
    int h = s->have;
    xSemaphoreGive(s->mu);
    return h;
}

static bool stream_done(stream_t *s)
{
    xSemaphoreTake(s->mu, portMAX_DELAY);
    bool d = s->done;
    xSemaphoreGive(s->mu);
    return d;
}

static void reader_task(void *arg)
{
    stream_t *s = (stream_t *)arg;
    while (true) {
        if (stream_done(s)) break;
        if (s->have >= s->cap) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        int want = (s->cap - s->have) > 16384 ? 16384 : (s->cap - s->have);
        int r = esp_http_client_read(s->c, (char *)s->buf + s->have, want);
        if (r > 0) {
            xSemaphoreTake(s->mu, portMAX_DELAY);
            s->have += r;
            xSemaphoreGive(s->mu);
            xSemaphoreGive(s->more);
        } else if (r == 0) {
            if (esp_http_client_is_complete_data_received(s->c)) break;
            vTaskDelay(pdMS_TO_TICKS(10));
        } else {
            break;
        }
    }
    xSemaphoreTake(s->mu, portMAX_DELAY);
    s->done = true;
    xSemaphoreGive(s->mu);
    xSemaphoreGive(s->more);
    s->exited = true;
    vTaskDelete(NULL);
}

/* Whole reply first, then play: the network and the speaker never share a loop. */
static void play_reply(esp_http_client_handle_t c)
{
    const int cap = 1024 * 1024;
    stream_t st = {0};
    st.buf = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
    st.cap = cap;
    st.mu = xSemaphoreCreateMutex();
    st.more = xSemaphoreCreateBinary();
    st.c = c;
    if (!st.buf || !st.mu || !st.more) {
        ESP_LOGW(TAG, "no psram for reply");
        return;
    }

    int off = -1, sr = RATE, ch = 1, bits = 16;
    while (st.have < cap) {
        int r = esp_http_client_read(c, (char *)st.buf + st.have, 16384);
        if (r == 0) {
            if (esp_http_client_is_complete_data_received(c)) break;
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (r < 0) break;
        st.have += r;
        off = wav_hdr_len(st.buf, st.have, &sr, &ch, &bits);
        if (off > 0 && (st.have - off) >= (PREBUF_MS * sr * 2) / 1000) break;
        if (off > 0 && esp_http_client_is_complete_data_received(c)) break;
    }
    if (off <= 0 || bits != 16) {
        ESP_LOGW(TAG, "bad wav (off=%d sr=%d bits=%d)", off, sr, bits);
        free(st.buf);
        return;
    }
    ESP_LOGI(TAG, "wav %d Hz, fully buffered: %d bytes (%.1f s of audio)",
             sr, st.have - off, (float)(st.have - off) / 2.0f / (float)sr);

    xTaskCreatePinnedToCore(reader_task, "reader", 8192, &st, 4, NULL, 0);

    float step = (float)sr / (float)RATE;
    float pos = 0.0f;
    int16_t out[FRAME];
    int on = 0;
    uint32_t total = 0;
    int starves = 0;
    int16_t *pcm = (int16_t *)st.buf + (off / 2);

    if (s_spk) xSemaphoreTake(s_spk, portMAX_DELAY);
    i2s_channel_enable(s_tx);
    {
        /* prime the amp: writing audio into a cold DMA queue clipped the first syllable */
        int16_t prime[FRAME] = {0};
        for (int k = 0; k < 4; k++) {
            spk_write(prime, FRAME, NULL);
        }
        vTaskDelay(pdMS_TO_TICKS(25));
    }

    while (true) {
        int avail = (stream_have(&st) - off) / 2;
        int j = (int)pos;
        if (j + 1 >= avail) {
            if (st.have >= cap) break;
            if (stream_done(&st) && j + 1 >= avail) break;
            starves++;
            xSemaphoreTake(st.more, pdMS_TO_TICKS(2000));
            continue;
        }
        float f = pos - (float)j;
        float a = (float)pcm[j], b = (float)pcm[j + 1];
        float v = (a + (b - a) * f) * PLAY_GAIN;
        if (v > 32767.0f) v = 32767.0f;
        if (v < -32767.0f) v = -32767.0f;
        out[on++] = (int16_t)v;
        pos += step;
        if (on == FRAME) {
            spk_write(out, on, NULL);
            total += on;
            on = 0;
        }
    }
    if (on > 0) {
        spk_write(out, on, NULL);
        total += on;
    }
    /* 200 ms of silence before shutting the amp: disabling with audio still queued
     * swallowed the last word of every sentence */
    int16_t sil[FRAME] = {0};
    for (int k = 0; k < 10; k++) {
        spk_write(sil, FRAME, NULL);
    }
    vTaskDelay(pdMS_TO_TICKS(60));
    i2s_channel_disable(s_tx);
    if (s_spk) xSemaphoreGive(s_spk);

    st.done = true;
    xSemaphoreGive(st.more);
    for (int k = 0; k < 200 && !st.exited; k++) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (st.exited) {
        free(st.buf);
    } else {
        ESP_LOGW(TAG, "reader still running - leaking %d KB", cap / 1024);
    }
    ESP_LOGI(TAG, "played %.1f s in %d starve waits", (float)total / (float)RATE, starves);
}

/* ---- filler ------------------------------------------------------------- */
static void play_filler(void)
{
    if (!s_spk || !xSemaphoreTake(s_spk, pdMS_TO_TICKS(8000))) {
        return;
    }
    i2s_channel_enable(s_tx);
    int i = 0;
    while (i + FRAME <= FILLER_SAMPLES) {
        spk_write(filler_pcm + i, FRAME, NULL);
        i += FRAME;
    }
    if (i < FILLER_SAMPLES) {
        spk_write(filler_pcm + i, FILLER_SAMPLES - i, NULL);
    }
    int16_t sil[FRAME] = {0};
    spk_write(sil, FRAME, NULL);
    i2s_channel_disable(s_tx);
    xSemaphoreGive(s_spk);
}

/* ---- request ------------------------------------------------------------ */
static void request(const char *url, const int16_t *pcm, int samples)
{
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 120000,
        .buffer_size = 4096,
        .buffer_size_tx = 2048,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    esp_http_client_set_header(c, "Content-Type", "application/octet-stream");
    if (VB_TOY_KEY[0]) {
        esp_http_client_set_header(c, "X-Toy-Key", VB_TOY_KEY);
    }

    if (samples > 0) {
        ESP_LOGI(TAG, "asking the toy (%.1f s)", samples / 16000.0);
    } else {
        ESP_LOGI(TAG, "asking the toy to continue");
    }
    if (esp_http_client_open(c, samples * 2) != ESP_OK) {
        ESP_LOGW(TAG, "open failed");
        esp_http_client_cleanup(c);
        return;
    }
    if (samples > 0) {
        esp_http_client_write(c, (const char *)pcm, samples * 2);
    }
    int clen = esp_http_client_fetch_headers(c);
    int status = esp_http_client_get_status_code(c);
    if (status != 200) {
        ESP_LOGW(TAG, "toy said %d", status);
        esp_http_client_cleanup(c);
        return;
    }
    log_hdr(c, "X-Toy-Transcript");
    log_hdr(c, "X-Toy-Reply");
    ESP_LOGI(TAG, "reply starting (length %d)", clen);

    play_reply(c);

    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    hpf_x1 = hpf_y1 = 0;
}

static void talk(const int16_t *pcm, int samples)
{
    request(VB_TOY_URL, pcm, samples);
}

static void nudge(void)
{
    request(VB_TOY_NUDGE_URL, NULL, 0);
}

static void turn_task(void *arg)
{
    talk(s_buf, (int)(intptr_t)arg);
    if (s_turn) xSemaphoreGive(s_turn);
    vTaskDelete(NULL);
}

/* ---- main loop ---------------------------------------------------------- */
static bool stop_requested(void)
{
    static const char *words[] = {"bye", "goodbye", "stop", "නවත්ත", "එපා", "බායි", "නවතින්න"};
    char low[256];
    int i = 0;
    for (; s_heard[i] && i < (int)sizeof low - 1; i++) {
        char c = s_heard[i];
        low[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
    }
    low[i] = 0;
    for (int k = 0; k < (int)(sizeof words / sizeof words[0]); k++) {
        if (strstr(low, words[k])) {
            ESP_LOGW(TAG, "stop requested (%s)", words[k]);
            return true;
        }
    }
    return false;
}

static void toy_task(void *arg)
{
    int16_t frame[FRAME];
    int utter = 0;
    int quiet = 0;
    int voiced_run = 0;
    int idle_frames = 0;
    int nudges = 0;
    bool speaking = false;

    ESP_LOGI(TAG, "listening - speak Sinhala");
    while (true) {
        mic_read(frame, FRAME);
        float level = rms_of(frame, FRAME);

        static uint32_t ticks, last_log;
        static float peak;
        if (level > peak) peak = level;
        ticks++;
        if (!speaking && ticks - last_log > 100) {   /* every 2 s while idle */
            last_log = ticks;
            ESP_LOGI(TAG, "mic %.0f dB  peak %.0f dB%s", 20 * log10f(level + 1e-9f),
                     20 * log10f(peak + 1e-9f),
                     peak > VAD_RMS ? "  <- speech seen" : "");
            peak = 0;
        }

        if (!speaking) {
            idle_frames++;
            if (idle_frames > 400 && nudges < 3 && s_heard[0]) {   /* 8 s */
                idle_frames = 0;
                nudges++;
                ESP_LOGI(TAG, "silence - nudging (%d/3)", nudges);
                nudge();
                vTaskDelay(pdMS_TO_TICKS(200));
                continue;
            }

            memcpy(s_preroll + (s_pre_idx % PREROLL_FRAMES) * FRAME, frame, FRAME_BYTES);
            s_pre_idx++;
            if (s_pre_count < PREROLL_FRAMES) s_pre_count++;
            if (level <= VAD_RMS) voiced_run = 0;

            if (level > VAD_RMS) {
                if (++voiced_run < MIN_SPEECH_FRAMES) {
                    continue;
                }
                speaking = true;
                quiet = 0;
                utter = 0;
                idle_frames = 0;
                nudges = 0;
                int start = (s_pre_idx - s_pre_count + PREROLL_FRAMES * 100) % PREROLL_FRAMES;
                for (int k = 0; k < s_pre_count && utter < MAX_UTTER_FRAMES * FRAME; k++) {
                    int idx = (start + k) % PREROLL_FRAMES;
                    memcpy(s_buf + utter, s_preroll + idx * FRAME, FRAME_BYTES);
                    utter += FRAME;
                }
                ESP_LOGI(TAG, "speech detected (%.0f dB)", 20 * log10f(level + 1e-9f));
            }
            continue;
        }

        if (utter + FRAME <= MAX_UTTER_FRAMES * FRAME) {
            memcpy(s_buf + utter, frame, FRAME_BYTES);
            utter += FRAME;
        }
        quiet = (level < VAD_RMS * 0.6f) ? quiet + 1 : 0;

        if (quiet >= SILENCE_STOP_FRAMES || utter >= MAX_UTTER_FRAMES * FRAME) {
            speaking = false;
            voiced_run = 0;
            s_pre_idx = 0;
            s_pre_count = 0;
            ESP_LOGI(TAG, "utterance %.1f s -> asking", utter / 16000.0);

            if (s_turn) {
                xTaskCreatePinnedToCore(turn_task, "turn", 8192, (void *)(intptr_t)utter,
                                        5, NULL, 0);
                play_filler();            /* speaks while the reply is fetched */
                xSemaphoreTake(s_turn, portMAX_DELAY);
            } else {
                talk(s_buf, utter);
            }
            utter = 0;
            vTaskDelay(pdMS_TO_TICKS(300));
            if (stop_requested()) {
                ESP_LOGW(TAG, "conversation ended by user - idle for 60 s");
                for (int k = 0; k < 60; k++) {
                    vTaskDelay(pdMS_TO_TICKS(1000));
                }
                s_heard[0] = 0;
            }
            ESP_LOGI(TAG, "listening");
        }
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
    load_cfg();

    s_buf = heap_caps_malloc(MAX_UTTER_FRAMES * FRAME_BYTES, MALLOC_CAP_SPIRAM);
    s_preroll = heap_caps_malloc(PREROLL_FRAMES * FRAME_BYTES, MALLOC_CAP_SPIRAM);
    if (!s_buf || !s_preroll) {
        ESP_LOGE(TAG, "psram alloc failed");
        return;
    }

    s_spk = xSemaphoreCreateMutex();
    s_turn = xSemaphoreCreateBinary();

    wifi_start();
    xEventGroupWaitBits(s_net, GOT_IP, pdFALSE, pdTRUE, pdMS_TO_TICKS(30000));
    i2s_start();

    ESP_LOGI(TAG, "GBox I2S toy ready (prebuffer %d ms, play gain %.1f, toy key %d chars)",
             PREBUF_MS, PLAY_GAIN, (int)strlen(VB_TOY_KEY));

    /* three beeps: proves amp + speaker without involving the network */
    vTaskDelay(pdMS_TO_TICKS(50));
    esp_err_t en = i2s_channel_enable(s_tx);
    ESP_LOGI(TAG, "tx enable: %s", esp_err_to_name(en));

    int16_t tone[FRAME];
    for (int beep = 0; beep < 3; beep++) {
        for (int b = 0; b < 25; b++) {        /* 500 ms */
            for (int i = 0; i < FRAME; i++) {
                int nn = b * FRAME + i;
                tone[i] = (int16_t)(25000.0f * sinf(2.0f * (float)M_PI * 1000.0f * nn / RATE));
            }
            spk_write(tone, FRAME, NULL);
        }
        memset(tone, 0, sizeof tone);
        for (int b = 0; b < 15; b++) {        /* 300 ms gap */
            spk_write(tone, FRAME, NULL);
        }
    }
    i2s_channel_disable(s_tx);
    ESP_LOGI(TAG, "tone test done");

    xTaskCreatePinnedToCore(toy_task, "toy", 8192, NULL, 5, NULL, 1);
}
