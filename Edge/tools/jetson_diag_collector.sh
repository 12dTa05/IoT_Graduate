#!/usr/bin/env bash
# ==============================================================================
# Edge/tools/jetson_diag_collector.sh
#
# Passive / safe diagnostic collector for Jetson nodes to capture health metrics,
# system state, journal/syslog excerpts, thermal status, and lockup investigation artifacts.
#
# Safety characteristics:
# - Passive & read-only: does not modify runtime configurations or enable kernel watchdogs.
# - Safe defaults: does not reboot or stop running services.
# - Output: timestamped archive in Edge/logs/diagnostics/ (or specified directory).
# ==============================================================================

set -euo pipefail

# ponytail: minimal script doing exact diagnostic collection with zero external dependencies

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDGE_DIR="$(dirname "$SCRIPT_DIR")"
DEFAULT_OUT_DIR="$EDGE_DIR/logs/diagnostics"
OUT_DIR="${1:-$DEFAULT_OUT_DIR}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
TARGET_DIR="${OUT_DIR}/jetson_diag_${TIMESTAMP}"

mkdir -p "$TARGET_DIR"
echo "[jetson_diag] Collecting system diagnostics to: $TARGET_DIR"

# 1. System & OS Info
echo "[jetson_diag] Capturing system and OS metadata..."
uname -a > "$TARGET_DIR/uname.txt" 2>&1 || true
uptime > "$TARGET_DIR/uptime.txt" 2>&1 || true
free -h > "$TARGET_DIR/free.txt" 2>&1 || true
df -h > "$TARGET_DIR/df.txt" 2>&1 || true
lsblk > "$TARGET_DIR/lsblk.txt" 2>&1 || true

# 2. Jetson / Hardware / Thermal status
echo "[jetson_diag] Capturing thermal and Jetson hardware state..."
if command -v tegrastats >/dev/null 2>&1; then
    timeout 3 tegrastats --interval 500 > "$TARGET_DIR/tegrastats.txt" 2>&1 || true
fi

if [ -d "/sys/devices/virtual/thermal" ]; then
    for tz in /sys/devices/virtual/thermal/thermal_zone*; do
        if [ -d "$tz" ]; then
            tz_name=$(basename "$tz")
            type=$(cat "$tz/type" 2>/dev/null || echo "unknown")
            temp=$(cat "$tz/temp" 2>/dev/null || echo "unknown")
            echo "$tz_name ($type): $temp mC" >> "$TARGET_DIR/thermal_zones.txt"
        fi
    done
fi

if [ -f "/sys/kernel/debug/nvmap/iovmm/allocations" ]; then
    cat /sys/kernel/debug/nvmap/iovmm/allocations > "$TARGET_DIR/nvmap_allocations.txt" 2>&1 || true
fi

# 3. Process & Memory snapshots
echo "[jetson_diag] Capturing process and memory metrics..."
ps aux --sort=-%cpu > "$TARGET_DIR/ps_cpu.txt" 2>&1 || true
ps aux --sort=-%mem > "$TARGET_DIR/ps_mem.txt" 2>&1 || true
cat /proc/meminfo > "$TARGET_DIR/meminfo.txt" 2>&1 || true
cat /proc/vmstat > "$TARGET_DIR/vmstat.txt" 2>&1 || true
cat /proc/loadavg > "$TARGET_DIR/loadavg.txt" 2>&1 || true

# 4. Kernel / Syslog / Journal excerpts
echo "[jetson_diag] Extracting kernel and system logs..."
dmesg -T > "$TARGET_DIR/dmesg.txt" 2>&1 || true

# Check pstore if available (ramoops / panic log from previous crash)
if [ -d "/sys/fs/pstore" ] && [ "$(ls -A /sys/fs/pstore 2>/dev/null)" ]; then
    mkdir -p "$TARGET_DIR/pstore"
    cp -r /sys/fs/pstore/* "$TARGET_DIR/pstore/" 2>/dev/null || true
fi

# Extract last 1000 lines from journald or syslog
if command -v journalctl >/dev/null 2>&1; then
    journalctl -n 1000 --no-pager > "$TARGET_DIR/journalctl_tail.txt" 2>&1 || true
    # Capture previous boot journal if persistent logging was enabled
    journalctl -b -1 -n 1000 --no-pager > "$TARGET_DIR/journalctl_prev_boot.txt" 2>&1 || true
fi

if [ -f "/var/log/syslog" ]; then
    tail -n 1000 /var/log/syslog > "$TARGET_DIR/syslog_tail.txt" 2>&1 || true
fi

if [ -f "/var/log/kern.log" ]; then
    tail -n 1000 /var/log/kern.log > "$TARGET_DIR/kern_tail.txt" 2>&1 || true
fi

# 5. SpeedFlow Application State & Telemetry
echo "[jetson_diag] Capturing SpeedFlow edge state..."
if [ -f "/dev/shm/speedflow_fps.json" ]; then
    cp /dev/shm/speedflow_fps.json "$TARGET_DIR/shm_speedflow_fps.json" 2>/dev/null || true
fi

if [ -d "$EDGE_DIR/logs" ]; then
    ls -lha "$EDGE_DIR/logs" > "$TARGET_DIR/edge_logs_list.txt" 2>&1 || true
    # Copy newest run log if present
    LATEST_LOG="$(ls -t "$EDGE_DIR/logs"/run_*.log 2>/dev/null | head -n 1 || true)"
    if [ -n "$LATEST_LOG" ] && [ -f "$LATEST_LOG" ]; then
        tail -n 1000 "$LATEST_LOG" > "$TARGET_DIR/latest_run_log_tail.txt" 2>&1 || true
    fi
fi

# 6. Sysctl / SysRq / Crashdump configuration inspection
echo "[jetson_diag] Checking kernel crash investigation parameters..."
cat /proc/sys/kernel/sysrq > "$TARGET_DIR/sysrq_status.txt" 2>&1 || true
cat /proc/sys/kernel/panic > "$TARGET_DIR/panic_timeout.txt" 2>&1 || true
cat /proc/sys/kernel/hung_task_timeout_secs > "$TARGET_DIR/hung_task_timeout.txt" 2>&1 || true

# Compress report
ARCHIVE_PATH="${TARGET_DIR}.tar.gz"
tar -czf "$ARCHIVE_PATH" -C "$OUT_DIR" "$(basename "$TARGET_DIR")"
rm -rf "$TARGET_DIR"

echo "[jetson_diag] Diagnostic report created successfully:"
echo "  -> $ARCHIVE_PATH"
