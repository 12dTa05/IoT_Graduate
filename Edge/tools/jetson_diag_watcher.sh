#!/usr/bin/env bash
# ==============================================================================
# Edge/tools/jetson_diag_watcher.sh
#
# Passive background health ring-buffer monitor.
# Records periodic lightweight system stats (load, free mem, tegrastats/thermals)
# and performs passive rate-limited hardware error detection (mmc/CQHCI timeout,
# cache flush -110, RCU stalls) to local diagnostics log without triggering
# watchdogs or modifying sysctl/runtime behavior.
#
# Usage:
#   ./jetson_diag_watcher.sh [interval_seconds] [history_lines]
#   Example: ./jetson_diag_watcher.sh 5 720 &
# ==============================================================================

set -euo pipefail

# ponytail: minimal circular background sampler without daemon framework overhead

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDGE_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$EDGE_DIR/logs/diagnostics"
mkdir -p "$LOG_DIR"

INTERVAL="${1:-5}"
MAX_LINES="${2:-1440}"  # ~2 hours at 5s intervals
BUFFER_FILE="$LOG_DIR/system_health_ring.csv"
TMP_FILE="$LOG_DIR/.system_health_ring.tmp"
ALERT_FILE="$LOG_DIR/hardware_alerts.log"

ALERT_COOLDOWN=60
LAST_ALERT_TIME=0
LAST_ERROR_SIG=""
HW_PATTERN='cqhci:.*timeout|cqhci.*timed out|mmc[0-9]*:.*timeout|mmc.*timed out|cache flush.*-110|-110.*cache flush|rcu(_preempt|_sched)?:.*stall|rcu.*stall|detected stalls on CPUs/tasks'

echo "[diag_watcher] Starting passive diagnostic sampler (interval=${INTERVAL}s, max_lines=${MAX_LINES})..."
echo "[diag_watcher] Output file: $BUFFER_FILE"
echo "[diag_watcher] Hardware alerts file: $ALERT_FILE"

# Write header if file does not exist
if [ ! -f "$BUFFER_FILE" ]; then
    echo "timestamp,load_1m,load_5m,load_15m,mem_avail_mb,swap_free_mb,temp_thermal0_mc" > "$BUFFER_FILE"
fi

_cleanup() {
    echo "[diag_watcher] Stopping passive sampler."
    exit 0
}
trap _cleanup SIGINT SIGTERM

while true; do
    TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    
    # Read load averages
    read -r L1 L5 L15 _ < /proc/loadavg 2>/dev/null || { L1="0"; L5="0"; L15="0"; }
    
    # Read memory stats (MB)
    MEM_AVAIL=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo "0")
    SWAP_FREE=$(awk '/SwapFree/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo "0")
    
    # Read primary thermal sensor if present
    TEMP0="0"
    if [ -f "/sys/devices/virtual/thermal/thermal_zone0/temp" ]; then
        TEMP0=$(cat "/sys/devices/virtual/thermal/thermal_zone0/temp" 2>/dev/null || echo "0")
    fi
    
    # Append metric row
    echo "$TS,$L1,$L5,$L15,$MEM_AVAIL,$SWAP_FREE,$TEMP0" >> "$BUFFER_FILE"
    
    # Simple line-count rotation to maintain fixed size ring buffer
    LINE_COUNT=$(wc -l < "$BUFFER_FILE" 2>/dev/null || echo 0)
    if [ "$LINE_COUNT" -gt "$MAX_LINES" ]; then
        # Retain header + last MAX_LINES
        { head -n 1 "$BUFFER_FILE"; tail -n "$MAX_LINES" "$BUFFER_FILE"; } > "$TMP_FILE"
        mv "$TMP_FILE" "$BUFFER_FILE"
    fi

    # Passive hardware error detection (rate-limited, non-failing)
    NOW_SEC=$(date +%s 2>/dev/null || echo "0")
    if [ $((NOW_SEC - LAST_ALERT_TIME)) -ge "$ALERT_COOLDOWN" ]; then
        MATCHES=$( ( (dmesg 2>/dev/null || true) | grep -iE "$HW_PATTERN" | tail -n 5 ) 2>/dev/null || true )
        if [ -n "$MATCHES" ] && [ "$MATCHES" != "$LAST_ERROR_SIG" ]; then
            echo "[${TS}] [HARDWARE_ALERT] Detected hardware/kernel anomaly:" >> "$ALERT_FILE"
            echo "$MATCHES" >> "$ALERT_FILE"
            echo "---" >> "$ALERT_FILE"
            LAST_ALERT_TIME="$NOW_SEC"
            LAST_ERROR_SIG="$MATCHES"
        fi
    fi
    
    sleep "$INTERVAL"
done
