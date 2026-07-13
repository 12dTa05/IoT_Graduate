#!/usr/bin/env bash
# run_edge.sh — Start health_agent + pipeline in a single command.
#
# Usage:
#   ./run_edge.sh                        # rtsp_push mode (default)
#   ./run_edge.sh --mode display         # display mode
#   ./run_edge.sh --mode rtsp_push --rtsp-push-url rtsp://host:8554/jetson_A
#   ./run_edge.sh --load-policy predict_with_base --load-model formula
#
#   # Collect calibration data while the pipeline runs, then stop automatically:
#   ./run_edge.sh --collect
#   ./run_edge.sh --collect --collect-output logs/calibration.csv \
#                           --collect-duration 600 \
#                           --collect-wbase-ref 12.5
#
# Press Ctrl+C once to gracefully stop both processes.

set -euo pipefail

EDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$EDGE_DIR"

PYTHON="${PYTHON:-python3}"
MODE="${MODE:-rtsp_push}"
LOAD_POLICY="${LOAD_POLICY:-actual}"
LOAD_MODEL="${LOAD_MODEL:-formula}"

# Collection defaults
COLLECT=0
COLLECT_OUTPUT="logs/calibration.csv"
COLLECT_DURATION=600
COLLECT_INTERVAL=2.0
COLLECT_WBASE_REF=0.0

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
        --collect)
            COLLECT=1
            shift
            ;;
        --collect-output)
            if [[ $# -lt 2 ]]; then
                echo "[run_edge] ERROR: --collect-output requires a value" >&2
                exit 1
            fi
            COLLECT_OUTPUT="$2"
            shift 2
            ;;
        --collect-duration)
            if [[ $# -lt 2 ]]; then
                echo "[run_edge] ERROR: --collect-duration requires a value" >&2
                exit 1
            fi
            COLLECT_DURATION="$2"
            shift 2
            ;;
        --collect-interval)
            if [[ $# -lt 2 ]]; then
                echo "[run_edge] ERROR: --collect-interval requires a value" >&2
                exit 1
            fi
            COLLECT_INTERVAL="$2"
            shift 2
            ;;
        --collect-wbase-ref)
            if [[ $# -lt 2 ]]; then
                echo "[run_edge] ERROR: --collect-wbase-ref requires a value" >&2
                exit 1
            fi
            COLLECT_WBASE_REF="$2"
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
# 3. (Optional) Start profile_collect.py and stop everything when it finishes
# ---------------------------------------------------------------------------
COLLECT_PID=""
if [[ "$COLLECT" -eq 1 ]]; then
    # Wait for the pipeline's FPS stats file to appear (written every 2s after first frame)
    echo "[run_edge] --collect: waiting for pipeline to produce first FPS stats..."
    WAIT_S=0
    until [[ -f /dev/shm/speedflow_fps.json ]] || [[ $WAIT_S -ge 30 ]]; do
        sleep 1
        (( WAIT_S++ )) || true
    done
    if [[ ! -f /dev/shm/speedflow_fps.json ]]; then
        echo "[run_edge] WARNING: FPS stats file not found after 30s — starting collector anyway."
    fi

    echo "[run_edge] Starting profile_collect.py → $COLLECT_OUTPUT  (${COLLECT_DURATION}s, interval=${COLLECT_INTERVAL}s, wbase_ref=${COLLECT_WBASE_REF})"
    "$PYTHON" tools/profile_collect.py \
        --output       "$COLLECT_OUTPUT" \
        --duration     "$COLLECT_DURATION" \
        --interval     "$COLLECT_INTERVAL" \
        --wbase-ref    "$COLLECT_WBASE_REF" &
    COLLECT_PID=$!
    _pids+=("$COLLECT_PID")
    echo "[run_edge] profile_collect PID=$COLLECT_PID"
fi

# ---------------------------------------------------------------------------
# Wait — exit if either child dies unexpectedly;
# if --collect is active, exit cleanly when the collector finishes.
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

    # Collector finished → stop the pipeline gracefully
    if [[ -n "$COLLECT_PID" ]] && ! kill -0 "$COLLECT_PID" 2>/dev/null; then
        echo "[run_edge] Collection complete → $COLLECT_OUTPUT"
        echo "[run_edge] Stopping pipeline and health_agent."
        exit 0
    fi
done
