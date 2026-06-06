#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export STRESS_SHOWFPS="${STRESS_SHOWFPS:-0}"
export STRESS_CAPTURE_DURING_HOLD="${STRESS_CAPTURE_DURING_HOLD:-1}"
export STRESS_FORWARD_SECS="${STRESS_FORWARD_SECS:-3}"
export STRESS_TURN_SECS="${STRESS_TURN_SECS:-2}"
export STRESS_STRAFE_SECS="${STRESS_STRAFE_SECS:-3}"
export SMOKE_BUILD_TARGET="${SMOKE_BUILD_TARGET:-chunk-playable-debug-rom}"
export SMOKE_RUN_TARGET="${SMOKE_RUN_TARGET:-chunk-playable-debug-gngeo}"
export SMOKE_OUTPUT_DIR="${SMOKE_OUTPUT_DIR:-.tools/screens/latest/chunk-debug-movement}"
export SMOKE_LOG="${SMOKE_LOG:-.tools/logs/chunk-debug-movement-gngeo.log}"
export SMOKE_MAKE_ARGS="${SMOKE_MAKE_ARGS:-ROM=build/chunk-playable-debug-rom}"
export MOVEMENT_CHECK_ARGS="${MOVEMENT_CHECK_ARGS:---min-play-colored 50000 --min-play-varied 20 --min-diff-mean 1 --min-diff-pixels 1000}"
CHECK_ARGS=()
if [ -n "${MOVEMENT_CHECK_ARGS:-}" ]; then
    # shellcheck disable=SC2206
    CHECK_ARGS=($MOVEMENT_CHECK_ARGS)
fi

tools/stress_movement.sh
status=0
tools/check_chunk_debug_screens.py --dir "$SMOKE_OUTPUT_DIR" || status=1
tools/check_movement_screens.py --dir "$SMOKE_OUTPUT_DIR" --expect-frame-stats "${CHECK_ARGS[@]}" || status=1
if [ ! -f "$SMOKE_LOG" ]; then
    echo "chunk debug movement log missing: $SMOKE_LOG" >&2
    exit 1
fi
if grep -q 'Invalid write' "$SMOKE_LOG"; then
    echo "chunk debug movement saw invalid emulator writes in $SMOKE_LOG" >&2
    grep 'Invalid write' "$SMOKE_LOG" | head -20 >&2
    exit 1
fi

echo "chunk debug movement log: $SMOKE_LOG"
exit "$status"
