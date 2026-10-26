#!/usr/bin/env bash
# Build the NO-LISTEN experiment variant (fw 0.6.10-nolisten).
#
# Same as the CVSD control image plus VB_HF_NO_LISTEN_ARM, which makes
# bta_hf_client_sco_listen() a no-op: we never arm the SCO listen, so there is no
# listen registration to remove right before we originate. This tests Hermes's
# hypothesis (a) that the remove->create sequence provokes HCI_ERR_MAX_NUM_OF_SCOS.
# The source says the listen sends no HCI command (BTM_CreateSco(is_orig=FALSE) only
# sets SCO_ST_LISTENING; BTM_RemoveSco on such an entry takes the "no HCI handle"
# early return), so (a) is expected to be disproved - this just makes it empirical.
# Caveat: it also disables accepting an AG-initiated eSCO (btm_sco_conn_req finds no
# LISTENING entry), which is why the plain -cvsd image is the one to run first.
#
# Writes build/voicebridge-nolisten.bin and leaves the working tree untouched.
set -euo pipefail
cd "$(dirname "$0")/../firmware"

cp sdkconfig sdkconfig.nl.bak
cp main/main.c main/main.c.nl.bak
cp main/CMakeLists.txt main/CMakeLists.txt.nl.bak
restore() {
    mv -f sdkconfig.nl.bak sdkconfig
    mv -f main/main.c.nl.bak main/main.c
    mv -f main/CMakeLists.txt.nl.bak main/CMakeLists.txt
    touch sdkconfig main/main.c main/CMakeLists.txt
}
trap restore EXIT

sed -i 's/^CONFIG_BT_HFP_WBS_ENABLE=y/# CONFIG_BT_HFP_WBS_ENABLE is not set/' sdkconfig
VER="$(sed -n 's/^#define FW_VERSION "\(.*\)"/\1/p' main/main.c)"
sed -i "s/#define FW_VERSION \"$VER\"/#define FW_VERSION \"$VER-nolisten\"/" main/main.c
sed -i "/^idf_component_get_property(bt_lib/i list(APPEND vb_bt_defs VB_HF_NO_LISTEN_ARM=1)" main/CMakeLists.txt

idf.py build

if ! grep -aq "$VER-nolisten" build/voicebridge.bin; then
    echo "FAILED: build/voicebridge.bin does not carry the '$VER-nolisten' version string" >&2
    exit 1
fi
if ! grep -aq "listen NOT armed" build/voicebridge.bin; then
    echo "FAILED: VB_HF_NO_LISTEN_ARM is not in the image" >&2
    exit 1
fi
if grep -q "^#define CONFIG_BT_HFP_WBS_ENABLE 1" build/config/sdkconfig.h; then
    echo "FAILED: this build still has CONFIG_BT_HFP_WBS_ENABLE=y" >&2
    exit 1
fi
cp build/voicebridge.bin build/voicebridge-nolisten.bin

echo "no-listen experiment build written: build/voicebridge-nolisten.bin"
echo -n "  sha256 "; sha256sum build/voicebridge-nolisten.bin | cut -d' ' -f1
