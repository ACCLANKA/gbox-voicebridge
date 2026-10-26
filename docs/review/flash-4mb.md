# Switching the board to its real 4 MB flash

## The evidence
The boot log settles it, and no board interaction is needed to read it:

    I (48) boot.esp32: SPI Flash Size : 4MB

The chip is 4 MB; the firmware was configured for 2 MB, which capped the app partition at 1500 K.
Configuring the part correctly and regenerating the table gives the app **4032 K**.

| | before | after |
|---|---|---|
| flash configured | 2 MB | 4 MB |
| app partition | 1500 K (0x10000 + 0x177000) | **4032 K** (0x10000 + 0x3F0000) |
| image | 1,334,096 | 1,333,760 |
| free in partition | 201,904 (13%) | **2,795,008 (68%)** |
| nvs / phy_init | 0x9000 24K / 0xf000 4K | unchanged |

## What changed, and why it is a three-region flash
- `firmware/partitions-4mb.csv` (new): nvs and phy_init keep their existing **offsets and sizes**
  so an existing NVS survives. Factory takes the rest.
- `CONFIG_ESPTOOLPY_FLASHSIZE` is both a bool and a **string**; esptool reads the string. Both are
  set, or the image is still built for a 2 MB part.
- `CONFIG_PARTITION_TABLE_CUSTOM=y` points at the new file.
- Built as `0.6.32-4mb` via `build-variant.sh 4mb VB_FLASH_4MB=1`, so it is self-identifying.

The bootloader and the partition table change, so **this is not an app-only flash**:

| region | offset | file |
|---|---|---|
| bootloader | 0x1000 | `voicebridge-bootloader-4mb.bin` |
| partition table | 0x8000 | `voicebridge-partition-table-4mb.bin` |
| application | 0x10000 | `voicebridge-app-cvsd-4mb-0.6.32.bin` |

## NVS is preserved if you write only those three
NVS is at 0x9000 and none of the three regions overlap it, so `write_flash 0x1000 ... 0x8000 ...
0x10000 ...` leaves it alone. **Back it up anyway** (`read_flash 0x9000 0x6000 nvs.bin`) - it holds
the device's host, port and serial, so wiping it means re-running the provisioning. If it is wiped
deliberately, re-apply the config rather than assuming the defaults.

## Before flashing this onto anything: confirm the part
This layout is **wrong and corrupting on a 2 MB module**. Check first, from the boot log
(`SPI Flash Size : ...`) or `esptool.py flash_id`. GBX-BENCH01 is confirmed 4 MB; **GBX-10001 is
unverified**, so it stays on the 2 MB image until someone reads its boot log.

## Rollback
Reflash all three regions from the 2 MB build (same offsets), for the same reason it took three to
get here. The old table is in the bundle at `voicebridge-partition-table.bin`.

## Why this matters beyond headroom
The old free space was 201 KB, which is why the payload work was constrained: Opus would have cost
about 100 KB, half of everything remaining. With 2.7 MB free, the codec choice is a design decision
again rather than a budget fight. It does not fix the link - only the payload rate and a decent
supply do that - but it stops the firmware size being the reason we cannot try something.
