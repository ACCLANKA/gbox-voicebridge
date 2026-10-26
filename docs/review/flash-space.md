# Where the flash goes, and what each lever buys (measured)

## The constraint
The app partition is **1500 K** — `CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE` with
`CONFIG_ESPTOOLPY_FLASHSIZE_2MB`. As built the image was **1,482,752 B: 3% free**, and the
build said so. A second ceiling was tighter still: **IRAM at 94% (7,786 B free)**.

```
nvs,data,nvs,0x9000,24K
phy_init,data,phy,0xf000,4K
factory,app,factory,0x10000,1500K
```

## Root cause of the tightness
Nothing was wasteful in our code — the image was built **`-Og` with assertions on**, which is
ESP-IDF's *default* optimisation level ("Debug"). Nobody had ever chosen it; it is simply what
you get if you never switch to size. Measured with `scripts/flash-space-budget.sh`:

| Config | Image | Free in 1500 K | IRAM used |
|---|---|---|---|
| baseline `-Og` debug | 1,482,752 | 53,248 (3%) | 123,286 (94.06%) |
| **`-Os` only** | **build FAILS** — see below | — | — |
| **`-Os` + HFP AG off** | **1,334,096** | **201,904 (13%)** | **115,846 (88.4%)** |
| + log level INFO→WARN | 1,318,960 | 217,040 (14%) | (not kept) |

**Net: +145 KB of flash and +7.4 KB of IRAM, with no functionality removed.**

## The coupling worth knowing
`-Os` alone does **not** build:

```
components/bt/host/bluedroid/bta/hf_ag/bta_ag_sco.c:257:86:
  error: array subscript 1 is above array bounds of 'tBTA_AG_SCB[1]' [-Werror=array-bounds=]
```

The HFP **AG** (audio gateway) code has an out-of-bounds `SCB[1]` pattern that GCC's
`array-bounds` analysis only flags at `-Os` — and `-Werror` turns it into a failure. This
firmware is an HFP **client** (`esp_hf_client_*` only; the AG half is never called), so the fix
is to stop compiling a module we do not use rather than to paper over the warning:

```
CONFIG_COMPILER_OPTIMIZATION_SIZE=y
# CONFIG_BT_HFP_AG_ENABLE is not set
```

## Rejected, and why
- **Log level INFO→WARN buys only ~15 KB.** It strips every `ESP_LOGI` string, including the boot
  `reset reason:` line and the HF connection/audio state lines — the ones this investigation is
  still running on. 15 KB is not worth that. (If a future build needs it, the number is here.)
- **Shrinking the buffers.** `DL_BYTES` 4096 is a jitter allowance, not slack in the image.
- **Disabling WBS/mSBC.** It would remove a supported mode; `build-variant.sh` already turns WBS
  off for the bench variants, so this buys nothing here anyway.

## Not yet done: a bigger app partition
If the module really has **4 MB** of flash, the 2 MB setting is the real limit and a custom
partition table (a ~3 MB app) would triple the headroom with no code change at all. That needs
one fact I cannot establish from here: **the flash size the ROM reports at boot** (the boot log
prints it). It is in the serial captures, which live on the bench side. Until that is confirmed,
the partition table stays as-is — flashing an image for a 4 MB part onto a 2 MB part would corrupt it.

## Flash and rollback
The smaller image is a **drop-in**: same partition table, same offsets, same variant knobs
(`VB_CALL_COEX_PREFER=2`), only fewer bytes. Version bumped to **0.6.30** so the console's
`fw_version` distinguishes it from 0.6.29. Rollback is re-flashing 0.6.29 — no NVS or server
migration is involved.

## Reproduce
```
. /home/openhands/workspace/idf53/export.sh
bash scripts/flash-space-budget.sh benchwifi VB_CALL_COEX_PREFER=2
```
