"""
speedflow_python/load_model.py

Phase 2 — Proactive Load Model (pure functions, no jtop / Zenoh deps).

Implements:
  L_proactive  — CV-plane workload predictor (vehicle/plate/stationarity features)
  H_reactive   — hardware safety fuse (max utilisation × thermal penalty)
  Θ_thermal    — smooth thermal ramp replacing the discrete omega-thermal preset
  CycleSmoother — sliding-window averager aligned to the traffic-light period
  fuse()        — noisy-OR risk combination U = 1-(1-L̂)(1-Ĥ)

All parameters are read from the 'proactive' section of edge_node.yml, loaded
once at construction time and refreshable via reload_cfg().

Design principles
-----------------
* Pure functions for L_proactive, H_reactive, fuse — unit-testable without
  hardware.
* CycleSmoother is the only stateful class; one instance per node in
  health_agent._run.
* Θ_thermal replaces the omega 'thermal' preset: _select_omega now only has
  'normal' and 'bandwidth', and H_reactive multiplies by Θ_thermal after the
  max() call.  This avoids double-counting.
* L̂ and Ĥ are independently clamped to [0,1] before fusion so the noisy-OR
  formula is always well-defined.
* When proactive.enabled is False the module is imported but fuse() is never
  called — the legacy load_score path is untouched.

Zenoh heartbeat additions
--------------------------
The health payload gains five new fields when proactive.enabled is True:
  l_proactive         float [0,1]  raw (non-smoothed) proactive index
  h_reactive          float [0,1]  raw reactive index
  risk_index          float [0,1]  cycle-smoothed noisy-OR U
  n_track_mean        float        avg vehicles/frame across cameras
  n_plate_mean        float        avg plate detections/frame
  stationary_fraction float        avg fraction of stationary vehicles
"""

from __future__ import annotations

import collections
import logging
import math
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (used when edge_node.yml section is absent or keys are missing)
# ---------------------------------------------------------------------------

_DEFAULT_CFG: dict = {
    "enabled":        False,
    "w_base":         12.0,
    "alpha1":         0.0,
    "alpha2":         0.0,
    "beta":           0.0,
    "gamma":          0.0,
    "cycle_window_s": 90.0,
    "risk_threshold": 0.85,
    "theta_thermal": {
        "t_low":    75.0,
        "t_high":   90.0,
        "max_mult": 1.25,
    },
}


# ---------------------------------------------------------------------------
# Θ_thermal — smooth linear ramp replacing the discrete omega thermal preset
# ---------------------------------------------------------------------------

def theta_thermal(gpu_temp_c: float, cfg: dict) -> float:
    """
    Smooth thermal multiplier in [1.0, max_mult].

    Linear ramp from 1.0 at t_low to max_mult at t_high.
    Below t_low → 1.0 (no penalty).
    Above t_high → max_mult (full penalty, capped).

    Parameters are read from cfg['theta_thermal']:
        t_low    (float) — ramp start temperature in °C   (default 75)
        t_high   (float) — ramp end temperature in °C    (default 90)
        max_mult (float) — multiplier at t_high           (default 1.25)

    The ramp replaces the hard step at 80°C from the original plan and
    eliminates the discrete omega 'thermal' preset that previously double-
    counted the same signal.
    """
    th_cfg   = cfg.get("theta_thermal", _DEFAULT_CFG["theta_thermal"])
    t_low    = float(th_cfg.get("t_low",    75.0))
    t_high   = float(th_cfg.get("t_high",   90.0))
    max_mult = float(th_cfg.get("max_mult", 1.25))

    if gpu_temp_c <= t_low:
        return 1.0
    if gpu_temp_c >= t_high:
        return max_mult
    frac = (gpu_temp_c - t_low) / (t_high - t_low)
    return 1.0 + frac * (max_mult - 1.0)


# ---------------------------------------------------------------------------
# H_reactive — hardware safety fuse
# ---------------------------------------------------------------------------

def compute_h_reactive(
    gpu_percent: float,
    cpu_percent: float,
    ram_percent: float,
    gpu_temp_c:  float,
    cfg: dict,
) -> float:
    """
    H_reactive = max(R_GPU, R_CPU, R_RAM) × Θ_thermal  ∈ [0, 1]

    Takes the worst utilisation (worst-case bottleneck) then multiplies by the
    thermal ramp.  Result is clamped to [0, 1].

    Units: all utilisation inputs in percent (0–100); normalised internally.
    """
    r_gpu = gpu_percent / 100.0
    r_cpu = cpu_percent / 100.0
    r_ram = ram_percent / 100.0

    base = max(r_gpu, r_cpu, r_ram)
    th   = theta_thermal(gpu_temp_c, cfg)
    return min(1.0, base * th)


# ---------------------------------------------------------------------------
# L_proactive — CV-plane workload predictor
# ---------------------------------------------------------------------------

def compute_l_proactive(
    feature_stats: Dict[str, Dict[str, float]],
    cfg: dict,
    policy: str = "predict_with_base",
) -> Tuple[float, float, float, float]:
    """
    L_proactive = (W_base + Σ_m[α₁·N_track + α₂·N_track² + β·N_plate + γ·S_m]) / 100

    Normalised to [0, 1] by dividing by 100 (same scale as GPU percent).

    feature_stats: {camera_id: {n_track, n_plate, stationary_fraction}}
                   as written by SpeedProbe._flush_features()

    Returns (L_proactive, n_track_mean, n_plate_mean, stationary_fraction_mean)
    so the caller can include raw feature values in the heartbeat.
    """
    if policy == "actual":
        return 0.0, 0.0, 0.0, 0.0

    w_base = (
        float(cfg.get("w_base",  _DEFAULT_CFG["w_base"]))
        if policy == "predict_with_base"
        else 0.0
    )
    alpha1 = float(cfg.get("alpha1",  0.0))
    alpha2 = float(cfg.get("alpha2",  0.0))
    beta   = float(cfg.get("beta",    0.0))
    gamma  = float(cfg.get("gamma",   0.0))

    workload = w_base
    n_track_vals  = []
    n_plate_vals  = []
    stat_frac_vals = []

    for cam_feats in feature_stats.values():
        n_track = float(cam_feats.get("n_track",             0.0))
        n_plate = float(cam_feats.get("n_plate",             0.0))
        s_m     = float(cam_feats.get("stationary_fraction", 0.0))

        workload += (alpha1 * n_track
                     + alpha2 * n_track ** 2
                     + beta   * n_plate
                     + gamma  * s_m)

        n_track_vals.append(n_track)
        n_plate_vals.append(n_plate)
        stat_frac_vals.append(s_m)

    l_raw = min(1.0, max(0.0, workload / 100.0))

    n_track_mean  = (sum(n_track_vals)  / len(n_track_vals)  if n_track_vals  else 0.0)
    n_plate_mean  = (sum(n_plate_vals)  / len(n_plate_vals)  if n_plate_vals  else 0.0)
    stat_frac_mean = (sum(stat_frac_vals) / len(stat_frac_vals) if stat_frac_vals else 0.0)

    return l_raw, n_track_mean, n_plate_mean, stat_frac_mean


class DLPredictor:
    """Small ONNX time-series predictor using 3 traffic features."""

    def __init__(self, dl_cfg: dict, edge_root: Optional[Path] = None) -> None:
        self._window_k = max(1, int(dl_cfg.get("window_k", 5)))
        self._history: collections.deque = collections.deque(maxlen=self._window_k)
        self._session = None
        self._input_name = None
        model_path = str(dl_cfg.get("model_path", "")).strip()
        self._model_path = self._resolve_path(model_path, edge_root)
        if self._model_path:
            self._load_model()

    @staticmethod
    def _resolve_path(model_path: str, edge_root: Optional[Path]) -> str:
        if not model_path:
            return ""
        path = Path(model_path)
        if not path.is_absolute():
            root = edge_root or Path(__file__).resolve().parents[1]
            path = root / path
        return str(path)

    def _load_model(self) -> None:
        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(self._model_path)
            self._input_name = self._session.get_inputs()[0].name
            logger.info("[DLPredictor] Loaded ONNX model: %s", self._model_path)
        except Exception as exc:
            logger.warning("[DLPredictor] Disabled; failed to load %s: %s", self._model_path, exc)
            self._session = None
            self._input_name = None

    @staticmethod
    def _feature_means(feature_stats: Dict[str, Dict[str, float]]) -> Tuple[float, float, float]:
        n_track_vals = []
        n_plate_vals = []
        stat_vals = []
        for cam_feats in feature_stats.values():
            n_track_vals.append(float(cam_feats.get("n_track", 0.0)))
            n_plate_vals.append(float(cam_feats.get("n_plate", 0.0)))
            stat_vals.append(float(cam_feats.get("stationary_fraction", 0.0)))
        n_track = sum(n_track_vals) / len(n_track_vals) if n_track_vals else 0.0
        n_plate = sum(n_plate_vals) / len(n_plate_vals) if n_plate_vals else 0.0
        stat = sum(stat_vals) / len(stat_vals) if stat_vals else 0.0
        return n_track, n_plate, stat

    def predict(
        self,
        feature_stats: Dict[str, Dict[str, float]],
    ) -> Tuple[float, float, float, float]:
        n_track, n_plate, stat = self._feature_means(feature_stats)
        self._history.append([n_track, n_plate, stat])

        if self._session is None or self._input_name is None:
            return 0.0, n_track, n_plate, stat
        if len(self._history) < self._window_k:
            return 0.0, n_track, n_plate, stat

        try:
            import numpy as np
            x = np.asarray([list(self._history)], dtype=np.float32)
            y = self._session.run(None, {self._input_name: x})[0]
            pred = float(np.asarray(y).reshape(-1)[0])
        except Exception as exc:
            logger.debug("[DLPredictor] inference failed: %s", exc, exc_info=True)
            return 0.0, n_track, n_plate, stat

        # DL output is already the selected target on the 0-100 load scale.
        # For predict_with_base, train on load_score/gpu_percent so base cost is
        # included in the target. For predict_no_base, train on a delta-load
        # target that excludes base cost. Do not add W_base here, or with-base
        # DL predictions double-count the idle pipeline workload.
        l_raw = min(1.0, max(0.0, pred / 100.0))
        return l_raw, n_track, n_plate, stat


# ---------------------------------------------------------------------------
# Noisy-OR fusion
# ---------------------------------------------------------------------------

def fuse(l_hat: float, h_hat: float) -> float:
    """
    U = 1 - (1 - L̂)(1 - Ĥ)   noisy-OR heuristic.

    L̂ and Ĥ must be in [0, 1].  If either tier saturates → U → 1.
    """
    l_hat = min(1.0, max(0.0, l_hat))
    h_hat = min(1.0, max(0.0, h_hat))
    return 1.0 - (1.0 - l_hat) * (1.0 - h_hat)


# ---------------------------------------------------------------------------
# CycleSmoother — sliding-window average aligned to the signal cycle
# ---------------------------------------------------------------------------

class CycleSmoother:
    """
    Maintains a deque of (timestamp, value) pairs and returns the time-
    weighted mean over the last `window_s` seconds.

    One instance per scalar (L_proactive, H_reactive, U) per node.

    Thread-safety: NOT thread-safe — caller must hold a lock if called from
    multiple threads.  health_agent._run is single-threaded so no lock needed.

    window_s: sliding window duration in seconds.  Should be set to
              cycle_window_s from the proactive config (≈ one signal cycle,
              default 90 s).
    """

    def __init__(self, window_s: float = 90.0) -> None:
        self._window_s = window_s
        self._buf: collections.deque = collections.deque()  # (ts, value)

    def update(self, value: float, ts: Optional[float] = None) -> float:
        """
        Push a new value and return the windowed mean.

        ts: wall-clock timestamp (default: time.time()).
        """
        now = ts if ts is not None else time.time()
        self._buf.append((now, float(value)))
        cutoff = now - self._window_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()
        if not self._buf:
            return float(value)
        return sum(v for _, v in self._buf) / len(self._buf)

    def mean(self) -> float:
        """Current windowed mean without pushing a new value."""
        if not self._buf:
            return 0.0
        return sum(v for _, v in self._buf) / len(self._buf)

    def reset(self) -> None:
        self._buf.clear()


# ---------------------------------------------------------------------------
# ProactiveModel — convenience wrapper used by health_agent and run_python
# ---------------------------------------------------------------------------

class ProactiveModel:
    """
    Stateful wrapper: holds three CycleSmoothers (L, H, U) and the current
    config.  Call compute() once per health cycle; it returns a dict ready to
    merge into the heartbeat payload.

    Usage in health_agent._run:
        model = ProactiveModel(edge_cfg.get("proactive", {}))
        ...
        result = model.compute(metrics, feature_stats)
        if result["proactive_enabled"]:
            payload.update(result)
    """

    def __init__(self, proactive_cfg: dict, policy: str = "predict_with_base", model_type: str = "formula") -> None:
        self._cfg = {**_DEFAULT_CFG, **proactive_cfg}
        self._policy = str(policy or "actual").strip().lower()
        self._model_type = str(model_type or "formula").strip().lower()
        if self._policy not in {"actual", "predict_no_base", "predict_with_base"}:
            logger.warning("[ProactiveModel] Invalid policy=%r; using actual", self._policy)
            self._policy = "actual"
        if self._model_type not in {"formula", "dl"}:
            logger.warning("[ProactiveModel] Invalid model_type=%r; using formula", self._model_type)
            self._model_type = "formula"
        window_s  = float(self._cfg.get("cycle_window_s", 90.0))
        self._smoother_l = CycleSmoother(window_s)
        self._smoother_h = CycleSmoother(window_s)
        self._smoother_u = CycleSmoother(window_s)
        self._dl_predictor = None
        if self._model_type == "dl":
            self._dl_predictor = DLPredictor(self._cfg.get("dl_model", {}))
        self._warn_if_inert()

    def reload_cfg(self, proactive_cfg: dict) -> None:
        """Hot-reload coefficients from updated edge_node.yml."""
        old_dl_path = ""
        if self._dl_predictor is not None:
            old_dl_path = self._dl_predictor._model_path
        self._cfg = {**_DEFAULT_CFG, **proactive_cfg}
        if self._model_type == "dl":
            new_predictor = DLPredictor(self._cfg.get("dl_model", {}))
            if new_predictor._model_path != old_dl_path:
                self._dl_predictor = new_predictor
        self._warn_if_inert()

    def _warn_if_inert(self) -> None:
        if not self.enabled:
            return
        if self._model_type == "dl":
            return
        coeffs = ("alpha1", "alpha2", "beta", "gamma")
        if all(float(self._cfg.get(k, 0.0)) == 0.0 for k in coeffs):
            logger.warning(
                "[ProactiveModel] enabled but alpha1/alpha2/beta/gamma are all zero; "
                "L_proactive will stay at w_base/100 until coefficients are fitted."
            )

    @property
    def enabled(self) -> bool:
        return self._policy != "actual" and bool(self._cfg.get("enabled", False))

    @property
    def risk_threshold(self) -> float:
        return float(self._cfg.get("risk_threshold", 0.85))

    def compute(
        self,
        metrics:       dict,
        feature_stats: Dict[str, Dict[str, float]],
        ts:            Optional[float] = None,
    ) -> dict:
        """
        Run one compute cycle.

        metrics:       from HealthAgent._collect_metrics()
                       {gpu_percent, cpu_percent, ram_percent, gpu_temp_c, ...}
        feature_stats: from _read_feature_stats()
                       {camera_id: {n_track, n_plate, stationary_fraction}}

        Returns a dict with all proactive fields.  If enabled=False the dict
        only contains proactive_enabled=False; caller can merge safely without
        polluting the payload with zeroes.
        """
        if not self.enabled:
            return {
                "proactive_enabled": False,
                "load_policy": self._policy,
                "load_model": self._model_type,
            }

        # Raw tiers
        if self._model_type == "dl" and self._dl_predictor is not None:
            l_raw, n_track_mean, n_plate_mean, stat_frac = self._dl_predictor.predict(feature_stats)
        else:
            l_raw, n_track_mean, n_plate_mean, stat_frac = compute_l_proactive(
                feature_stats, self._cfg, policy=self._policy
            )
        h_raw = compute_h_reactive(
            gpu_percent=metrics.get("gpu_percent", 0.0),
            cpu_percent=metrics.get("cpu_percent", 0.0),
            ram_percent=metrics.get("ram_percent", 0.0),
            gpu_temp_c =metrics.get("gpu_temp_c",  0.0),
            cfg=self._cfg,
        )
        u_raw = fuse(l_raw, h_raw)

        # Cycle-averaged (smoothed) values — what drives the control decision
        l_smooth = self._smoother_l.update(l_raw,  ts)
        h_smooth = self._smoother_h.update(h_raw,  ts)
        u_smooth = self._smoother_u.update(u_raw,  ts)

        return {
            "proactive_enabled":   True,
            "load_policy":         self._policy,
            "load_model":          self._model_type,
            "l_proactive":         round(l_smooth, 4),
            "h_reactive":          round(h_smooth, 4),
            "risk_index":          round(u_smooth, 4),   # drives offload trigger
            "l_proactive_instant": round(l_raw,    4),   # for Chart 1 logging
            "h_reactive_instant":  round(h_raw,    4),
            "risk_index_instant":  round(u_raw,    4),
            "n_track_mean":        round(n_track_mean,  2),
            "n_plate_mean":        round(n_plate_mean,  2),
            "stationary_fraction": round(stat_frac,     3),
            "theta_thermal":       round(
                theta_thermal(metrics.get("gpu_temp_c", 0.0), self._cfg), 4),
        }
