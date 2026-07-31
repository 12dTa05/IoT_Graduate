#!/usr/bin/env bash
# run_edge.sh — Start health_agent + pipeline in a single command.
#
# Normal usage:
#   ./run_edge.sh                        # rtsp_push mode (default)
#   ./run_edge.sh --mode display
#   ./run_edge.sh --mode rtsp_push --rtsp-push-url rtsp://host:8554/jetson_A
#   ./run_edge.sh --load-policy predict_with_base --load-model formula
#   ./run_edge.sh --telemetry-interval 0.5   # snapshot cadence (default: 1.0 s)
#
# Collect calibration data while the pipeline runs, then stop automatically:
#   ./run_edge.sh --collect
#   ./run_edge.sh --collect --collect-output logs/calibration.csv \
#                           --collect-duration 600 \
#                           --collect-wbase-ref 12.5
#
# Full 6-step automated calibration pipeline (wbase → collect → fit/train → plot):
#   ./run_edge.sh --calibrate
#   ./run_edge.sh --calibrate --load-model dl \
#                             --collect-duration 1200 \
#                             --collect-output logs/calibration.csv \
#                             --wbase-output   logs/wbase.txt \
#                             --wbase-duration 60 \
#                             --model-output   models/load_predictor.onnx \
#                             --plot-rmse      logs/chart1_rmse.png \
#                             --plot-burst     logs/chart2_burst.png
#
# Train DL model from pre-collected multi-case CSVs (no pipeline):
#   ./run_edge.sh --train-dataset csv_collected
#   ./run_edge.sh --train-dataset csv_collected \
#                 --model-output   models/load_predictor.onnx \
#                 --collect-interval 2.0 \
#                 --load-policy    predict_with_base
#
# Press Ctrl+C once to gracefully stop all processes.

set -euo pipefail

EDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$EDGE_DIR"

mkdir -p logs
RUN_LOG="${RUN_LOG:-logs/run_$(date +%Y%m%d_%H%M%S).log}"
exec > >(tee -a "$RUN_LOG") 2>&1
echo "[run_edge] Runtime log: $RUN_LOG"

PYTHON="${PYTHON:-python3}"
MODE="${MODE:-rtsp_push}"
LOAD_POLICY="${LOAD_POLICY:-actual}"
LOAD_MODEL="${LOAD_MODEL:-formula}"
TELEMETRY_INTERVAL="${TELEMETRY_INTERVAL:-1.0}"

# --collect defaults
COLLECT=0
COLLECT_OUTPUT="logs/calibration.csv"
COLLECT_DURATION=600
COLLECT_INTERVAL=2.0
COLLECT_WBASE_REF=0.0

# --calibrate defaults (superset of --collect)
CALIBRATE=0
WBASE_OUTPUT="logs/wbase.txt"
WBASE_DURATION=60
MODEL_OUTPUT="models/load_predictor.onnx"
PLOT_RMSE="logs/chart1_rmse.png"
PLOT_BURST="logs/chart2_burst.png"

# --train-dataset (multi-case DL training, no pipeline)
TRAIN_DATASET=""

# Parse args — collect/calibrate flags consumed here; rest forwarded to main.py
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --load-policy)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --load-policy requires a value" >&2; exit 1; }
            LOAD_POLICY="$2"; shift 2 ;;
        --load-model)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --load-model requires a value" >&2; exit 1; }
            LOAD_MODEL="$2"; shift 2 ;;
        --telemetry-interval)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --telemetry-interval requires a value" >&2; exit 1; }
            TELEMETRY_INTERVAL="$2"; shift 2 ;;
        --collect)
            COLLECT=1; shift ;;
        --calibrate)
            CALIBRATE=1; COLLECT=1; shift ;;
        --collect-output)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --collect-output requires a value" >&2; exit 1; }
            COLLECT_OUTPUT="$2"; shift 2 ;;
        --collect-duration)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --collect-duration requires a value" >&2; exit 1; }
            COLLECT_DURATION="$2"; shift 2 ;;
        --collect-interval)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --collect-interval requires a value" >&2; exit 1; }
            COLLECT_INTERVAL="$2"; shift 2 ;;
        --collect-wbase-ref)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --collect-wbase-ref requires a value" >&2; exit 1; }
            COLLECT_WBASE_REF="$2"; shift 2 ;;
        --wbase-output)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --wbase-output requires a value" >&2; exit 1; }
            WBASE_OUTPUT="$2"; shift 2 ;;
        --wbase-duration)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --wbase-duration requires a value" >&2; exit 1; }
            WBASE_DURATION="$2"; shift 2 ;;
        --model-output)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --model-output requires a value" >&2; exit 1; }
            MODEL_OUTPUT="$2"; shift 2 ;;
        --plot-rmse)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --plot-rmse requires a value" >&2; exit 1; }
            PLOT_RMSE="$2"; shift 2 ;;
        --plot-burst)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --plot-burst requires a value" >&2; exit 1; }
            PLOT_BURST="$2"; shift 2 ;;
        --train-dataset)
            [[ $# -lt 2 ]] && { echo "[run_edge] ERROR: --train-dataset requires a directory" >&2; exit 1; }
            TRAIN_DATASET="$2"; shift 2 ;;
        *)
            EXTRA_ARGS+=("$1"); shift ;;
    esac
done

# ===========================================================================
# --train-dataset: clean + train DL model from pre-collected CSVs, then exit
# ===========================================================================
if [[ -n "$TRAIN_DATASET" ]]; then
    if [[ ! -d "$TRAIN_DATASET" ]]; then
        echo "[run_edge] ERROR: --train-dataset not a directory: $TRAIN_DATASET" >&2
        exit 1
    fi
    if [[ "$LOAD_MODEL" != "dl" ]]; then
        echo "[run_edge] ERROR: --train-dataset requires --load-model dl (got: $LOAD_MODEL)" >&2
        exit 1
    fi

    CLEANED_DIR="logs/cleaned"
    echo "[run_edge] ── Cleaning CSVs: $TRAIN_DATASET → $CLEANED_DIR ──"
    rm -rf "$CLEANED_DIR"
    "$PYTHON" tools/clean_collected_csvs.py \
        --input-dir  "$TRAIN_DATASET" \
        --output-dir "$CLEANED_DIR"
    TRAIN_CSV="$CLEANED_DIR/load_prediction_clean.csv"
    if [[ ! -f "$TRAIN_CSV" ]]; then
        echo "[run_edge] ERROR: cleaning produced no $TRAIN_CSV" >&2
        exit 1
    fi

    HORIZON_ROWS=$(python3 -c "import math; print(max(1, round(10 / ${COLLECT_INTERVAL})))")
    if [[ "$LOAD_POLICY" == "predict_no_base" ]]; then
        echo "[run_edge] Running train_dl_model.py (target=delta_load, window_k=5, horizon_rows=$HORIZON_ROWS)"
        mkdir -p "$(dirname "$MODEL_OUTPUT")"
        "$PYTHON" tools/train_dl_model.py \
            --csv          "$TRAIN_CSV" \
            --target       delta_load \
            --window-k     5 \
            --horizon-rows "$HORIZON_ROWS" \
            --epochs       200 \
            --output       "$MODEL_OUTPUT"
    else
        echo "[run_edge] Running train_dl_model.py (target=<auto>, window_k=5, horizon_rows=$HORIZON_ROWS)"
        mkdir -p "$(dirname "$MODEL_OUTPUT")"
        "$PYTHON" tools/train_dl_model.py \
            --csv          "$TRAIN_CSV" \
            --window-k     5 \
            --horizon-rows "$HORIZON_ROWS" \
            --epochs       200 \
            --output       "$MODEL_OUTPUT"
    fi
    echo "[run_edge] ONNX model written to $MODEL_OUTPUT"
    echo "[run_edge] ACTION REQUIRED: set 'enabled: true' in configs/edge_node.yml → proactive: section, then restart."
    exit 0
fi

# --- Normal pipeline path below this point ---

case "$LOAD_POLICY" in
    actual|predict_no_base|predict_with_base) ;;
    *)
        echo "[run_edge] ERROR: LOAD_POLICY must be: actual | predict_no_base | predict_with_base" >&2
        exit 1 ;;
esac

case "$LOAD_MODEL" in
    formula|dl) ;;
    *)
        echo "[run_edge] ERROR: LOAD_MODEL must be: formula | dl" >&2
        exit 1 ;;
esac

if ! [[ "$TELEMETRY_INTERVAL" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] || \
   ! awk "BEGIN { exit !($TELEMETRY_INTERVAL > 0) }"; then
    echo "[run_edge] ERROR: --telemetry-interval must be a positive number" >&2
    exit 1
fi

export LOAD_POLICY LOAD_MODEL TELEMETRY_INTERVAL
echo "[run_edge] LOAD_POLICY=$LOAD_POLICY  LOAD_MODEL=$LOAD_MODEL  TELEMETRY_INTERVAL=${TELEMETRY_INTERVAL}s"

# ---------------------------------------------------------------------------
# Cleanup: kill all tracked child processes on Ctrl+C / EXIT
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

# ===========================================================================
# STEP 1 (--calibrate only): Measure W_base — idle GPU load, no pipeline
# ===========================================================================
if [[ "$CALIBRATE" -eq 1 ]]; then
    echo ""
    echo "[run_edge] ── STEP 1/6: Measuring W_base (${WBASE_DURATION}s, no pipeline) ──"
    mkdir -p "$(dirname "$WBASE_OUTPUT")"
    "$PYTHON" tools/profile_collect.py \
        --wbase \
        --wbase-duration  "$WBASE_DURATION" \
        --wbase-output    "$WBASE_OUTPUT"

    # Read the measured value and use it as wbase-ref for collection
    if [[ -f "$WBASE_OUTPUT" ]]; then
        COLLECT_WBASE_REF="$(grep -oP '[\d.]+' "$WBASE_OUTPUT" | head -1)"
        echo "[run_edge] W_base = ${COLLECT_WBASE_REF}% GPU  (saved to $WBASE_OUTPUT)"
    else
        echo "[run_edge] WARNING: wbase output not found, using COLLECT_WBASE_REF=${COLLECT_WBASE_REF}"
    fi
fi

# ===========================================================================
# STEP 2: Start health_agent (WebSocket + Zenoh + metrics)
# ===========================================================================
_STEP2_LABEL="STEP 2"
[[ "$CALIBRATE" -eq 1 ]] && _STEP2_LABEL="STEP 2/6"
echo ""
echo "[run_edge] ── ${_STEP2_LABEL}: Starting health_agent.py ──"
"$PYTHON" health_agent.py &
_pids+=($!)
HEALTH_PID=${_pids[-1]}

# Give health_agent time to connect to Zenoh and the monitoring server
sleep 2

# ===========================================================================
# STEP 3: Start main.py pipeline
# ===========================================================================
_STEP3_LABEL="STEP 3"
[[ "$CALIBRATE" -eq 1 ]] && _STEP3_LABEL="STEP 3/6"
echo "[run_edge] ── ${_STEP3_LABEL}: Starting pipeline (mode=$MODE) ──"
if [ ${#EXTRA_ARGS[@]} -eq 0 ]; then
    "$PYTHON" main.py --mode "$MODE" &
else
    "$PYTHON" main.py "${EXTRA_ARGS[@]}" &
fi
_pids+=($!)
PIPELINE_PID=${_pids[-1]}
echo "[run_edge] health_agent PID=$HEALTH_PID  |  pipeline PID=$PIPELINE_PID"

# ===========================================================================
# STEP 4 (--collect / --calibrate): Run profile_collect.py alongside pipeline
# ===========================================================================
COLLECT_PID=""
if [[ "$COLLECT" -eq 1 ]]; then
    _STEP4_LABEL="STEP 4"
    [[ "$CALIBRATE" -eq 1 ]] && _STEP4_LABEL="STEP 4/6"
    echo ""
    echo "[run_edge] ── ${_STEP4_LABEL}: Waiting for pipeline FPS stats before collecting... ──"

    WAIT_S=0
    until [[ -f /dev/shm/speedflow_fps.json ]] || [[ $WAIT_S -ge 30 ]]; do
        sleep 1
        (( WAIT_S++ )) || true
    done
    if [[ ! -f /dev/shm/speedflow_fps.json ]]; then
        echo "[run_edge] WARNING: FPS stats file not found after 30s — starting collector anyway."
    fi

    mkdir -p "$(dirname "$COLLECT_OUTPUT")"
    echo "[run_edge] Collecting → $COLLECT_OUTPUT  (${COLLECT_DURATION}s, interval=${COLLECT_INTERVAL}s, wbase_ref=${COLLECT_WBASE_REF})"
    "$PYTHON" tools/profile_collect.py \
        --output    "$COLLECT_OUTPUT" \
        --duration  "$COLLECT_DURATION" \
        --interval  "$COLLECT_INTERVAL" \
        --wbase-ref "$COLLECT_WBASE_REF" &
    COLLECT_PID=$!
    _pids+=("$COLLECT_PID")
    echo "[run_edge] profile_collect PID=$COLLECT_PID"

    if [[ "$CALIBRATE" -ne 1 ]]; then
        echo "[run_edge] Press Ctrl+C to stop early. Pipeline stops automatically when collection ends."
    else
        echo "[run_edge] Pipeline stops automatically when collection ends, then fit+plot will run."
    fi
else
    echo "[run_edge] Press Ctrl+C to stop."
fi

# ---------------------------------------------------------------------------
# Watch loop — exits when collector finishes (collect/calibrate), or on error
# ---------------------------------------------------------------------------
while true; do
    sleep 2

    if ! kill -0 "$HEALTH_PID" 2>/dev/null; then
        echo "[run_edge] ERROR: health_agent exited unexpectedly. Stopping pipeline." >&2
        exit 1
    fi

    if ! kill -0 "$PIPELINE_PID" 2>/dev/null; then
        echo "[run_edge] ERROR: pipeline exited unexpectedly. Stopping health_agent." >&2
        exit 1
    fi

    if [[ -n "$COLLECT_PID" ]] && ! kill -0 "$COLLECT_PID" 2>/dev/null; then
        echo "[run_edge] Collection complete → $COLLECT_OUTPUT"
        echo "[run_edge] Stopping pipeline and health_agent..."
        # Stop pipeline and health_agent (trap will also fire, but be explicit)
        kill "$PIPELINE_PID" 2>/dev/null || true
        kill "$HEALTH_PID"   2>/dev/null || true
        wait "$PIPELINE_PID" 2>/dev/null || true
        wait "$HEALTH_PID"   2>/dev/null || true
        # Remove from _pids so _cleanup doesn't double-kill
        _pids=()
        break
    fi
done

# ===========================================================================
# STEP 5 (--calibrate only): Fit coefficients or train DL model
# ===========================================================================
if [[ "$CALIBRATE" -eq 1 ]]; then
    echo ""
    echo "[run_edge] ── STEP 5/6: Fitting model (LOAD_MODEL=$LOAD_MODEL) ──"

    if [[ "$LOAD_MODEL" == "formula" ]]; then
        # Determine target based on policy
        if [[ "$LOAD_POLICY" == "predict_no_base" ]]; then
            FIT_TARGET="delta_load"
        else
            FIT_TARGET="gpu_percent"
        fi
        echo "[run_edge] Running fit_coefficients.py (target=$FIT_TARGET, wbase=${COLLECT_WBASE_REF})"
        "$PYTHON" tools/fit_coefficients.py \
            --csv    "$COLLECT_OUTPUT" \
            --wbase  "$COLLECT_WBASE_REF" \
            --target "$FIT_TARGET" \
            --output configs/edge_node.yml
        echo "[run_edge] Coefficients written to configs/edge_node.yml"
        echo "[run_edge] ACTION REQUIRED: set 'enabled: true' in configs/edge_node.yml → proactive: section, then restart."

    else
        # DL model
        HORIZON_ROWS=$(python3 -c "import math; print(max(1, round(10 / ${COLLECT_INTERVAL})))")
        if [[ "$LOAD_POLICY" == "predict_no_base" ]]; then
            # predict_no_base trains on delta_load explicitly
            echo "[run_edge] Running train_dl_model.py (target=delta_load, window_k=5, horizon_rows=${HORIZON_ROWS})"
            mkdir -p "$(dirname "$MODEL_OUTPUT")"
            "$PYTHON" tools/train_dl_model.py \
                --csv          "$COLLECT_OUTPUT" \
                --target       delta_load \
                --window-k     5 \
                --horizon-rows "$HORIZON_ROWS" \
                --epochs       200 \
                --output       "$MODEL_OUTPUT"
        else
            # predict_with_base / actual: let train_dl_model pick its canonical
            # load_score target (load_score_smoothed → load_score_raw → load_score
            # → actual_load → gpu_percent); do NOT force --target gpu_percent.
            echo "[run_edge] Running train_dl_model.py (target=<auto>, window_k=5, horizon_rows=${HORIZON_ROWS})"
            mkdir -p "$(dirname "$MODEL_OUTPUT")"
            "$PYTHON" tools/train_dl_model.py \
                --csv          "$COLLECT_OUTPUT" \
                --window-k     5 \
                --horizon-rows "$HORIZON_ROWS" \
                --epochs       200 \
                --output       "$MODEL_OUTPUT"
        fi
        echo "[run_edge] ONNX model written to $MODEL_OUTPUT"
        echo "[run_edge] ACTION REQUIRED: set 'enabled: true' in configs/edge_node.yml → proactive: section, then restart."
    fi

    # ===========================================================================
    # STEP 6 (--calibrate only): Plot RMSE and burst charts
    # ===========================================================================
    echo ""
    echo "[run_edge] ── STEP 6/6: Generating validation plots ──"

    mkdir -p "$(dirname "$PLOT_RMSE")"
    echo "[run_edge] plot_rmse.py → $PLOT_RMSE"
    "$PYTHON" tools/plot_rmse.py \
        --csv   "$COLLECT_OUTPUT" \
        --cfg   configs/edge_node.yml \
        --wbase "$COLLECT_WBASE_REF" \
        --out   "$PLOT_RMSE" || echo "[run_edge] WARNING: plot_rmse.py failed (matplotlib missing?)"

    mkdir -p "$(dirname "$PLOT_BURST")"
    echo "[run_edge] plot_burst.py → $PLOT_BURST"
    "$PYTHON" tools/plot_burst.py \
        --csv   "$COLLECT_OUTPUT" \
        --cfg   configs/edge_node.yml \
        --wbase "$COLLECT_WBASE_REF" \
        --out   "$PLOT_BURST" || echo "[run_edge] WARNING: plot_burst.py failed (matplotlib missing?)"

    echo ""
    echo "[run_edge] ══ Calibration complete ══"
    echo "[run_edge]   W_base measurement : $WBASE_OUTPUT"
    echo "[run_edge]   Calibration CSV    : $COLLECT_OUTPUT"
    if [[ "$LOAD_MODEL" == "formula" ]]; then
        echo "[run_edge]   Fitted coefficients: configs/edge_node.yml (proactive: section)"
    else
        echo "[run_edge]   ONNX model          : $MODEL_OUTPUT"
    fi
    echo "[run_edge]   RMSE chart         : $PLOT_RMSE"
    echo "[run_edge]   Burst chart        : $PLOT_BURST"
    echo ""
    echo "[run_edge]   Next: edit configs/edge_node.yml, set proactive.enabled: true, then run:"
    echo "[run_edge]   ./run_edge.sh --load-policy ${LOAD_POLICY} --load-model ${LOAD_MODEL}"
fi
