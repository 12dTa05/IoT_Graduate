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
    HEALTH_LOG_EVERY,
    TARGET_FPS,
    FPS_STATS_FILE,
    MONITOR_URL,
    ADVERTISE_IP,
    LOAD_POLICY,
    LOAD_MODEL,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("health_agent")


# ---------------------------------------------------------------------------
# FPS Reader (read JSON file written by SpeedProbe)
# ---------------------------------------------------------------------------

def _read_fps_stats() -> Dict[str, float]:
    """
    Read JSON file written by SpeedProbe containing FPS per camera.
    Return dict {camera_id: fps} or empty dict if file doesn't exist.

    File format:
        {
            "cam_01": 24.7,
            "cam_02": 25.1,
            "_updated_at": 1714739900.12,
            "_features": {"cam_01": {"n_track": 8.2, ...}, ...}
        }
    """
    try:
        with open(FPS_STATS_FILE, "r") as f:
            data = json.load(f)
        # Filter out meta-keys that are not cameras
        return {k: v for k, v in data.items()
                if not k.startswith("_") and isinstance(v, (int, float))}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug("Failed to read FPS stats: %s", exc)
        return {}


def _read_feature_stats() -> Dict[str, Dict[str, float]]:
    """
    Read per-camera proactive features written by SpeedProbe._fps_writer_loop.

    Returns {camera_id: {n_track, n_plate, stationary_fraction}} or {} on error.
    Gracefully returns empty when running without the DeepStream pipeline
    (e.g., during offline calibration or health-agent-only mode).
    """
    try:
        with open(FPS_STATS_FILE, "r") as f:
            data = json.load(f)
        return data.get("_features", {})
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug("Failed to read feature stats: %s", exc)
        return {}


def _read_offload_crops() -> dict:
    """
    Read _offload_crops snapshot written by SpeedProbe._fps_writer_loop.

    Returns {processed_count, received_per_s, ts} or {"received_per_s": 0.0}
    when the file is missing/offload receiver is absent.
    """
    try:
        with open(FPS_STATS_FILE, "r") as f:
            data = json.load(f)
        return data.get("_offload_crops", {"received_per_s": 0.0})
    except (FileNotFoundError, KeyError):
        return {"received_per_s": 0.0}
    except Exception as exc:
        logger.debug("Failed to read offload crops: %s", exc)
        return {"received_per_s": 0.0}


# ---------------------------------------------------------------------------
# Metric Collector
# ---------------------------------------------------------------------------

def _collect_jetson_metrics() -> Dict:
    """
    Called when jtop is unavailable (e.g. daemon not running or JetPack
    version mismatch).  Returns all-zero metrics so the health loop never
    crashes — the load score will just be driven entirely by the FPS penalty
    until jtop recovers.

    This project targets NVIDIA Jetson devices.  Jetson does not use
    nvidia-smi for the integrated GPU; jtop/tegrastats are the correct metric
    sources.  Therefore no generic nvidia-smi fallback is used here.
    """
    logger.warning(
        "[HealthAgent] jtop unavailable — metrics are zero. "
        "Ensure the jtop daemon is running: sudo systemctl start jtop"
    )
    return {
        "gpu_percent": 0.0,
        "cpu_percent": 0.0,
        "ram_percent": 0.0,
        "gpu_temp_c":  0.0,
        "power_mw":    0.0,
        "source": "jtop_unavailable",
    }


# Path to edge_node.yml and mtime for reload-on-use
_EDGE_NODE_YML = Path(__file__).resolve().parent / "configs" / "edge_node.yml"
_EDGE_CFG: dict = {}
_EDGE_CFG_MTIME: float = 0.0


def _load_edge_node_cfg() -> dict:
    """
    Read edge_node.yml. Returns the full parsed dict, or {} on error.
    Used by _maybe_reload_edge_cfg for mtime-based hot-reload.
    """
    try:
        import yaml
        with open(_EDGE_NODE_YML, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.debug("[HealthAgent] edge_node.yml load error: %s", exc)
        return {}


def _maybe_reload_edge_cfg() -> None:
    """
    Reload _EDGE_CFG if edge_node.yml mtime has changed since last read.
    Idempotent — called before every config-consuming operation.
    """
    global _EDGE_CFG, _EDGE_CFG_MTIME
    try:
        mtime = _EDGE_NODE_YML.stat().st_mtime
    except OSError:
        mtime = 0.0
    if mtime != _EDGE_CFG_MTIME:
        _EDGE_CFG = _load_edge_node_cfg()
        _EDGE_CFG_MTIME = mtime
        logger.info("[HealthAgent] edge_node.yml reloaded (mtime changed)")


def get_edge_cfg() -> dict:
    """Return the latest edge_node.yml config, reloading when the file changed."""
    _maybe_reload_edge_cfg()
    return _EDGE_CFG


# Seed at import time once, then mtime-based reload kicks in on each use.
_EDGE_CFG = _load_edge_node_cfg()
try:
    _EDGE_CFG_MTIME = _EDGE_NODE_YML.stat().st_mtime
except OSError:
    _EDGE_CFG_MTIME = 0.0


def _compute_load_score(metrics: dict, fps_stats: dict) -> tuple:
    """
    FPS-dominant load score with a lightweight hardware base and a configurable
    hardware saturation safety fuse.

    Formula:
        base    = w_gpu * GPU% + w_cpu * CPU% + w_ram * RAM%     (diagnostic whisper)
        penalty = drop_frac * fps_penalty_max                     (dominant signal)
        fuse    = hw_fuse_bonus when ANY of {GPU,CPU,RAM} >= hw_fuse_threshold, else 0
        score   = min(100.0, base + penalty + fuse)

    Hardware weights are intentionally tiny (0.05 each) — a diagnostic baseline
    that prevents a node with excellent FPS from appearing idle when its GPU
    happens to be 0 %.  The FPS drop is the real signal.

    Returns (score: float, "fps_dominant") — a single stable preset string so
    the heartbeat field stays consistent; no bandwidth/normal switching.
    """
    _maybe_reload_edge_cfg()
    ls_cfg = _EDGE_CFG.get("load_score", {})
    w_gpu  = float(ls_cfg.get("weight_gpu",  0.05))
    w_cpu  = float(ls_cfg.get("weight_cpu",  0.05))
    w_ram  = float(ls_cfg.get("weight_ram",  0.05))
    fps_penalty_max = float(ls_cfg.get("fps_penalty_max", 80.0))
    hw_fuse_threshold = float(ls_cfg.get("hw_fuse_threshold", 90.0))
    hw_fuse_bonus     = float(ls_cfg.get("hw_fuse_bonus",     25.0))

    base = (
        w_gpu * metrics.get("gpu_percent", 0.0) +
        w_cpu * metrics.get("cpu_percent", 0.0) +
        w_ram * metrics.get("ram_percent", 0.0)
    )

    active_fps_vals = [v for v in fps_stats.values() if v > 0.0]
    if active_fps_vals:
        avg_fps = sum(active_fps_vals) / len(active_fps_vals)
    elif fps_stats:
        avg_fps = 0.0
    else:
        avg_fps = TARGET_FPS   # no cameras → no penalty

    fps_drop = max(0.0, TARGET_FPS - avg_fps)
    penalty  = (fps_drop / TARGET_FPS) * fps_penalty_max

    # Hardware saturation safety fuse — one-shot bonus when any single metric
    # crosses the threshold (GPU, CPU, or RAM).  This catches the case where
    # the pipeline is thrashing a hardware ceiling but FPS hasn't collapsed yet,
    # e.g. a GPU at 92 % delivering 25 fps because DeepStream absorbs drops.
    fuse = 0.0
    if (metrics.get("gpu_percent", 0.0) >= hw_fuse_threshold or
        metrics.get("cpu_percent", 0.0) >= hw_fuse_threshold or
        metrics.get("ram_percent", 0.0) >= hw_fuse_threshold):
        fuse = hw_fuse_bonus

    score = min(100.0, base + penalty + fuse)
    return round(score, 1), "fps_dominant"


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
        # Proactive load model — instantiated lazily in _run so that
        # edge_node.yml is read after the process fully starts.
        self._proactive_model = None
        self._cam_configs_cache: Dict[str, dict] = {}
        self._last_cfg_reload = 0.0

    def _reload_cam_configs(self) -> Dict[str, dict]:
        """Read cameras.yml for peer failover metadata in health payloads."""
        try:
            import yaml

            cam_yml = Path(__file__).resolve().parent / "configs" / "cameras.yml"
            with open(cam_yml, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

            result: Dict[str, dict] = {}
            for cam_id, cfg in raw.get("cameras", {}).items():
                if cfg and cfg.get("enabled", True):
                    result[cam_id] = {
                        "camera_id":       cam_id,
                        "source_id":       int(cfg.get("source_id", 0)),
                        "uri":             cfg.get("uri", ""),
                        "name":            cfg.get("name", cam_id),
                        "fps":             float(cfg.get("fps", 25.0)),
                        "speed_limit_kmh": float(cfg.get("speed_limit_kmh", 80.0)),
                        "homography":      cfg.get("homography", {}),
                        "roi_polygon":     cfg.get("roi_polygon", []),
                        "output":          cfg.get("output", {}),
                    }
            self._cam_configs_cache = result
            return result
        except Exception as exc:
            logger.warning("[HealthAgent] Failed to reload camera configs: %s", exc)
            return self._cam_configs_cache

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
        """Try to open a persistent jtop session; return it or None.

        jtop is a Thread subclass.  The correct persistent usage (without
        the 'with' context manager) is:
            j = jtop()
            j.start()          # starts the background thread
            j.ok()             # BLOCKS until the first data packet arrives

        Calling any property (gpu, cpu, temperature, …) before ok() returns
        True raises KeyError because self._stats is still {}.

        Fix #15: run j.ok() in a daemon thread with a hard timeout so a hung
        jtop daemon (unresponsive hardware / JetPack mismatch) never deadlocks
        the HealthAgent startup.
        """
        try:
            from jtop import jtop as JTop
            j = JTop()
            j.start()
            # Block until the first data collection completes so _stats is
            # populated before _collect_metrics reads from it — but give up
            # after 10 seconds so a frozen jtop daemon never deadlocks startup.
            _ok_event = threading.Event()

            def _wait_ok():
                try:
                    if j.ok():
                        _ok_event.set()
                except Exception:
                    pass

            _t = threading.Thread(target=_wait_ok, daemon=True)
            _t.start()
            _t.join(timeout=10.0)

            if not _ok_event.is_set():
                logger.warning(
                    "[HealthAgent] jtop ok() did not return within 10 s "
                    "(hardware unresponsive?) — falling back to psutil."
                )
                try:
                    j.close()
                except Exception:
                    pass
                return None

            logger.info("[HealthAgent] jtop session opened and ready (persistent).")
            return j
        except Exception as exc:
            logger.debug("[HealthAgent] jtop unavailable: %s — using zero fallback.", exc)
            return None

    def _collect_metrics(self) -> Dict:
        """Read metrics from persistent jtop session.

        Reads from jtop's dedicated properties (gpu, cpu, memory, temperature,
        power) rather than from jtop.stats.  jtop.stats is a computed property
        that calls all sub-properties internally — if any one of them raises a
        KeyError (e.g. 'power' not in _stats) the entire stats call fails.
        Reading each property individually lets us handle missing sensors
        gracefully without losing GPU% and Temp.

        If jtop is unavailable, returns all-zero metrics with source='jtop_unavailable'.
        """
        if self._jtop is not None:
            try:
                # --- GPU % ---
                # jtop.gpu is a GPU object (dict-like): {name: {status: {load:…}}}
                # The first GPU entry's status.load is the utilisation percentage.
                gpu_pct = 0.0
                try:
                    for gpu_info in self._jtop.gpu.values():
                        load = gpu_info.get("status", {}).get("load", 0.0)
                        gpu_pct = float(load)
                        break  # only first GPU
                except Exception:
                    pass

                # --- CPU % ---
                # jtop.cpu = {"total": {"idle": float, …}, "cpu": […]}
                # total.idle is the aggregate idle percentage across all cores.
                cpu_pct = 0.0
                try:
                    cpu_total = self._jtop.cpu.get("total", {})
                    cpu_idle  = cpu_total.get("idle", 100.0)
                    cpu_pct   = 100.0 - float(cpu_idle)
                except Exception:
                    pass

                # --- RAM % ---
                # jtop.memory["RAM"] = {"used": int KB, "tot": int KB, …}
                ram_pct = 0.0
                try:
                    mem = self._jtop.memory
                    ram_tot = mem["RAM"]["tot"]
                    if ram_tot > 0:
                        ram_pct = float(mem["RAM"]["used"]) / ram_tot * 100.0
                except Exception:
                    pass

                # --- Temperature ---
                # jtop.temperature = {sensor_name: {"temp": float, "online": bool, …}}
                # Sensor names on Orin/JetPack 6: "cpu", "gpu", "soc0", "soc1",
                # "soc2", "tj" etc.  Pick "gpu" first, then "tj" (junction),
                # then the first online sensor with a plausible value.
                temp_c = 0.0
                try:
                    temp_dict = self._jtop.temperature
                    for key in ("gpu", "tj", "cpu"):
                        info = temp_dict.get(key)
                        if not isinstance(info, dict):
                            continue
                        t = info.get("temp", -256)
                        if isinstance(t, (int, float)) and 0 < t < 120:
                            temp_c = float(t)
                            break
                    else:
                        # fallback: first sensor with plausible value
                        for info in temp_dict.values():
                            if not isinstance(info, dict):
                                continue
                            t = info.get("temp", -256)
                            if isinstance(t, (int, float)) and 0 < t < 120:
                                temp_c = float(t)
                                break
                except Exception:
                    pass

                # --- Power ---
                # jtop.power = {"rail": {…}, "tot": {"power": int mW, …}}
                # May not exist on all boards; guard carefully.
                power_mw = 0.0
                try:
                    pwr = self._jtop.power
                    if isinstance(pwr, dict):
                        power_mw = float(pwr.get("tot", {}).get("power", 0))
                except Exception:
                    pass

                return {
                    "gpu_percent": round(gpu_pct, 1),
                    "cpu_percent": round(cpu_pct, 1),
                    "ram_percent": round(ram_pct, 1),
                    "gpu_temp_c":  round(temp_c, 1),
                    "power_mw":    round(power_mw, 0),
                    "source":      "jtop",
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

        # Instantiate proactive model using the proactive: section of edge_node.yml.
        get_edge_cfg()
        from speedflow_python.load_model import ProactiveModel
        self._proactive_model = ProactiveModel(
            _EDGE_CFG.get("proactive", {}),
            policy=LOAD_POLICY,
            model_type=LOAD_MODEL,
        )
        logger.info("[HealthAgent] LOAD_POLICY=%s LOAD_MODEL=%s", LOAD_POLICY, LOAD_MODEL)
        if self._proactive_model.enabled:
            logger.info("[HealthAgent] Proactive load model ENABLED "
                        "(risk_threshold=%.2f)", self._proactive_model.risk_threshold)
        else:
            logger.info("[HealthAgent] Proactive load model disabled "
                        "(set proactive.enabled: true in edge_node.yml to activate)")

        _zenoh_retry_interval = 30.0  # seconds between Zenoh reconnect attempts
        _last_zenoh_attempt = time.time()
        _log_cycle = 0  # counts health cycles; log LoadScore every HEALTH_LOG_EVERY
        _cfg_reload_interval = 30.0
        self._last_cfg_reload = 0.0

        while self._running:
            try:
                if time.monotonic() - self._last_cfg_reload >= _cfg_reload_interval:
                    _maybe_reload_edge_cfg()
                    if self._proactive_model is not None:
                        self._proactive_model.reload_cfg(
                            get_edge_cfg().get("proactive", {})
                        )
                    self._reload_cam_configs()
                    self._last_cfg_reload = time.monotonic()

                # Periodically retry Zenoh if the session is not established
                if self._session is None:
                    if time.time() - _last_zenoh_attempt >= _zenoh_retry_interval:
                        logger.info("[HealthAgent] Retrying Zenoh connection...")
                        self._session, self._pub = self._connect_zenoh()
                        _last_zenoh_attempt = time.time()
                        if self._session:
                            logger.info("[HealthAgent] Zenoh reconnected successfully.")

                metrics       = self._collect_metrics()
                fps_stats     = _read_fps_stats()
                feature_stats = _read_feature_stats()
                offload_crops = _read_offload_crops()
                offload_crops_received_per_s = float(offload_crops.get("received_per_s", 0.0))
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
                    # Metric source ("jtop" or "jtop_unavailable") so the dashboard
                    # can render GPU%/Temp as "N/A" instead of a misleading 0.0.
                    "source":        metrics.get("source", "jtop"),
                    "pipeline": {
                        "fps_per_camera": fps_stats,
                        "avg_fps":        avg_fps,
                        # BUG-15 fix: only include cameras actively producing
                        # frames — 0-FPS entries are stale/removed cameras.
                        "active_cameras": [k for k, v in fps_stats.items() if v > 0.0],
                        # Used by peers for failover ADD commands when this node dies.
                        "camera_configs": self._cam_configs_cache,
                    },
                }

                if warmup_ms is not None:
                    payload["warmup_ms"] = warmup_ms

                # ── Proactive model ────────────────────────────────────────
                # Compute and merge proactive fields when enabled.
                # The legacy load_score is always present for backward-compat
                # (reactive baseline used in Chart 2 comparison).
                if self._proactive_model is not None:
                    # Filter to cameras with fps > 0 — matches collector's
                    # n_active_cameras definition in profile_collect.py.
                    _active_ids = {k for k, v in fps_stats.items() if v > 0.0}
                    proactive_result = self._proactive_model.compute(
                        metrics,
                        {k: v for k, v in feature_stats.items() if k in _active_ids},
                        offload_crops_received_per_s=offload_crops_received_per_s,
                    )
                    payload.update(proactive_result)

                if self._pub:
                    self._pub.put(msgpack.packb(payload, use_bin_type=True))

                _log_cycle += 1
                if _log_cycle % HEALTH_LOG_EVERY == 1:
                    _risk_str = (
                        f" | U={payload.get('risk_index', 0.0):.3f}"
                        f" L={payload.get('l_proactive', 0.0):.3f}"
                        f" H={payload.get('h_reactive', 0.0):.3f}"
                        if payload.get("proactive_enabled") else ""
                    )
                    logger.info(
                        "LoadScore=%.1f [%s] | GPU=%.1f%% CPU=%.1f%% RAM=%.1f%% "
                        "Temp=%.1f°C Power=%.0fmW | FPS=%s%s",
                        load_score, omega_preset,
                        metrics["gpu_percent"],
                        metrics["cpu_percent"],
                        metrics["ram_percent"],
                        metrics["gpu_temp_c"],
                        metrics["power_mw"],
                        fps_stats,
                        _risk_str,
                    )

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
# Standalone functions for use by run_python.py's health_push_loop.
# These expose a persistent jtop open/collect pattern without requiring
# a HealthAgent instance (avoids the __new__ stub hack).
# ---------------------------------------------------------------------------

def open_jtop_session():
    """
    Open and return a persistent jtop session.
    Identical to HealthAgent._open_jtop() — run_python.py can call this
    directly to avoid the HealthAgent.__new__ stub pattern.
    Returns the jtop object or None.
    """
    _h = HealthAgent()
    return HealthAgent._open_jtop(_h)


def collect_metrics(jtop_session):
    """
    Read metrics from a persistent jtop session.
    Identical to HealthAgent._collect_metrics() — takes the jtop object
    returned by open_jtop_session().
    If jtop_session is None, returns all-zero fallback metrics.
    """
    _h = HealthAgent()
    _h._jtop = jtop_session
    return HealthAgent._collect_metrics(_h)


# ---------------------------------------------------------------------------
# Entry point (run standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        import zenoh
    except ImportError:
        logger.error("zenoh not installed. Run: pip install zenoh")
        sys.exit(1)

    # PID file lock — prevent two health_agent instances for the same node.
    # Two instances connecting with the same node_id cause the server to close
    # the older connection (code 1000) every time the newer one sends a message,
    # creating an endless close/reconnect loop.
    import os as _os
    _PID_FILE = Path(__file__).resolve().parent / "health_agent.pid"
    _my_pid   = _os.getpid()
    if _PID_FILE.exists():
        try:
            _old_pid = int(_PID_FILE.read_text().strip())
            if _old_pid != _my_pid:
                try:
                    _os.kill(_old_pid, 0)   # signal 0 = existence check only
                    logger.error(
                        "health_agent.py is already running (PID %d). "
                        "Kill it first: kill %d", _old_pid, _old_pid
                    )
                    sys.exit(1)
                except OSError:
                    pass   # old process is dead — stale PID file, safe to continue
        except ValueError:
            pass
    _PID_FILE.write_text(str(_my_pid))

    import atexit as _atexit
    _atexit.register(lambda: _PID_FILE.unlink(missing_ok=True))

    agent = HealthAgent()
    agent.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping HealthAgent...")
        agent.stop()
