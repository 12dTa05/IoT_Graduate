#!/usr/bin/env python3
"""
Edge/health_agent.py

Health Agent — Collect hardware metrics and publish to MQTT broker.

Reads all configuration from Edge/.env via speedflow_python.settings.
No default values in this file — all values must be set in .env.

Broker penalty:
  When BROKER_ENABLED=true, this node is running the embedded Mosquitto broker.
  BROKER_PENALTY_SCORE is added to the computed load_score so that all peers
  see an elevated score and deprioritize this node in camera auctions.
  The payload also includes is_broker=true and broker_host so that BrokerWatcher
  on other nodes can resolve this node's IP during failover.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import time
import threading
from pathlib import Path
from typing import Dict, Optional

# Load settings from .env (must run from Edge/ or have Edge/ in path)
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from speedflow_python.settings import (
    NODE_ID,
    MQTT_BROKER_HOST   as BROKER_HOST,
    MQTT_BROKER_PORT   as BROKER_PORT,
    MQTT_USER,
    MQTT_PASS,
    HEALTH_INTERVAL,
    TARGET_FPS,
    FPS_STATS_FILE,
    SIGNALING_PORT,
    BROKER_ENABLED,
    BROKER_PENALTY_SCORE,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("health_agent")

STATUS_TOPIC = f"peers/status/{NODE_ID}"

# Resolve this node's LAN IP once at startup (used in broker_host field)
def _local_ip() -> str:
    try:
        with socket.create_connection(("8.8.8.8", 80), timeout=1) as s:
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"

_MY_IP = _local_ip()


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


def _compute_load_score(
    metrics: Dict,
    fps_stats: Dict,
    is_broker: bool = False,
) -> float:
    """
    Tính Load Score tổng hợp từ thông số phần cứng và FPS pipeline.

    Công thức:
        base    = 0.5 * GPU_% + 0.3 * CPU_% + 0.2 * RAM_%
        penalty = max(0, (TARGET_FPS - avg_fps) / TARGET_FPS * 30)
        broker  = BROKER_PENALTY_SCORE  (added when this node runs Mosquitto)
        score   = min(100, base + penalty + broker)

    Broker penalty ensures the broker node is deprioritized in camera auctions.
    All peers see the elevated load_score and skip this node when bidding.
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
    fps_penalty = (fps_drop / TARGET_FPS) * 30.0  # tối đa +30 điểm

    broker_penalty = BROKER_PENALTY_SCORE if is_broker else 0.0

    score = min(100.0, base + fps_penalty + broker_penalty)
    return round(score, 1)


# ---------------------------------------------------------------------------
# Health Agent Main Loop
# ---------------------------------------------------------------------------

class HealthAgent:
    """
    Thu thập metrics và publish định kỳ lên MQTT.
    Chạy trong thread daemon riêng biệt.

    Args:
        broker_manager: Optional BrokerManager instance. When provided,
            is_running() is polled each cycle to set the is_broker flag
            in the payload (the node may dynamically become the broker
            after a failover even if it did not start as one).
    """

    def __init__(self, broker_manager=None) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._broker_host = BROKER_HOST
        self._broker_port = BROKER_PORT
        self._broker_manager = broker_manager
        self._lock = threading.Lock()

    def start(self) -> None:
        """Khởi động agent trong thread daemon."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="HealthAgent",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[HealthAgent] Started. Node=%s, Interval=%.1fs, Topic=%s",
            NODE_ID, HEALTH_INTERVAL, STATUS_TOPIC,
        )

    def stop(self) -> None:
        self._running = False
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass

    def reconnect(self, new_host: str, new_port: int) -> None:
        """
        Called by BrokerWatcher when the MQTT broker moves to a new host.
        Disconnects the current client; _run() will reconnect automatically.
        """
        logger.info(
            "[HealthAgent] Broker changed → %s:%d. Reconnecting...", new_host, new_port
        )
        with self._lock:
            self._broker_host = new_host
            self._broker_port = new_port
            client = self._client
            self._client = None
        if client:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    def _connect_mqtt(self):
        """Khởi tạo và kết nối MQTT client."""
        import paho.mqtt.client as mqtt

        with self._lock:
            host = self._broker_host
            port = self._broker_port

        client = mqtt.Client(client_id=f"health_{NODE_ID}")
        if MQTT_USER:
            client.username_pw_set(MQTT_USER, MQTT_PASS)

        client.on_connect = lambda c, u, f, rc: logger.info(
            "[HealthAgent] MQTT connected (rc=%d)", rc
        ) if rc == 0 else logger.error("[HealthAgent] MQTT connect failed (rc=%d)", rc)
        client.on_disconnect = lambda c, u, rc: logger.warning(
            "[HealthAgent] MQTT disconnected (rc=%d)", rc
        ) if rc != 0 else None

        try:
            client.connect(host, port, keepalive=60)
            client.loop_start()
        except Exception as exc:
            logger.error("[HealthAgent] Cannot connect to broker: %s", exc)
            return None

        return client

    def _run(self) -> None:
        """Vòng lặp chính — đo và publish định kỳ."""
        self._client = self._connect_mqtt()
        if not self._client:
            logger.error("[HealthAgent] MQTT unavailable. Running in log-only mode.")

        while self._running:
            try:
                # Determine whether THIS node is currently running the broker.
                # BROKER_ENABLED is static config; broker_manager may have been
                # started dynamically after a failover.
                bm = self._broker_manager
                is_broker = (
                    BROKER_ENABLED
                    or (bm is not None and bm.is_running())
                )

                metrics    = _collect_jetson_metrics()
                fps_stats  = _read_fps_stats()
                load_score = _compute_load_score(metrics, fps_stats, is_broker=is_broker)

                payload = {
                    "node_id":        NODE_ID,
                    "timestamp":      time.time(),
                    "load_score":     load_score,
                    "gpu_percent":    metrics["gpu_percent"],
                    "cpu_percent":    metrics["cpu_percent"],
                    "ram_percent":    metrics["ram_percent"],
                    "gpu_temp_c":     metrics["gpu_temp_c"],
                    "power_mw":       metrics["power_mw"],
                    "signaling_port": SIGNALING_PORT,
                    # Broker identity — read by BrokerWatcher on other nodes
                    "is_broker":      is_broker,
                    "broker_host":    _MY_IP if is_broker else "",
                    "pipeline": {
                        "fps_per_camera": fps_stats,
                        "avg_fps": round(
                            sum(fps_stats.values()) / len(fps_stats), 1
                        ) if fps_stats else None,
                        "active_cameras": list(fps_stats.keys()),
                    },
                }

                broker_tag = " [BROKER]" if is_broker else ""
                logger.info(
                    "LoadScore=%.1f%s | GPU=%.1f%% CPU=%.1f%% RAM=%.1f%% "
                    "Temp=%.1f°C Power=%.0fmW | FPS=%s",
                    load_score,
                    broker_tag,
                    metrics["gpu_percent"],
                    metrics["cpu_percent"],
                    metrics["ram_percent"],
                    metrics["gpu_temp_c"],
                    metrics["power_mw"],
                    fps_stats,
                )

                # Reconnect if client was cleared by reconnect()
                with self._lock:
                    client = self._client
                if client is None:
                    self._client = self._connect_mqtt()

                if self._client:
                    self._client.publish(STATUS_TOPIC, json.dumps(payload), qos=0)

            except Exception as exc:
                logger.error("[HealthAgent] Error in collect loop: %s", exc)

            time.sleep(HEALTH_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point (chạy standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        import paho.mqtt.client
    except ImportError:
        logger.error("paho-mqtt not installed. Run: pip install paho-mqtt")
        sys.exit(1)

    # When run standalone, honour BROKER_ENABLED by starting BrokerManager
    broker_mgr = None
    if BROKER_ENABLED:
        from speedflow_python.broker_manager import BrokerManager
        broker_mgr = BrokerManager(port=BROKER_PORT)
        try:
            broker_mgr.start()
            logger.info("[main] Embedded broker started on port %d.", BROKER_PORT)
        except RuntimeError as exc:
            logger.error("[main] %s", exc)
            sys.exit(1)

    agent = HealthAgent(broker_manager=broker_mgr)
    agent.start()

    try:
        # Chạy mãi mãi cho đến khi có Ctrl+C
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping HealthAgent...")
        agent.stop()
        if broker_mgr:
            broker_mgr.stop()

