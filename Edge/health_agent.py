#!/usr/bin/env python3
"""
Edge/health_agent.py

Health Agent — Collect hardware metrics and publish via Zenoh (peer mode).

Reads all configuration from Edge/.env via speedflow_python.settings.
No default values in this file — all values must be set in .env.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import threading
from pathlib import Path
from typing import Dict, Optional

import msgpack

from speedflow_python.zenoh_session import make_session

# Load settings from .env (must run from Edge/ or have Edge/ in path)
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from speedflow_python.settings import (
    NODE_ID,
    HEALTH_INTERVAL,
    TARGET_FPS,
    FPS_STATS_FILE,
    MONITOR_URL,
    ADVERTISE_IP,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("health_agent")


# ---------------------------------------------------------------------------
# FPS Reader (đọc từ file JSON được SpeedProbe ghi ra)
# ---------------------------------------------------------------------------

def _read_fps_stats() -> Dict[str, float]:
    """
    Đọc file JSON do SpeedProbe ghi chứa FPS theo từng camera.
    Trả về dict {camera_id: fps} hoặc dict rỗng nếu chưa có file.

    Định dạng file:
        {
            "cam_01": 24.7,
            "cam_02": 25.1,
            "_updated_at": 1714739900.12
        }
    """
    try:
        with open(FPS_STATS_FILE, "r") as f:
            data = json.load(f)
        # Loại bỏ meta-keys không phải camera
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug("Failed to read FPS stats: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Metric Collector
# ---------------------------------------------------------------------------

def _collect_jetson_metrics() -> Dict:
    """
    Thu thập thông số phần cứng từ thiết bị Jetson bằng thư viện jtop.
    Trả về dict với các key chuẩn hoá.

    Fallback sang psutil nếu không chạy trên Jetson thật (phục vụ dev/test).

    NOTE: Do NOT open a new jtop() context here — use the persistent
    session held by HealthAgent._jtop to avoid socket/fd exhaustion.
    This function is only used as a one-shot fallback during init.
    """
    # --- Fallback: psutil (non-blocking, no interval sleep) ---
    try:
        import psutil
        # interval=None: non-blocking, returns value since last call.
        # Call virtual_memory() inside a local scope — no fd leak.
        cpu_pct = psutil.cpu_percent(interval=None)
        ram_pct = psutil.virtual_memory().percent
        return {
            "gpu_percent": 0.0,   # psutil không đo được GPU
            "cpu_percent": round(cpu_pct, 1),
            "ram_percent": round(ram_pct, 1),
            "gpu_temp_c":  0.0,
            "power_mw":    0.0,
            "source": "psutil",
        }
    except Exception as exc:
        logger.error("psutil error: %s", exc)
        return {
            "gpu_percent": 0.0,
            "cpu_percent": 0.0,
            "ram_percent": 0.0,
            "gpu_temp_c":  0.0,
            "power_mw":    0.0,
            "source": "error",
        }


def _compute_load_score(metrics: Dict, fps_stats: Dict) -> float:
    """
    Tính Load Score tổng hợp từ thông số phần cứng và FPS pipeline.

    Công thức:
        base    = 0.5 * GPU_% + 0.3 * CPU_% + 0.2 * RAM_%
        penalty = max(0, (TARGET_FPS - avg_fps) / TARGET_FPS * 30)
        score   = min(100, base + penalty)

    Ý nghĩa Penalty:
        - Nếu FPS = 25 (target) → penalty = 0
        - Nếu FPS = 12 (drop 50%) → penalty = +15 điểm
        - Nếu FPS = 0 (pipeline ngừng) → penalty = +30 điểm (tối đa)
    """
    base = (
        0.5 * metrics["gpu_percent"] +
        0.3 * metrics["cpu_percent"] +
        0.2 * metrics["ram_percent"]
    )

    # Tính avg_fps từ tất cả camera đang chạy
    # BUG-10: exclude cameras reporting 0.0 fps (stalled but not yet removed)
    # so a single hung camera doesn't permanently max out the penalty term.
    active_fps = [v for v in fps_stats.values() if v > 0.0]
    if active_fps:
        avg_fps = sum(active_fps) / len(active_fps)
    elif fps_stats:
        # All cameras are at 0 fps — pipeline is truly stalled, apply full penalty
        avg_fps = 0.0
    else:
        avg_fps = TARGET_FPS  # Không có dữ liệu → không phạt

    fps_drop = max(0.0, TARGET_FPS - avg_fps)
    penalty = (fps_drop / TARGET_FPS) * 30.0  # tối đa +30 điểm

    score = min(100.0, base + penalty)
    return round(score, 1)


# ---------------------------------------------------------------------------
# Health Agent Main Loop
# ---------------------------------------------------------------------------

class HealthAgent:
    """
    Collect metrics and publish periodically via Zenoh (peer mode).
    Runs a daemon thread with a persistent jtop session to avoid
    socket/fd exhaustion from opening a new jtop() context each cycle.

    When run as a standalone process, opens its own MonitorClient
    WebSocket to push health payloads to the Central Monitor Server.
    """

    def __init__(self) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._pub = None
        self._jtop = None          # persistent jtop session
        self._monitor_client = None  # own WS client when run standalone

    def start(self) -> None:
        """Start agent in daemon thread."""
        # Open own MonitorClient if running standalone
        if MONITOR_URL:
            try:
                from speedflow_python.monitor_client import MonitorClient, set_default_client
                self._monitor_client = MonitorClient(MONITOR_URL, NODE_ID, ADVERTISE_IP)
                self._monitor_client.start()
                set_default_client(self._monitor_client)
                logger.info("[HealthAgent] MonitorClient started → %s", MONITOR_URL)
            except Exception as exc:
                logger.warning("[HealthAgent] MonitorClient failed to start: %s", exc)

        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="HealthAgent",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[HealthAgent] Started. Node=%s, Interval=%.1fs",
            NODE_ID, HEALTH_INTERVAL,
        )

    def stop(self) -> None:
        self._running = False
        if self._jtop is not None:
            try:
                self._jtop.close()
            except Exception:
                pass
            self._jtop = None
        if self._monitor_client is not None:
            try:
                self._monitor_client.stop()
            except Exception:
                pass
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass

    def _connect_zenoh(self):
        """Open Zenoh session, declare publisher, subscribe to traffic events."""
        import zenoh
        try:
            session = make_session()
            pub = session.declare_publisher(f"peers/status/{NODE_ID}")

            # Subscribe to overspeed events from the pipeline process and
            # forward them to the Central Monitor Server via MonitorClient.
            # This avoids having two MonitorClient WS connections for the
            # same node_id (which causes a reconnect loop on the Server).
            session.declare_subscriber(
                f"traffic/events/{NODE_ID}/**",
                self._on_traffic_event,
            )

            logger.info("[HealthAgent] Zenoh session opened (peer mode).")
            return session, pub
        except Exception as exc:
            logger.error("[HealthAgent] Cannot open Zenoh session: %s", exc)
            return None, None

    def _on_traffic_event(self, sample) -> None:
        """Forward overspeed events from pipeline → Central Monitor."""
        try:
            payload = msgpack.unpackb(sample.payload.to_bytes(), raw=False)
            if payload.get("type") == "overspeed":
                from speedflow_python.monitor_client import send_to_monitor
                send_to_monitor(payload)
        except Exception as exc:
            logger.debug("[HealthAgent] Traffic event forward error: %s", exc)

    def _open_jtop(self):
        """Try to open a persistent jtop session; return it or None."""
        try:
            from jtop import jtop as JTop
            j = JTop()
            j.start()
            logger.info("[HealthAgent] jtop session opened (persistent).")
            return j
        except Exception as exc:
            logger.debug("[HealthAgent] jtop unavailable: %s — using psutil.", exc)
            return None

    def _collect_metrics(self) -> Dict:
        """Read metrics from persistent jtop session or fall back to psutil."""
        if self._jtop is not None:
            try:
                stats    = self._jtop.stats
                mem      = self._jtop.memory
                temp     = self._jtop.temperature
                power    = self._jtop.power

                # BUG-17: jtop.stats returns None until the first collection
                # interval completes. Guard explicitly before calling .get().
                if stats is None or mem is None:
                    raise ValueError("jtop not ready yet")

                gpu_pct  = float(stats.get("GPU", 0))

                # CPU: stats has CPU1..CPU12, not a single "CPU" key.
                # Compute average from j.cpu['total']['idle'].
                cpu_total = self._jtop.cpu.get("total", {})
                cpu_idle  = cpu_total.get("idle", 100.0)
                cpu_pct   = 100.0 - cpu_idle

                ram_pct  = float(mem["RAM"]["used"] / mem["RAM"]["tot"] * 100)

                # Temperature: use 'gpu' sensor if online, else 'tj' (junction)
                gpu_temp_info = temp.get("gpu", {})
                if gpu_temp_info.get("online", False) and gpu_temp_info.get("temp", -256) > -100:
                    temp_c = float(gpu_temp_info["temp"])
                else:
                    tj_info = temp.get("tj", {})
                    temp_c = float(tj_info.get("temp", 0))

                # Power: total power in mW
                power_mw = float(power.get("tot", {}).get("power", 0))

                return {
                    "gpu_percent": round(gpu_pct, 1),
                    "cpu_percent": round(cpu_pct, 1),
                    "ram_percent": round(ram_pct, 1),
                    "gpu_temp_c":  round(temp_c, 1),
                    "power_mw":    round(power_mw, 0),
                    "source": "jtop",
                }
            except Exception as exc:
                logger.debug("[HealthAgent] jtop read error: %s — falling back.", exc)

        return _collect_jetson_metrics()

    def _run(self) -> None:
        """Main loop — collect and publish periodically."""
        self._session, self._pub = self._connect_zenoh()
        if not self._session:
            logger.error("[HealthAgent] Zenoh unavailable. Running in log-only mode.")

        self._jtop = self._open_jtop()

        # Pre-warm psutil cpu_percent (first call always returns 0.0)
        try:
            import psutil
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

        while self._running:
            try:
                metrics    = self._collect_metrics()
                fps_stats  = _read_fps_stats()
                load_score = _compute_load_score(metrics, fps_stats)

                payload = {
                    "type":          "health",
                    "node_id":       NODE_ID,
                    "timestamp":     time.time(),
                    "load_score":    load_score,
                    "gpu_percent":   metrics["gpu_percent"],
                    "cpu_percent":   metrics["cpu_percent"],
                    "ram_percent":   metrics["ram_percent"],
                    "gpu_temp_c":    metrics["gpu_temp_c"],
                    "power_mw":      metrics["power_mw"],
                    "pipeline": {
                        "fps_per_camera": fps_stats,
                        "avg_fps": round(
                            sum(fps_stats.values()) / len(fps_stats), 1
                        ) if fps_stats else None,
                        "active_cameras": list(fps_stats.keys()),
                    },
                }

                logger.info(
                    "LoadScore=%.1f | GPU=%.1f%% CPU=%.1f%% RAM=%.1f%% "
                    "Temp=%.1f°C Power=%.0fmW | FPS=%s",
                    load_score,
                    metrics["gpu_percent"],
                    metrics["cpu_percent"],
                    metrics["ram_percent"],
                    metrics["gpu_temp_c"],
                    metrics["power_mw"],
                    fps_stats,
                )

                if self._pub:
                    self._pub.put(msgpack.packb(payload, use_bin_type=True))

                # Push to Central Monitor Server (qua MonitorClient)
                try:
                    from speedflow_python.monitor_client import send_to_monitor
                    send_to_monitor(payload)
                except ImportError:
                    pass

            except Exception as exc:
                logger.error("[HealthAgent] Error in collect loop: %s", exc)

            time.sleep(HEALTH_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point (chạy standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        import zenoh
    except ImportError:
        logger.error("zenoh not installed. Run: pip install zenoh")
        sys.exit(1)

    agent = HealthAgent()
    agent.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping HealthAgent...")
        agent.stop()
