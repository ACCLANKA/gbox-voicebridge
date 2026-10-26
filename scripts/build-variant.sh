#!/usr/bin/env bash
# Build a diagnostic variant of the firmware.
#
#   scripts/build-variant.sh <suffix> [DEFINE ...]
#
# Examples:
#   scripts/build-variant.sh retry VB_HF_SCO_RETRY=3
#   scripts/build-variant.sh nolisten VB_HF_NO_LISTEN_ARM=1
#
# Every variant is CVSD-only (WBS off) and gets a `-<suffix>` FW_VERSION so the image is
# self-identifying. Definitions are injected into the bt component's vb_bt_defs list
# BEFORE target_compile_definitions consumes it - appending at the end of the file is
# silently ignored (that cost one wasted build in 0.6.10). The working tree is restored
# on exit, and sdkconfig / main.c / CMakeLists.txt are touched so the next build cannot
# silently reuse this variant's objects.
#
# Writes build/voicebridge-<suffix>.bin.
set -euo pipefail
SUFFIX="${1:?usage: build-variant.sh <suffix> [DEFINE ...]}"
shift || true
cd "$(dirname "$0")/../firmware"

cp sdkconfig sdkconfig.var.bak
cp main/main.c main/main.c.var.bak
cp main/vb_bt.c main/vb_bt.c.var.bak
cp main/CMakeLists.txt main/CMakeLists.txt.var.bak
restore() {
    mv -f sdkconfig.var.bak sdkconfig
    mv -f main/main.c.var.bak main/main.c
    mv -f main/vb_bt.c.var.bak main/vb_bt.c
    mv -f main/CMakeLists.txt.var.bak main/CMakeLists.txt
    touch sdkconfig main/main.c main/vb_bt.c main/CMakeLists.txt
}
trap restore EXIT

sed -i 's/^CONFIG_BT_HFP_WBS_ENABLE=y/# CONFIG_BT_HFP_WBS_ENABLE is not set/' sdkconfig
VER="$(sed -n 's/^#define FW_VERSION "\(.*\)"/\1/p' main/main.c)"
sed -i "s/#define FW_VERSION \"$VER\"/#define FW_VERSION \"$VER-$SUFFIX\"/" main/main.c
for d in "$@"; do
    case "$d" in
        # Firmware-side knob: lives in the MAIN component, which gets no definitions from
        # CMake, so it has to be patched into the source. Everything else goes to bt/.
        VB_SCO_RETRY=*)
            sed -i "s/^#define VB_HF_SCO_RETRY .*/#define VB_HF_SCO_RETRY ${d#*=}/" main/vb_bt.c
            echo "  patched main/vb_bt.c: VB_HF_SCO_RETRY=${d#*=}"
            ;;
        # A Kconfig string, so it lives in sdkconfig (CONFIG_ style), not in a source define.
        # The serial must match a device row in the tenant's account: this is how a production
        # unit announces itself as, say, GBX-10001 instead of the GBX-XXXXXX placeholder.
        # 4MB flash layout. Needs its own table AND a matching FLASHSIZE (both the bool and the
        # string - the string is what esptool reads). Changes the partition table, so the
        # bootloader and table must be reflashed too, not just the app.
        VB_FLASH_4MB=1)
            sed -i 's/^CONFIG_ESPTOOLPY_FLASHSIZE_2MB=y/# CONFIG_ESPTOOLPY_FLASHSIZE_2MB is not set/' sdkconfig
            sed -i 's/^CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y/# CONFIG_ESPTOOLPY_FLASHSIZE_4MB is not set/' sdkconfig
            sed -i 's/^CONFIG_ESPTOOLPY_FLASHSIZE=".*"/CONFIG_ESPTOOLPY_FLASHSIZE="4MB"/' sdkconfig
            grep -q '^CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y' sdkconfig || echo 'CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y' >> sdkconfig
            sed -i 's/^CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE=y/# CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE is not set/' sdkconfig
            sed -i 's/^CONFIG_PARTITION_TABLE_CUSTOM=y/# CONFIG_PARTITION_TABLE_CUSTOM is not set/' sdkconfig
            grep -q '^CONFIG_PARTITION_TABLE_CUSTOM=y' sdkconfig || echo 'CONFIG_PARTITION_TABLE_CUSTOM=y' >> sdkconfig
            sed -i 's|^CONFIG_PARTITION_TABLE_CUSTOM_FILENAME=.*|CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions-4mb.csv"|' sdkconfig
            grep -q '^CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions-4mb.csv"' sdkconfig || echo 'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions-4mb.csv"' >> sdkconfig
            echo '  sdkconfig: 4MB flash + partitions-4mb.csv (reflash bootloader + table too)'
            ;;
        VB_SERIAL=*)
            val="${d#*=}"
            sed -i "/^CONFIG_VB_SERIAL=/d" sdkconfig
            echo "CONFIG_VB_SERIAL=\"$val\"" >> sdkconfig
            echo "  sdkconfig: CONFIG_VB_SERIAL=\"$val\""
            ;;
        # Bench knobs whose DEFAULT lives in main/vb_bt.c. They have to be patched at the
        # definition site: the CMake -D route reaches the bt component but not this file
        # (verified in 0.6.20 - a bench image built byte-identical to the listen one
        # because the defines never arrived, which is a whole wasted test cycle).
        VB_LOCAL_PIP=*|VB_LOCAL_PIP_MIX=*|VB_LOCAL_TONE_SEC=*|VB_LOCAL_ECHO=*|VB_CALL_COEX_PREFER=*|VB_CALL_PS_BIAS=*|VB_ULAW=*)
            key="${d%%=*}"
            val="${d#*=}"
            sed -i "s/^#define $key .*/#define $key $val/" main/vb_bt.c
            if ! grep -q "^#define $key $val$" main/vb_bt.c; then
                echo "FAILED: $key=$val is not in main/vb_bt.c (no such definition site)" >&2
                exit 1
            fi
            echo "  patched main/vb_bt.c: $key=$val"
            ;;
        # Same trap, main component side.
        VB_WS_ECHO=*|VB_WS_NET_TIMEOUT_MS=*)
            key="${d%%=*}"
            val="${d#*=}"
            sed -i "s/^#define $key .*/#define $key $val/" main/main.c
            if ! grep -q "^#define $key $val$" main/main.c; then
                echo "FAILED: $key=$val is not in main/main.c (no such definition site)" >&2
                exit 1
            fi
            echo "  patched main/main.c: $key=$val"
            ;;
        *)
            sed -i "/^idf_component_get_property(bt_lib/i list(APPEND vb_bt_defs $d)" main/CMakeLists.txt
            echo "  injected into bt component: $d"
            ;;
    esac
done

idf.py build

if ! grep -aq "$VER-$SUFFIX" build/voicebridge.bin; then
    echo "FAILED: build/voicebridge.bin does not carry the '$VER-$SUFFIX' version string" >&2
    exit 1
fi
if grep -q "^#define CONFIG_BT_HFP_WBS_ENABLE 1" build/config/sdkconfig.h; then
    echo "FAILED: this build still has CONFIG_BT_HFP_WBS_ENABLE=y" >&2
    exit 1
fi
cp build/voicebridge.bin "build/voicebridge-$SUFFIX.bin"

echo "variant build written: build/voicebridge-$SUFFIX.bin"
echo -n "  sha256 "; sha256sum "build/voicebridge-$SUFFIX.bin" | cut -d' ' -f1
