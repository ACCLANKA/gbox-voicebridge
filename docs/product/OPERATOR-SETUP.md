# Operator setup: what you configure, and what is already decided

Everything here is what an operator must do. Anything not listed has a default that should not be
changed without a reason.

## 1. Flash the right image for your part

**Check the flash size first, from the boot log:**

    I (48) boot.esp32: SPI Flash Size : 4MB

| part | image to flash | app partition |
|---|---|---|
| 4 MB | `...-4mb-0.6.35.bin` + `bootloader-4mb.bin` + `partition-table-4mb.bin` | 4032 K |
| 2 MB | the plain `...-0.6.35.bin`, app only | 1500 K |

The 4 MB layout is **wrong and corrupting on a 2 MB module**. It is a three-region flash
(bootloader 0x1000, table 0x8000, app 0x10000) because the table changes; nvs (0x9000) and phy_init
(0xf000) keep their offsets, so an existing NVS survives. Back it up anyway:
`esptool.py read_flash 0x9000 0x6000 nvs.bin`. See `docs/review/flash-4mb.md`.

## 2. Provision the device

The device config holds: **host, port, TLS on/off, serial, token**. It lives in NVS. The serial must
match a device row in your account - that is how a unit announces itself as, say, `GBX-10001` instead
of the placeholder. Nothing else needs configuring: volumes, codec, auto-answer and the audio levels
all have working defaults.

## 3. Deploy the server at the same revision as the firmware

Two things the firmware relies on the server for, both of which fail silently if the server is older:

* **telemetry persistence** - without it the counters are computed and dropped, and a board on mains
  power (no USB) cannot be measured at all;
* **the mu-law decoder (kind 0x04)** - without it a mu-law frame is an unknown kind. The board only
  sends mu-law when built with `VB_ULAW=1`, and it is off by default, so a mixed fleet is safe: an
  old server and a PCM board work together.

Check with: `GET /v1/ops/ping` for ops reachability, and a device row showing `tx_ok`/`ul_owv`
columns populated after a call.

## 4. Bench knobs and what they do (never ship these on)

| knob | effect |
|---|---|
| `VB_LOCAL_TONE_SEC=N` | N seconds of 1 kHz tone at call start. 0 = off. Was the "beep". |
| `VB_LOCAL_ECHO=1` | the board loops its own mic to its own earpiece, no cloud. Proves the phone↔board path alone. |
| `VB_WS_ECHO=1` | appends `&echo=1`; the server echoes the uplink back. Proves the whole chain. |
| `VB_ULAW=1` | mu-law uplink: half the bytes per frame. Off until the far end decodes 0x04. |
| `VB_FLASH_4MB=1` | the 4 MB layout + table (see 1). |
| `VB_SCO_RETRY=0` | listen-only SCO instead of originate. Diagnostic; the default (1) matches the product. |

Build them with `scripts/build-variant.sh <suffix> KNOB=VALUE ...`. Each image self-identifies in its
version string, and `ulk=` on the per-second in-call line tells you which payload codec it is using.

## 5. What to watch on a call

Per-second `in-call tx:` line: `ok`/`frames` (delivered), `drops`, `fail_streak`, `tx_age_s`,
`skip` (audio deliberately not offered), `boffmax` (deepest backoff), `wsr` (socket restarts),
`noconn`/`short` (never attempted / transport could not finish - `short` should be ~0), `ulk` (codec).

Per-call `dl:` line: `fills`/`partial`/`empty` (earpiece feed), `gap_max_ms` (worst arrival gap),
`gaps200` (how many arrivals were >200 ms apart - a count, not a worst case), `ul_ovw`.

`gap_max_ms` is a **running maximum**, not a per-window value: a repeated number is one event
holding, not several.
