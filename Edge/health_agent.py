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


def _load_edge_node_cfg() -> dict:
    """
    Read edge_node.yml once.  Returns the full parsed dict, or {} on error.
    The health agent is also started standalone (no GStreamer), so we cannot
    assume the speedflow_python settings module has loaded edge_node.yml.
    """
    try:
        import yaml
        yml_path = Path(__file__).resolve().parent / "configs" / "edge_node.yml"
        with open(yml_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.debug("[HealthAgent] edge_node.yml load error: %s", exc)
        return {}


# Load once at module import — used by _compute_load_score
_EDGE_CFG: dict = _load_edge_node_cfg()


def _select_omega(metrics: dict, n_active_cameras: int) -> tuple:
    """
    Select adaptive ω weight triple (w_gpu, w_cpu, w_ram) based on context.

    Presets (from edge_node.yml load_score section):
      thermal   — when gpu_temp_c ≥ thermal_threshold_c
      bandwidth — when active camera count ≥ stream_bandwidth_threshold
      normal    — default

    Returns (w_gpu, w_cpu, w_ram, preset_name) so the preset can be logged
    and included in the heartbeat payload.
    """
    ls_cfg = _EDGE_CFG.get("load_score", {})

    thermal_thresh = float(ls_cfg.get("thermal_threshold_c", 75.0))
    bw_thresh      = int(ls_cfg.get("stream_bandwidth_threshold", 3))

    w_normal    = ls_cfg.get("weights_normal",    [0.5, 0.3, 0.2])
    w_thermal   = ls_cfg.get("weights_thermal",   [0.3, 0.2, 0.5])
    w_bandwidth = ls_cfg.get("weights_bandwidth", [0.2, 0.5, 0.3])

    gpu_temp = metrics.get("gpu_temp_c", 0.0)

    if gpu_temp >= thermal_thresh:
        w = w_thermal
        preset = "thermal"
    elif n_active_cameras >= bw_thresh:
        w = w_bandwidth
        preset = "bandwidth"
    else:
        w = w_normal
        preset = "normal"

    return float(w[0]), float(w[1]), float(w[2]), preset


def _compute_load_score(metrics: dict, fps_stats: dict) -> tuple:
    """
    Compute load score with adaptive ω weights.

    Formula:
        base    = w_gpu * GPU_% + w_cpu * CPU_% + w_ram * RAM_%
        penalty = max(0, (TARGET_FPS - avg_fps) / TARGET_FPS * fps_penalty_max)
        score   = min(100, base + penalty)

    Returns (score: float, preset: str) where preset is the weight regime name.
    The preset is included in the heartbeat so peers can see which regime each
    node is operating in — this is the paper's "WAN context adaptation".
    """
    ls_cfg = _EDGE_CFG.get("load_score", {})
    fps_penalty_max = float(ls_cfg.get("fps_penalty_max", 30.0))

    n_active = len([v for v in fps_stats.values() if v > 0.0])
    w_gpu, w_cpu, w_ram, preset = _select_omega(metrics, n_active)

    base = (
        w_gpu * metrics["gpu_percent"] +
        w_cpu * metrics["cpu_percent"] +
        w_ram * metrics["ram_percent"]
    )

    active_fps_vals = [v for v in fps_stats.values() if v > 0.0]
    if active_fps_vals:
        avg_fps = sum(active_fps_vals) / len(active_fps_vals)
    elif fps_stats:
        avg_fps = 0.0
    else:
        avg_fps = TARGET_FPS   # no data → no penalty

    fps_drop = max(0.0, TARGET_FPS - avg_fps)
    penalty  = (fps_drop / TARGET_FPS) * fps_penalty_max

    score = min(100.0, base + penalty)
    return round(score, 1), preset


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
        # One-shot warmup_ms set by run_python.py after pipeline PLAYING
        self._warmup_ms: Optional[float] = None

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

        _zenoh_retry_interval = 30.0  # seconds between Zenoh reconnect attempts
        _last_zenoh_attempt = time.time()

        while self._running:
            try:
                # Periodically retry Zenoh if the session is not established
                if self._session is None:
                    if time.time() - _last_zenoh_attempt >= _zenoh_retry_interval:
                        logger.info("[HealthAgent] Retrying Zenoh connection...")
                        self._session, self._pub = self._connect_zenoh()
                        _last_zenoh_attempt = time.time()
                        if self._session:
                            logger.info("[HealthAgent] Zenoh reconnected successfully.")

                metrics    = self._collect_metrics()
                fps_stats  = _read_fps_stats()
                load_score, omega_preset = _compute_load_score(metrics, fps_stats)

                # BUG-I fix: exclude 0-fps cameras from avg_fps, matching the
                # exclusion applied in _compute_load_score() so the reported
                # avg_fps is consistent with the load_score value.
                active_fps_vals = [v for v in fps_stats.values() if v > 0.0]
                avg_fps = round(sum(active_fps_vals) / len(active_fps_vals), 1) if active_fps_vals else None

                # Consume one-shot warmup_ms written by run_python.py after
                # pipeline.set_state(PLAYING).  Reset after reading so it
                # only appears in the first heartbeat following a cold start.
                warmup_ms = self._warmup_ms
                self._warmup_ms = None

                payload = {
                    "type":          "health",
                    "node_id":       NODE_ID,
                    "timestamp":     time.time(),
                    "load_score":    load_score,
                    "omega_preset":  omega_preset,   # adaptive weight regime name
                    "gpu_percent":   metrics["gpu_percent"],
                    "cpu_percent":   metrics["cpu_percent"],
                    "ram_percent":   metrics["ram_percent"],
                    "gpu_temp_c":    metrics["gpu_temp_c"],
                    "power_mw":      metrics["power_mw"],
                    "pipeline": {
                        "fps_per_camera": fps_stats,
                        "avg_fps":        avg_fps,
                        "active_cameras": list(fps_stats.keys()),
                    },
                }

                if warmup_ms is not None:
                    payload["warmup_ms"] = warmup_ms

                logger.info(
                    "LoadScore=%.1f [%s] | GPU=%.1f%% CPU=%.1f%% RAM=%.1f%% "
                    "Temp=%.1f°C Power=%.0fmW | FPS=%s",
                    load_score, omega_preset,
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
