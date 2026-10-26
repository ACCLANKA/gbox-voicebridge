#!/usr/bin/env bash
# Build the CVSD-only CONTROL variant of the firmware.
#
# Identical to the primary image except that WBS (mSBC) is disabled. The AG
# advertises codec negotiation, so a 16 kHz mSBC negotiation that never completes
# is one way the eSCO could fail to be created; disabling WBS forces CVSD and is
# the clean control for that hypothesis. Flash ONLY if the primary image still
# fails to get SCO - it is a diagnostic, not a fix.
#
# Writes build/voicebridge-cvsd.bin and leaves the working tree untouched.
# Requires the ESP-IDF environment to be sourced first (export.sh).
set -euo pipefail
cd "$(dirname "$0")/../firmware"

cp sdkconfig sdkconfig.cvsd.bak
cp main/main.c main/main.c.cvsd.bak
restore() {
    mv -f sdkconfig.cvsd.bak sdkconfig
    mv -f main/main.c.cvsd.bak main/main.c
    # Restoring with mv gives these files the OLDER mtime of the backup, so a
    # following build would consider them unchanged and silently reuse the
    # CVSD-compiled objects / config. Touch them so the next build is honest.
    touch sdkconfig main/main.c
}
trap restore EXIT

sed -i 's/^CONFIG_BT_HFP_WBS_ENABLE=y/# CONFIG_BT_HFP_WBS_ENABLE is not set/' sdkconfig
VER="$(sed -n 's/^#define FW_VERSION "\(.*\)"/\1/p' main/main.c)"
sed -i "s/#define FW_VERSION \"$VER\"/#define FW_VERSION \"$VER-cvsd\"/" main/main.c

idf.py build

# Prove the image really is the CVSD variant before handing it over: the version
# string must carry the -cvsd suffix and the built config must have WBS off.
if ! grep -aq "$VER-cvsd" build/voicebridge.bin; then
    echo "FAILED: build/voicebridge.bin does not carry the '$VER-cvsd' version string" >&2
    exit 1
fi
if grep -q "^#define CONFIG_BT_HFP_WBS_ENABLE 1" build/config/sdkconfig.h; then
    echo "FAILED: this build still has CONFIG_BT_HFP_WBS_ENABLE=y" >&2
    exit 1
fi
cp build/voicebridge.bin build/voicebridge-cvsd.bin

echo "CVSD control build written: build/voicebridge-cvsd.bin"
echo -n "  sha256 "; sha256sum build/voicebridge-cvsd.bin | cut -d' ' -f1
