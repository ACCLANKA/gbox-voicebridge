# Handoff brief — Voice Bridge ESP32-WROOM bring-up (agent → agent)

> **Audience:** the desktop agent ("Hermes") that has USB/COM access to the
> ESP32-WROOM-32 (DevKit v1) on a HAT.
> **Author:** the cloud-side agent (VPS). Repo: gbox / Voice Bridge.
> **Goal of this pass:** flash a real board so it **joins Wi-Fi and opens the
> device WebSocket to the cloud API**, and confirm the handshake end-to-end.
> Voice audio is **not** in scope yet (the audio path is still a stub — see §8).

---

## 1. The split of responsibilities

| Owner | Machine | Job |
| --- | --- | --- |
| Cloud agent (VPS) | sandbox | owns the API contract, builds the firmware image, defines the config interface |
| **Hermes** | your desktop | **runs ESP-IDF + esptool**, builds/flashes over USB, watches the serial monitor, reports logs |

The ESP32 is on your LAN. The cloud API must therefore run on a host **your
board can reach**. For this bring-up the API runs on the **desktop LAN**
(`http://<lan-ip>:8090`; 8080 is taken by the call-flow dashboard) and the board
uses **plain `ws://`**.

> **SNTP is implemented now.** The firmware syncs the clock over SNTP
> (`pool.ntp.org` by default) and attaches the mbedTLS **certificate bundle**,
> so `wss://` works and validates certificate dates. For the LAN bring-up keep
> plain `ws://` anyway — no cert to manage on a `192.168.x.x` host.

### Topology decision (this pass) — option (c)

**Chosen: (c) — API on Nuwan's desktop LAN at `:8090`; the board targets
`ws://<lan-ip>:8090`.** Rationale: the device already sits on that LAN, so
there is no cert, no SNTP, no public exposure, and no dependency on the sandbox
staying up. The VPS/sandbox stays build+contract only.

Moving the tree to the desktop (there is no remote today): a **git bundle** is
provided at `voicebridge.bundle` (all branches/commits); clone with
`git clone voicebridge.bundle voicebridge && cd voicebridge && git switch master`.
Then **re-provision on the desktop DB** — the device/key I have here live in the
sandbox DB and do not travel with the code (see §4).

Options (a)/(b) (device → public VPS) are now unblocked: **SNTP landed this pass**,
so `wss://` validates. Flip `tls=1` + point `host` at the public endpoint when you
want the device on the VPS instead of the LAN.

**Seeded DB included.** `voicebridge.db` (in the handoff bundle) already holds
`dev_caadc976fe` / `GBX-BENCH01` / `key_c12cb18559` under `ten_1`, so if you run
the desktop API from that file you can skip §4 re-provisioning. Otherwise
re-create device + key on a fresh DB with the §4 commands.

## 2. What was missing (now fixed in firmware/)

The scaffold compiled but could never reach the cloud. Fixed:

| Issue | Fix |
| --- | --- |
| No Wi-Fi code at all — `app_main` opened a socket with no netif | added Wi-Fi **STA** connect (`wifi_start`) |
| Hardcoded `wss://ingest.voicebridge.example`, `GBX-XXXXXX`, `vb_REPLACE_ME` | config layer `vb_config.c` + `Kconfig.projbuild` (`CONFIG_VB_*`), overridable from **NVS** |
| `wss://` only (TLS verify on) | scheme is now `ws` **or** `wss` per config (`VB_INGEST_TLS`) |
| `CONFIG_ESP32_SPIRAM_SUPPORT=y` breaks a WROOM without PSRAM | set to `n` in `sdkconfig.defaults` |
| `esp_websocket_client` not in the IDF tree | added `main/idf_component.yml` (fetched from the component registry during build) |
| `audio_in_task()` streamed **silence** (`TODO: real captured audio`) | Phase 1 input: I2S mic capture (`vb_audio.c`, `CONFIG_VB_I2S_*`) |

### 2b. Microphone (Phase 1 audio input)

`vb_audio.c` brings up **I2S0 in STD/Philips mode**, master, **16 kHz mono**,
**32-bit slots**, taking the **top 16 bits** to form PCM16 (`INMP441` is
left-justified). Defaults in `sdkconfig.defaults`:

| signal | GPIO | INMP441 pin |
| --- | --- | --- |
| BCLK | **26** | SCK |
| WS | **25** | WS |
| DIN | **22** | SD |
| — | GND | L/R (left slot) |
| — | 3V3 | VDD |

`CONFIG_VB_AUDIO_ENABLE=n` (or init failure) falls back to silence, so the image
still boots and streams without a mic. If your mic is on **different pins or is a
PDM part**, tell me and I'll rebuild — you can't menuconfig without the IDF.

**Bench check with no call:** every ~2 s the firmware logs
`vb: audio peak=<0..32767>`. Talk near the mic and watch it rise; silence stays
near 0. That proves capture on real hardware before any audio leg exists.

**Presence diagnostics (fw 0.3.2).** `i2s_channel_init` success proves nothing
about whether a mic is wired, so the firmware reports continuously:

```
vb: audio peak=1240 rms=186 distinct=312 lowbits=vary errs=0
```

| field | meaning |
| --- | --- |
| `peak` / `rms` | full-scale and RMS level of the window (PCM16) |
| `distinct` | max distinct **raw 32-bit** words in a 20 ms frame (capped at 65 = "many") |
| `lowbits` | whether the low 8 bits ever dither (`vary` = a live clock) |
| `errs` | I2S read failures / zero-length reads in the window |

Read it directly: **`distinct=1` (or `lowbits=static`) → nothing is driving
DIN**; `distinct` in the hundreds with low `rms` → a real mic in a quiet room;
high `rms` → the mic is live. A one-shot boot probe logs the same verdict, and a
1-distinct window is logged as a WARNING. `reset reason:` is logged at boot
(a `POWERON` from opening COM17 is not a crash).

**Runtime pin map.** Pins, slot and on/off come from the `vb` NVS namespace
(`i2s_bclk`, `i2s_ws`, `i2s_din`, `i2s_slot`, `audio_enable`; i32) falling back to
`CONFIG_VB_I2S_*`, so a wrong pinout is swept with
`scripts/make-device-config.py --i2s-bclk … --i2s-din …` plus an NVS reflash —
**no rebuild**. `i2s_slot` 0=left (INMP441 `L/R`→GND), 1=right.

## 3. Firmware config contract (the interface)

The firmware resolves config in this order: **NVS (namespace `vb`) → compile-time
`CONFIG_VB_*`**. Keys:

| NVS key | type | example | meaning |
| --- | --- | --- | --- |
| `ssid` | string | `MyNetwork` | Wi-Fi SSID — **case-sensitive**, 2.4 GHz only |
| `pass` | string | `s3cret` | Wi-Fi password |
| `host` | string | `192.168.1.50` | API host / LAN IP |
| `port` | i32 | `8080` | API port |
| `tls` | i32 | `0` | `0`→`ws://`, `1`→`wss://` |
| `serial` | string | `GBX-BENCH01` | **must match a device row** in the tenant |
| `key` | string | `vb_…` | **tenant API key** |
| `sntp` | string | `pool.ntp.org` | SNTP server (only used when `tls=1`) |
| `audio_enable` | i32 | `1` | mic capture on/off (default: firmware value) |
| `i2s_bclk` | i32 | `26` | I2S BCLK GPIO override |
| `i2s_ws` | i32 | `25` | I2S WS GPIO override |
| `i2s_din` | i32 | `22` | I2S DIN GPIO override |
| `i2s_slot` | i32 | `0` | `0`=left (L/R→GND), `1`=right (L/R→VDD) |

Two ways to set them (pick one):

- **menuconfig (quickest for a bench board):** build with `CONFIG_VB_*` set —
  secrets stay in the local, gitignored `sdkconfig`.
- **NVS image (field-clean):** run
  `python scripts/make-device-config.py --ssid … --host … --serial … --key … --out build/nvs-config.bin`
  then flash it at `0x9000`. Secrets never enter the published `.bin`.

## 4. Cloud-side prerequisites (against the LAN API)

The server rejects the HTTP upgrade with **403** unless `serial` belongs to the
tenant of the API key. Create both first. Easiest is the dashboard
(**Devices → + Add device** with serial `GBX-BENCH01`, then **API → API keys**);
or the admin API:

```bash
API=http://127.0.0.1:8080
J='Content-Type: application/json'

TOKEN=$(curl -s -X POST $API/v1/auth/login -H "$J" \
  -d '{"email":"owner@acme.example","password":"password123"}' | jq -r .access_token)

# mint the tenant API key (plaintext returned ONCE)
curl -s -X POST $API/v1/api-keys -H "Authorization: Bearer $TOKEN" -H "$J" \
  -d '{"name":"bench-board","scopes":["devices","calls"]}'
```

The device must be created under the **same tenant** (`ten_1` for the seeded
owner). Use the dashboard for that step so the session/tenant is unambiguous.

## 5. Build (on the desktop, ESP-IDF v5.3)

```bash
git pull                                   # get the firmware/config changes
. $IDF_PATH/export.sh                      # ESP-IDF v5.3
cd firmware
idf.py set-target esp32
# either: idf.py menuconfig  (Voice Bridge device configuration → set VB_*)
idf.py build
```

First build fetches `espressif/esp_websocket_client` — needs internet once.

**No IDF on the desktop?** Ask the VPS agent for
`voicebridge-esp32-merged.bin` (built there) and skip to §6.

## 6. Flash

With IDF:

```bash
idf.py -p COMx flash monitor     # handles offsets, then opens the monitor
```

Without IDF (esptool only, e.g. a merged image):

> **esptool v5:** there is no `esptool.py` shim and no `-z` flag any more. Use the
> module form and dashed command names (Hermes has esptool 5.4.0):

```bash
python -m esptool --chip esp32 --port COMx erase-flash
# merged image (bootloader + partition table + app):
python -m esptool --chip esp32 --port COMx write-flash 0x0 voicebridge-esp32-merged.bin
# OR app-only + separate NVS config:
python -m esptool --chip esp32 --port COMx write-flash 0x10000 build/voicebridge.bin 0x9000 build/nvs-config.bin
python -m serial.tools.miniterm COMx 115200
```

### Prebuilt artifact (from the VPS)

The VPS agent compiled this exact firmware against **ESP-IDF v5.3** and merged
the image, so no toolchain is needed on the desktop:

| | |
| --- | --- |
| file | `firmware/build/voicebridge-esp32-merged.bin` (download via the Files panel) |
| size | 1020112 bytes |
| sha256 | `e73113c3b8bb479c54988f78ba71102c2c92ddb66db27bce77596c9711df0416` |
| flash cmd | `python -m esptool --chip esp32 --port COMx write-flash 0x0 voicebridge-esp32-merged.bin` |

The image carries the **compile-time defaults only** (empty SSID/key) — so you
MUST also flash a config NVS image (§3) or rebuild with menuconfig; otherwise the
board boots, logs `missing Wi-Fi SSID or API key`, and sits in the reconnect loop.

**Boot strap:** if the HAT has no auto-reset (DTR/RTS) wiring, hold **BOOT**,
tap **EN/RST**, release BOOT to enter download mode before flashing.

## 7. Expected serial output (success)

```
I (xxx) vb_cfg: config: host=192.168.1.50:8080 tls=0 serial=GBX-BENCH01 ssid=MyNetwork key=set
I (xxx) vb: connecting to wifi ssid=MyNetwork
I (xxx) vb: wifi got ip 192.168.1.73
I (xxx) vb: websocket connected, hello sent
```

Cloud side: the device's `last_seen` updates and (after the first `telemetry`
frame, ~15 s) RSSI appears; the **Devices** tab shows it with a fresh heartbeat.

**Firmware version string:** the `hello` frame carries **`"fw":"0.3.2"`** (this
image adds I2S mic capture, the presence diagnostics and an NVS-overridable pin
map, on top of Wi-Fi STA, SNTP, cert bundle and telemetry).

### Pass gate (what counts as success)

| Check | Required? |
| --- | --- |
| boot → `vb_cfg: config:` → `wifi got ip` → `websocket connected, hello sent` | **required** |
| cloud receives `hello` and replies `{"type":"ack","action":"hello"}` (device `last_seen` updates) | **required** |
| first `telemetry` → `ack action=telemetry` within ~15 s (proves the periodic task + ack path) | **required** |
| device shows in the dashboard **Devices** tab | **required** |
| `ping`→`pong` | n/a — the firmware does not send `ping` |

PASS = all four required rows. `hello` alone proves connect+auth; the `telemetry`
ack is what proves the app tasks are running, so include it.

## 8. Known gaps (do NOT expect these to work yet)

- **Audio input is real now; the rest of the voice path is not.** I2S mic capture
  produces genuine 20 ms PCM16 frames (see §2b). Still stubbed: **HFP/SCO from a
  Bluetooth headset**, **AEC**, and **Opus/mSBC** encoding — output is raw PCM16
  only, and there is no playback path.
- **No BLE/SoftAP provisioning** — creds come from menuconfig/NVS for now.
- **Single board** — the S3 "stream board" from the two-board design is out of scope.
- **SNTP — implemented.** The clock is synced at boot (only awaited when
  `tls=1`), and `esp_crt_bundle_attach` is set for `wss://`, so certificate
  **date** validation now passes. `ws://` on the LAN remains the simplest path.
- **TLS** — `wss://` needs a valid cert; keep `CONFIG_ESP_TLS_INSECURE=n`.

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Handshake rejected with **HTTP 403** (no WS close frame) | serial/key mismatch. The server calls `ws.close()` *before* `accept()`, so uvicorn answers the HTTP upgrade with **403** — you will NOT see a WS 4401. esp_websocket_client logs a transport/handshake failure. | serial must exist for the key's tenant (§4) |
| `wifi disconnected, retrying` loop | 5 GHz SSID, wrong creds, or **wrong SSID casing** | ESP32 is 2.4 GHz only; SSIDs are case-sensitive (`oshadha` ≠ `Oshadha`) |
| Reboot loop / `SPIRAM` panic | PSRAM enabled on a non-PSRAM module | keep `CONFIG_ESP32_SPIRAM_SUPPORT=n` |
| `esp_websocket_client` not found | component not fetched | ensure internet on first build; check `main/idf_component.yml` |
| `vb_audio: DIN looks UNDRIVEN: distinct=1 …` | no mic, wrong pins, or no power — the SD line is floating | check 3V3+GND and the GPIO map (§2b); a live mic idles at noise, never a constant |
| `audio peak=32768` (full scale), `distinct=1`, or `lowbits=static` for many windows | DIN undriven, or the mic drives the other slot | hardware, not code: verify wiring; try `i2s_slot=1` or new pins via NVS (§2b), no rebuild |
| `POWERON_RESET` at the top of a fresh capture | opening COM17 asserts DTR/RTS | **not a crash** — the board resets when the port is opened |
| `Failed to connect ... No serial data` | not in download mode | hold BOOT + tap EN (§6) |
| Connects but no cloud activity | API bound to `127.0.0.1` | bind `0.0.0.0`, open the firewall, use the LAN IP |

## 10. Report back (what the VPS agent needs)

1. Full `esptool`/`idf.py flash` output (offsets + write verify).
2. Serial log from boot through `websocket connected, hello sent`, **including**:
   - the boot banner + reset reason (e.g. `rst:0x1 (POWERON_RESET)`);
   - the `vb_cfg: config:` line (redact the key);
   - `wifi got ip …`;
   - **`sntp time set` / `sntp epoch=…`** — proves the clock was valid *before* the
     TLS handshake (the whole point of the wss path);
   - any `esp_websocket_client` reconnect/TLS error lines.
3. Whether **`hello`→ack** AND the **first `telemetry`→ack** both arrived, and the
   RSSI/heap values in that telemetry frame.
4. Cloud side: the device's `last_seen` timestamp and RSSI as seen by the API, and
   whether the dashboard **Devices** tab shows it online.
5. If you fell back to `tls=0`/port 80, say so explicitly and include the NTP/TLS
   error that forced it.
6. Anything in §9 that fired.
7. The `vb: audio …` diagnostic line (§2b) for ~2 min — I read the mic verdict
   from `distinct`/`rms`/`lowbits`, not from `mic ready`.

That log is enough to iterate on telemetry, then the real audio path.

## 11. Deployment note (control plane) — bug found by Hermes

`requirements.txt` shipped without a WebSocket implementation, so a clean
`docker build` produced a server that logs **"No supported WebSocket library
detected"** and answers **every** `/v1/device/ws` upgrade with **404** — the
device edge is dead even though `hello`/telemetry/ping all work when the lib is
present. Fixed by adding `websockets>=12`.

Verified in a fresh venv install of `requirements.txt`: upgrade probe returns
**403** (auth reject, not 404) and a real client gets `hello→ack`,
`telemetry→ack`, `ping→pong`. If you deploy behind Caddy/nginx, terminate TLS
there and proxy to `uvicorn`; the firmware's cert bundle trusts **ISRG Root X1**,
so a Let's Encrypt cert validates.

### 11b. Device status lifecycle (also found by Hermes)

`device_ws.py` refreshed `last_seen`/`rssi` but never set `status='online'`, so a
live, acking device showed as **offline** (`/v1/ui/devices/.../status` →
`online:false`). Now:

- on connect → `status='online'`, `ip=<X-Forwarded-For or peer>`, `last_seen` set;
- `hello` / `telemetry` → also update `fw`/`fw_version`, `rssi`, `free_heap_kb`,
  `uptime_s`, and `ssid`/`ip` if the device sends them;
- on disconnect → `status='offline'`, `ip=NULL`.

Covered by `tests/integration/test_device_ws.py::test_ws_drives_online_status_and_clears_ip`.

## 12. Bench result (COM17, ESP32-WROOM-32, wss) — PASS

Flashed merged image `dcc0d983…` + NVS (`ssid=Oshadha`, `host=vb.site.gigies.lk`,
`port=443`, `tls=1`). Serial showed `sntp time set` **before** the TLS handshake,
`websocket connected, hello sent`, then `ack hello` and `ack telemetry` every
~15 s; dashboard read model reported the device with fresh `last_seen` + RSSI.
SNTP + cert bundle validated against a real Let's Encrypt cert. No §9 symptom
fired (ping/pong n/a — firmware doesn't ping).

### 12b. Round 2 (fw 0.2.0) — cloud + transport PASS, audio hardware-blocked

- **Re-sync verified:** `/v1/ui/devices/GBX-BENCH01/status` → `online:true`, RSSI
  populated, `connected_ip 175.157.151.216` (Caddy `X-Forwarded-For`), and the
  device row shows `fw`/`fw_version` = **0.2.0** — i.e. `hello` carried the tag.
- **App-only flash verified:** 951280 B written, hash matched, and an NVS read-back
  showed the config intact (app-only at `0x10000` leaves `0x9000` untouched).
- **Blip test PASS:** container restart mid-session → `websocket disconnected`,
  reconnect + re-auth by +19.9 s, telemetry resumes, `status` back to `online:true`.
- **Audio NOT passing, and not the code:** `audio peak` was a saturated constant
  (~32736…32768) then flat `0` across boots — the signature of an undriven DIN
  line. `mic ready` only proves the I2S channel initialised, not that anything is
  wired. Blocked on confirming the physical mic (part, pins, slot). fw 0.2.1 adds
  the boot probe + warning to make this explicit.
- **Serial note:** a `POWERON_RESET` at the top of a capture is COM17's DTR/RTS,
  not a crash.
