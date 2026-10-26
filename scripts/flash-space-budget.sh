#!/usr/bin/env bash
# Measure what each flash-space lever actually buys, by building with it on and off.
#
# The app partition is 1500K (single-app-large on a 2MB flash setting) and the image had
# 3% free, so "which knob buys what" was a guess. This turns it into numbers.
#
# Levers measured, in order:
#   A  -Os instead of the IDF default -Og debug build
#   B  + HFP AG disabled (this firmware is an HFP *client*; the AG half is linked in for nothing)
#   C  + log default level INFO -> WARN (measured only, then reverted - see the note below)
#
# The configuration we keep is B: A is unconditionally right for a production image, B is
# provably unused code, and C is deliberately NOT kept because it strips ESP_LOGI strings -
# including the boot "reset reason:" line and the HF state lines we are still debugging with.
# Its number is here so the choice can be made with the cost in view.
#
# Usage: scripts/flash-space-budget.sh [variant] [extra knobs...]
set -u
cd "$(dirname "$0")/.." || exit 1

VARIANT="${1:-benchwifi}"
shift 2>/dev/null || true
KNOBS=("$@")
PART=1536000

set_cfg() {
    python3 - "$@" <<'PY'
import re, sys
p = 'firmware/sdkconfig'
s = open(p).read()
for a in sys.argv[1:]:
    k, v = a.split('=', 1)
    line = ('# %s is not set' % k) if v == 'n' else '%s=%s' % (k, v)
    pat = re.compile(r'^#? ?' + re.escape(k) + r'[= ].*$', re.M)
    s = pat.sub(line, s, count=1) if pat.search(s) else s.rstrip('\n') + '\n' + line + '\n'
open(p, 'w').write(s)
PY
}

show() {
    local sz
    sz=$(stat -c%s "firmware/build/voicebridge-${VARIANT}.bin")
    printf '   image=%s  free_in_%sK=%s  (%s%%)\n' \
        "$sz" "$((PART / 1024))" "$((PART - sz))" "$(((PART - sz) * 100 / PART))"
}

build() {
    bash scripts/build-variant.sh "$VARIANT" "${KNOBS[@]}" >"/tmp/space-$1.log" 2>&1
    echo "   build=$?"
    grep -iE "error:" "/tmp/space-$1.log" | head -3
}

sz0=$(stat -c%s "firmware/build/voicebridge-${VARIANT}.bin")
printf 'baseline (-Og debug): image=%s free=%s (%s%%)\n' \
    "$sz0" "$((PART - sz0))" "$(((PART - sz0) * 100 / PART))"

echo "=== A: -Os (size optimisation) ==="
set_cfg CONFIG_COMPILER_OPTIMIZATION_SIZE=y \
        CONFIG_COMPILER_OPTIMIZATION_DEBUG=n \
        CONFIG_COMPILER_OPTIMIZATION_DEFAULT=n \
        CONFIG_COMPILER_OPTIMIZATION_LEVEL_DEBUG=n
build A; show

echo "=== B: + HFP AG off (we are a client) ==="
set_cfg CONFIG_BT_HFP_AG_ENABLE=n
build B; show
cp "firmware/build/voicebridge-${VARIANT}.bin" \
   "firmware/build/handoff/voicebridge-app-cvsd-${VARIANT}-os.bin"

echo "=== C: + log default level INFO -> WARN (measured only, not kept) ==="
set_cfg CONFIG_LOG_DEFAULT_LEVEL_WARN=y CONFIG_LOG_DEFAULT_LEVEL_INFO=n
build C; show

echo "=== revert the log level: B is the configuration we keep ==="
set_cfg CONFIG_LOG_DEFAULT_LEVEL_INFO=y CONFIG_LOG_DEFAULT_LEVEL_WARN=n
grep -nE "COMPILER_OPTIMIZATION|BT_HFP_AG_ENABLE|LOG_DEFAULT_LEVEL" firmware/sdkconfig | head
