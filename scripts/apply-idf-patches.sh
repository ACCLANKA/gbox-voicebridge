#!/usr/bin/env bash
# Apply the local ESP-IDF patches this firmware depends on. Idempotent: hunks that
# are already applied are skipped, so it is safe to re-run (e.g. after a fresh
# checkout of $IDF_PATH). Requires the ESP-IDF environment to be sourced.
#
#   0001-hf-client-at-dump-safe.patch
#       Caps Bluedroid's HFP AT dump buffer and moves it off the stack. Without
#       this, enabling BTA_HF_CLIENT_AT_DUMP (see firmware/main/CMakeLists.txt)
#       puts a 16 KB frame on a 4 KB task stack and panics the board - fw 0.6.3.
set -euo pipefail

IDF="${IDF_PATH:?source the ESP-IDF export.sh first}"
here="$(cd "$(dirname "$0")" && pwd)"

for p in "$here"/idf-patches/*.patch; do
    [ -e "$p" ] || continue
    name="$(basename "$p")"
    if patch -p1 -d "$IDF" --forward -r - < "$p"; then
        echo "applied   $name"
    else
        rc=$?
        if [ "$rc" -eq 1 ]; then
            echo "already applied (skipped)   $name"
        else
            echo "FAILED ($rc)   $name" >&2
            exit 1
        fi
    fi
done
