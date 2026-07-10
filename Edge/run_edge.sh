#!/usr/bin/env bash
# run_edge.sh — Start health_agent + pipeline in a single command.
#
# Usage:
#   ./run_edge.sh                        # rtsp_push mode (default)
#   ./run_edge.sh --mode display         # display mode
#   ./run_edge.sh --mode rtsp_push --rtsp-push-url rtsp://host:8554/jetson_A
#   ./run_edge.sh --load-policy predict_with_base --load-model formula
#
# Press Ctrl+C once to gracefully stop both processes.

set -euo pipefail

EDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$EDGE_DIR"

PYTHON="${PYTHON:-python3}"
MODE="${MODE:-rtsp_push}"
LOAD_POLICY="${LOAD_POLICY:-actual}"
LOAD_MODEL="${LOAD_MODEL:-formula}"

# Parse any extra args passed to this script and forward to main.py
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --load-policy)
            if [[ $# -lt 2 ]]; then
                echo "[run_edge] ERROR: --load-policy requires a value" >&2
                exit 1
            fi
            LOAD_POLICY="$2"
            shift 2
            ;;
        --load-model)
            if [[ $# -lt 2 ]]; then
                echo "[run_edge] ERROR: --load-model requires a value" >&2
                exit 1
            fi
            LOAD_MODEL="$2"
            shift 2
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

case "$LOAD_POLICY" in
    actual|predict_no_base|predict_with_base) ;;
    *)
        echo "[run_edge] ERROR: LOAD_POLICY must be: actual | predict_no_base | predict_with_base" >&2
        exit 1
        ;;
esac

case "$LOAD_MODEL" in
    formula|dl) ;;
    *)
        echo "[run_edge] ERROR: LOAD_MODEL must be: formula | dl" >&2
        exit 1
        ;;
esac

export LOAD_POLICY LOAD_MODEL
echo "[run_edge] LOAD_POLICY=$LOAD_POLICY  LOAD_MODEL=$LOAD_MODEL"

# ---------------------------------------------------------------------------
# Cleanup: kill both child processes on Ctrl+C / EXIT
# ---------------------------------------------------------------------------
_pids=()
_cleanup() {
    echo ""
    echo "[run_edge] Stopping all processes..."
    for pid in "${_pids[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "[run_edge] Done."
}
trap _cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# 1. Start health_agent (WebSocket + Zenoh + metrics)
# ---------------------------------------------------------------------------
echo "[run_edge] Starting health_agent.py ..."
"$PYTHON" health_agent.py &
_pids+=($!)
HEALTH_PID=${_pids[-1]}

# Give health_agent time to connect to Zenoh and the server before the
# pipeline starts publishing events — avoids the first few events being
# dropped because the subscriber isn't ready yet.
sleep 2

# ---------------------------------------------------------------------------
# 2. Start main.py pipeline
# ---------------------------------------------------------------------------
if [ ${#EXTRA_ARGS[@]} -eq 0 ]; then
    echo "[run_edge] Starting main.py --mode $MODE ..."
    "$PYTHON" main.py --mode "$MODE" &
else
    echo "[run_edge] Starting main.py ${EXTRA_ARGS[*]} ..."
    "$PYTHON" main.py "${EXTRA_ARGS[@]}" &
fi
_pids+=($!)
PIPELINE_PID=${_pids[-1]}

echo "[run_edge] health_agent PID=$HEALTH_PID  |  pipeline PID=$PIPELINE_PID"
echo "[run_edge] Press Ctrl+C to stop both."

# ---------------------------------------------------------------------------
# Wait — exit if either child dies unexpectedly
# ---------------------------------------------------------------------------
while true; do
    sleep 2

    if ! kill -0 "$HEALTH_PID" 2>/dev/null; then
        echo "[run_edge] ERROR: health_agent exited unexpectedly. Stopping pipeline."
        exit 1
    fi

    if ! kill -0 "$PIPELINE_PID" 2>/dev/null; then
        echo "[run_edge] ERROR: pipeline exited unexpectedly. Stopping health_agent."
        exit 1
    fi
done
