"""
Edge/speedflow_python/broker_manager.py

BrokerManager — Lifecycle management for an embedded Mosquitto MQTT broker.

Each Edge node MAY run this if BROKER_ENABLED=true in Edge/.env.
Exactly one node in the cluster should have it enabled at any given time;
BrokerWatcher (in peer_orchestrator.py) handles automatic failover when
the current broker node goes offline.

Responsibilities:
  - Write a minimal mosquitto.conf to /tmp/
  - Spawn the mosquitto subprocess (must already be installed:
    sudo apt install mosquitto)
  - Monitor the subprocess and restart it on unexpected exit
  - Stop cleanly on request
  - Expose is_running() via a TCP probe on the configured port

No configuration is read from here — callers pass port + optional auth.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("broker_manager")


class BrokerManager:
    """
    Manages a Mosquitto subprocess on this node.

    Usage::

        bm = BrokerManager(port=1883)
        bm.start()          # spawns mosquitto; blocks until port is ready
        bm.is_running()     # True/False via TCP probe
        bm.stop()           # terminates the subprocess

    Thread-safe: start/stop may be called from any thread.
    """

    # How long to wait for mosquitto to open its port after spawn (seconds)
    _READY_TIMEOUT = 10.0
    # Interval between readiness probes
    _PROBE_INTERVAL = 0.5
    # Restart delay after unexpected subprocess death
    _RESTART_DELAY = 3.0

    def __init__(
        self,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self._port = port
        self._username = username
        self._password = password

        self._proc: Optional[subprocess.Popen] = None
        self._conf_path: Optional[Path] = None
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Write mosquitto.conf, spawn the process, wait until port is ready.

        Raises RuntimeError if mosquitto is not found on PATH or if the
        port does not open within _READY_TIMEOUT seconds.
        """
        with self._lock:
            if self._running:
                logger.warning("[BrokerManager] Already running on port %d.", self._port)
                return
            self._running = True

        self._conf_path = self._write_conf()
        self._spawn()

        # Wait for port to open
        deadline = time.monotonic() + self._READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._probe():
                logger.info(
                    "[BrokerManager] Mosquitto ready on port %d (pid=%d).",
                    self._port, self._proc.pid if self._proc else -1,
                )
                break
            time.sleep(self._PROBE_INTERVAL)
        else:
            self.stop()
            raise RuntimeError(
                f"[BrokerManager] Mosquitto did not open port {self._port} "
                f"within {self._READY_TIMEOUT}s. Is it installed?"
            )

        # Background monitor — restart on unexpected death
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="BrokerMonitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        """Terminate the mosquitto subprocess."""
        with self._lock:
            self._running = False
            proc = self._proc
            self._proc = None

        if proc and proc.poll() is None:
            logger.info("[BrokerManager] Stopping mosquitto (pid=%d).", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            logger.info("[BrokerManager] Mosquitto stopped.")

        if self._conf_path and self._conf_path.exists():
            try:
                self._conf_path.unlink()
            except OSError:
                pass

    def is_running(self) -> bool:
        """
        Return True if a Mosquitto process is listening on self._port.

        Uses a TCP probe so it works regardless of whether *this* BrokerManager
        started the process (useful during failover).
        """
        return self._probe()

    @property
    def port(self) -> int:
        return self._port

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_conf(self) -> Path:
        """Write a minimal mosquitto.conf to /tmp/ and return its path."""
        lines = [
            f"listener {self._port}",
            "allow_anonymous true",   # open LAN — add auth later if needed
            "log_type error",
            "log_type warning",
            "log_type notice",
            "persistence false",
        ]

        if self._username and self._password:
            # Write password file alongside the conf
            pw_path = Path(tempfile.gettempdir()) / "mosquitto_pw.txt"
            # mosquitto_passwd -c -b <file> <user> <pass>
            try:
                subprocess.run(
                    ["mosquitto_passwd", "-c", "-b", str(pw_path), self._username, self._password],
                    check=True, capture_output=True,
                )
                lines[1] = "allow_anonymous false"
                lines.append(f"password_file {pw_path}")
            except Exception as exc:
                logger.warning(
                    "[BrokerManager] Could not create password file: %s. "
                    "Falling back to allow_anonymous=true.", exc,
                )

        conf_path = Path(tempfile.gettempdir()) / f"mosquitto_embedded_{self._port}.conf"
        conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.debug("[BrokerManager] Config written to %s", conf_path)
        return conf_path

    def _spawn(self) -> None:
        """Fork the mosquitto subprocess."""
        cmd = ["mosquitto", "-c", str(self._conf_path)]
        logger.info("[BrokerManager] Spawning: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "mosquitto not found on PATH. "
                "Install with: sudo apt install mosquitto"
            )
        with self._lock:
            self._proc = proc

    def _monitor_loop(self) -> None:
        """Restart mosquitto if it dies unexpectedly while _running is True."""
        while True:
            with self._lock:
                if not self._running:
                    return
                proc = self._proc

            if proc is not None:
                ret = proc.poll()
                if ret is not None:
                    # Read any error output for diagnostics
                    stderr_out = ""
                    try:
                        stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
                    except Exception:
                        pass
                    logger.error(
                        "[BrokerManager] Mosquitto exited unexpectedly (rc=%d). "
                        "stderr: %s. Restarting in %ss...",
                        ret, stderr_out or "<none>", self._RESTART_DELAY,
                    )
                    time.sleep(self._RESTART_DELAY)
                    with self._lock:
                        if not self._running:
                            return
                    self._spawn()
                    # Wait for port again
                    deadline = time.monotonic() + self._READY_TIMEOUT
                    while time.monotonic() < deadline:
                        if self._probe():
                            logger.info(
                                "[BrokerManager] Mosquitto restarted on port %d.", self._port
                            )
                            break
                        time.sleep(self._PROBE_INTERVAL)

            time.sleep(2.0)

    def _probe(self) -> bool:
        """Return True if port is open (TCP connect succeeds)."""
        try:
            with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                return True
        except OSError:
            return False
