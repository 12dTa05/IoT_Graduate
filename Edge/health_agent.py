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
    SIGNALING_PORT,
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
    """
    try:
        from jtop import jtop
        with jtop() as jetson:
            stats = jetson.stats
            # jetson.stats là dict chứa tất cả chỉ số Jetson
            gpu_pct  = float(stats.get("GPU", 0))
            cpu_pct  = float(stats.get("CPU", 0))         # average across all cores
            ram_pct  = float(jetson.memory["RAM"]["used"] /
                             jetson.memory["RAM"]["tot"] * 100)
            temp_c   = float(stats.get("Temp GPU", stats.get("Temp AO", 0)))
            power_mw = float(stats.get("Power TOT", stats.get("Power SYS", 0)))

            return {
                "gpu_percent": round(gpu_pct, 1),
                "cpu_percent": round(cpu_pct, 1),
                "ram_percent": round(ram_pct, 1),
                "gpu_temp_c":  round(temp_c, 1),
                "power_mw":    round(power_mw, 0),
                "source": "jtop",
            }
    except ImportError:
        logger.debug("jetson-stats not installed, falling back to psutil.")
    except Exception as exc:
        logger.debug("jtop error: %s — falling back to psutil.", exc)

    # --- Fallback: psutil (cho môi trường dev không phải Jetson) ---
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=0.5)
        ram     = psutil.virtual_memory()
        return {
            "gpu_percent": 0.0,   # psutil không đo được GPU
            "cpu_percent": round(cpu_pct, 1),
            "ram_percent": round(ram.percent, 1),
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
    if fps_stats:
        avg_fps = sum(fps_stats.values()) / len(fps_stats)
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
    Runs in a daemon thread.
    """

    def __init__(self) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._pub = None

    def start(self) -> None:
        """Start agent in daemon thread."""
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
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass

    def _connect_zenoh(self):
        """Open Zenoh session and declare publisher."""
        import zenoh
        try:
            session = make_session()
            pub = session.declare_publisher(f"peers/status/{NODE_ID}")
            logger.info("[HealthAgent] Zenoh session opened (peer mode).")
            return session, pub
        except Exception as exc:
            logger.error("[HealthAgent] Cannot open Zenoh session: %s", exc)
            return None, None

    def _run(self) -> None:
        """Main loop — collect and publish periodically."""
        self._session, self._pub = self._connect_zenoh()
        if not self._session:
            logger.error("[HealthAgent] Zenoh unavailable. Running in log-only mode.")

        while self._running:
            try:
                metrics   = _collect_jetson_metrics()
                fps_stats = _read_fps_stats()
                load_score = _compute_load_score(metrics, fps_stats)

                payload = {
                    "node_id":       NODE_ID,
                    "timestamp":     time.time(),
                    "load_score":    load_score,
                    "gpu_percent":   metrics["gpu_percent"],
                    "cpu_percent":   metrics["cpu_percent"],
                    "ram_percent":   metrics["ram_percent"],
                    "gpu_temp_c":    metrics["gpu_temp_c"],
                    "power_mw":      metrics["power_mw"],
                    "signaling_port": SIGNALING_PORT,
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

                # Push to Central Monitor Server (nếu có kết nối)
                try:
                    from speedflow_python.signaling import send_to_servers
                    send_to_servers(payload)
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
