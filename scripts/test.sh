#!/usr/bin/env bash
# Run the test suite with hard timeouts so a hung test can never wedge the shell.
#
# Two layers of protection:
#   * pytest-timeout caps each individual test (default 60s), and
#   * the shell `timeout` caps the whole run (default 600s) as a backstop in case
#     the plugin is missing or a single hang is otherwise unstoppable.
#
# Usage:  ./scripts/test.sh [extra pytest args...]
# Tune caps:  PYTEST_TIMEOUT=30 PYTEST_OVERALL_TIMEOUT=300 ./scripts/test.sh
set -uo pipefail
cd "$(dirname "$0")/.."

PER_TEST="${PYTEST_TIMEOUT:-60}"
OVERALL="${PYTEST_OVERALL_TIMEOUT:-600}"

args=()
if python3 -c "import pytest_timeout" >/dev/null 2>&1; then
    args+=(--timeout="${PER_TEST}" --timeout-method=thread)
else
    echo "note: pytest-timeout not installed; relying on the ${OVERALL}s shell cap" >&2
fi

exec timeout "${OVERALL}" python3 -m pytest "${args[@]}" "$@"
