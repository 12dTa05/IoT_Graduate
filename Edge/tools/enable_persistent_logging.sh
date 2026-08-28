#!/usr/bin/env bash
# enable_persistent_logging.sh — Configure persistent systemd journal and check crash diagnostic facilities on Jetson.
#
# Usage (run on Jetson device with sudo):
#   sudo bash Edge/tools/enable_persistent_logging.sh
#
# Actions:
#   1. Creates /var/log/journal and configures Storage=persistent in /etc/systemd/journald.conf
#   2. Restarts systemd-journald to persist kernel logs/dmesg across reboots
#   3. Verifies Tegra186 hardware watchdog (/dev/watchdog) and pstore/ramoops availability

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)." >&2
    exit 1
fi

echo "=== [1/4] Configuring persistent systemd-journald ==="
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true

JOURNALD_CONF="/etc/systemd/journald.conf"
if [[ -f "$JOURNALD_CONF" ]]; then
    if grep -qE "^#?Storage=" "$JOURNALD_CONF"; then
        sed -i 's/^#\?Storage=.*/Storage=persistent/' "$JOURNALD_CONF"
    else
        echo "Storage=persistent" >> "$JOURNALD_CONF"
    fi
fi

systemctl restart systemd-journald
echo "systemd-journald restarted with Storage=persistent."

echo "=== [2/4] Verifying journal persistence ==="
if [[ -d /var/log/journal ]]; then
    echo "OK: /var/log/journal exists ($(ls -A /var/log/journal | wc -l) machine directories)."
else
    echo "WARNING: /var/log/journal was not created." >&2
fi

echo "=== [3/4] Checking hardware watchdog ==="
if [[ -e /dev/watchdog ]]; then
    echo "OK: /dev/watchdog is present."
else
    echo "WARNING: /dev/watchdog not found." >&2
fi

echo "=== [4/4] Checking pstore / ramoops crashdump backend ==="
if [[ -d /sys/fs/pstore ]]; then
    echo "OK: /sys/fs/pstore mounted ($(ls -A /sys/fs/pstore | wc -l) crash records)."
else
    echo "INFO: /sys/fs/pstore directory not mounted."
fi

echo "=== Setup complete ==="
