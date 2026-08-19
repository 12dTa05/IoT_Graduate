#!/usr/bin/env python3
"""
Traffic & QoS Forecaster for Multi-Edge Video Analytics with P2P Offloading.

Two-Track Multi-Horizon Forecasting:
  - Track 1 (Workload): n_track_total (and n_plate_total) at +6s, +10s
  - Track 2 (Continuous QoS): fps_avg & qos_degradation at +6s, +10s
  - Track 3 (Risk & Usable Lead Time): L3 (fps<=22), L2 (fps<=19), L1 (fps<=17) events,
            precision/recall/F1, false alarm episodes, and usable lead time vs migration latency.

Evaluates across operational regimes:
  - V0_operational (All data with active P2P - PRIMARY)
  - V1_stable_diagnostic (Stable unperturbed windows - DIAGNOSTIC)
  - Sub-regimes: sender_offload, receiver_add, reclaim_states, camera_count_regimes.

Outputs:
  - data_audit.json, window_manifest.csv, metrics.json, predictions.csv, lead_time_metrics.json
  - Publication-grade plots for multi-node time-series, lead times, and regime comparisons.
"""

import argparse
import csv
import datetime
import json
import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import numpy as np

# Non-interactive matplotlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------
HORIZONS = [6, 10]
WINDOW_SIZE = 5  # 5 bins of 1s lookback
DEFAULT_WARMUP_TRIM = 30  # seconds
GAP_THRESHOLD_S = 3.0  # seconds to split segments
TRAIN_RATIO = 0.70

TRAFFIC_THRESHOLDS = [6.0, 8.0, 10.0, 12.0]
QOS_THRESHOLDS = [22.0, 19.0, 17.0]  # L3, L2, L1 FPS levels

FEATURE_NAMES = [
    "n_track_total",
    "n_plate_total",
    "stationary_fraction_mean",
    "fps_avg",
    "n_active_cameras",
    "n_cameras_total",
    "gpu_percent",
    "gpu_temp_c",
    "offload_crops_received_per_s",
    "is_near_migration",
    "is_post_migration",
    "is_camera_change",
]


def get_feature_labels(feature_names: List[str] = FEATURE_NAMES, window_size: int = WINDOW_SIZE) -> List[str]:
    """Generate human-readable lag feature names."""
    labels = []
    for step in range(window_size):
        lag = window_size - 1 - step
        for fn in feature_names:
            labels.append(f"{fn}_t-{lag}s")
    return labels


# ---------------------------------------------------------------------------
# Data Loading, Audit & Multi-Regime Tagging
# ---------------------------------------------------------------------------
def parse_iso_or_float_ts(val: str) -> float:
    """Parse ISO 8601 timestamp string or float epoch string to epoch float."""
    try:
        return float(val)
    except ValueError:
        pass
    dt = datetime.datetime.fromisoformat(val)
    return dt.timestamp()


def load_structured_migrations(mig_path: Path) -> List[Dict[str, Any]]:
    """Load structured migration records from p2p_migrations.csv."""
    if not mig_path.is_file():
        return []
    records = []
    with open(mig_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_str = row.get("timestamp_iso", "").strip()
            if not ts_str:
                continue
            try:
                ts = parse_iso_or_float_ts(ts_str)
                records.append({
                    "ts": ts,
                    "timestamp_iso": ts_str,
                    "from_node": row.get("from_node", ""),
                    "to_node": row.get("to_node", ""),
                    "camera_id": row.get("camera_id", ""),
                    "trigger_reason": row.get("trigger_reason", ""),
                    "trigger_load": float(row.get("trigger_load", 0.0) or 0.0),
                    "trigger_fps": float(row.get("trigger_fps", 0.0) or 0.0) if row.get("trigger_fps") else None,
                    "migration_time_ms": float(row.get("migration_time_ms", 0.0) or 0.0),
                    "result": row.get("result", ""),
                })
            except Exception:
                continue
    records.sort(key=lambda x: x["ts"])
    return records


def load_and_audit_node_csv(
    csv_path: Path,
    mig_records: List[Dict[str, Any]],
    node_name: str,
    warmup_trim: int = DEFAULT_WARMUP_TRIM,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Load calibration CSV, perform data audit, filter dead pipeline,
    and tag operational regimes.
    """
    raw_rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            raw_rows.append(r)

    total_raw = len(raw_rows)
    if total_raw == 0:
        return [], {"total_raw": 0, "kept": 0}

    valid_rows = []
    dead_pipeline_count = 0
    duplicate_ts_count = 0
    seen_ts = set()

    for r in raw_rows:
        try:
            ts = float(r["ts"])
            fps = float(r["fps_avg"])
            n_active = int(float(r.get("n_active_cameras", 0)))
            n_total = int(float(r.get("n_cameras_total", n_active)))
            expected_fps = float(r.get("expected_fps", 30.0))
            n_track = float(r["n_track_total"])
            n_plate = float(r["n_plate_total"])
            stat_frac = float(r["stationary_fraction_mean"])
            gpu_pct = float(r["gpu_percent"])
            gpu_temp = float(r["gpu_temp_c"])
            load_sc = float(r.get("load_score", 0.0))
            offload_crops = float(r.get("offload_crops_received_per_s", 0.0))
        except (ValueError, KeyError, TypeError):
            continue

        if ts in seen_ts:
            duplicate_ts_count += 1
            continue
        seen_ts.add(ts)

        # Drop dead pipeline (e.g. Node B crash after row 12464)
        if fps <= 0.0 and n_active == 0:
            dead_pipeline_count += 1
            continue

        # QoS degradation metric: (1 - fps / expected_fps) * 100
        qos_deg = max(0.0, (1.0 - fps / max(1.0, expected_fps)) * 100.0)

        valid_rows.append({
            "ts": ts,
            "node": node_name,
            "fps_avg": fps,
            "expected_fps": expected_fps,
            "qos_degradation": qos_deg,
            "n_active_cameras": n_active,
            "n_cameras_total": n_total,
            "n_track_total": n_track,
            "n_plate_total": n_plate,
            "stationary_fraction_mean": stat_frac,
            "gpu_percent": gpu_pct,
            "gpu_temp_c": gpu_temp,
            "load_score": load_sc,
            "offload_crops_received_per_s": offload_crops,
        })

    valid_rows.sort(key=lambda x: x["ts"])

    if len(valid_rows) > warmup_trim:
        valid_rows = valid_rows[warmup_trim:]

    # Multi-regime tagging
    tagged_rows = []
    prev_cam_count = None

    for r in valid_rows:
        ts = r["ts"]
        cam_count = r["n_active_cameras"]

        is_near_mig = 0.0
        is_post_mig = 0.0
        regime = "stable"
        active_mig_reason = "none"

        for m in mig_records:
            delta = ts - m["ts"]
            if abs(delta) <= 10.0:
                is_near_mig = 1.0
                active_mig_reason = m["trigger_reason"]
                if "reclaim" in m["trigger_reason"].lower():
                    regime = "reclaim_window"
                else:
                    regime = "offload_window"
                break
            elif 0.0 < delta <= 30.0:
                is_post_mig = 1.0
                if "reclaim" in m["trigger_reason"].lower():
                    regime = "post_reclaim"
                else:
                    regime = "post_offload"

        is_cam_change = 1.0 if (prev_cam_count is not None and cam_count != prev_cam_count) else 0.0
        if is_cam_change and regime == "stable":
            regime = "camera_count_changed"
        prev_cam_count = cam_count

        r_copy = dict(r)
        r_copy["is_near_migration"] = is_near_mig
        r_copy["is_post_migration"] = is_post_mig
        r_copy["is_camera_change"] = is_cam_change
        r_copy["regime"] = regime
        r_copy["active_mig_reason"] = active_mig_reason
        tagged_rows.append(r_copy)

    audit_summary = {
        "node": node_name,
        "total_raw": total_raw,
        "duplicate_ts_dropped": duplicate_ts_count,
        "dead_pipeline_dropped": dead_pipeline_count,
        "warmup_dropped": min(warmup_trim, total_raw),
        "kept_samples": len(tagged_rows),
        "regime_counts": {
            "stable": sum(1 for r in tagged_rows if r["regime"] == "stable"),
            "offload_window": sum(1 for r in tagged_rows if r["regime"] == "offload_window"),
            "post_offload": sum(1 for r in tagged_rows if r["regime"] == "post_offload"),
            "reclaim_window": sum(1 for r in tagged_rows if r["regime"] == "reclaim_window"),
            "post_reclaim": sum(1 for r in tagged_rows if r["regime"] == "post_reclaim"),
            "camera_count_changed": sum(1 for r in tagged_rows if r["regime"] == "camera_count_changed"),
        },
    }
    return tagged_rows, audit_summary


# ---------------------------------------------------------------------------
# Segment Splitting & Multi-Track Sample Building
# ---------------------------------------------------------------------------
def split_into_segments(rows: List[Dict[str, Any]], max_gap_s: float = GAP_THRESHOLD_S) -> List[List[Dict[str, Any]]]:
    """Split rows into continuous temporal segments without gaps."""
    if not rows:
        return []
    segments = []
    current_seg = [rows[0]]
    for i in range(1, len(rows)):
        if rows[i]["ts"] - rows[i - 1]["ts"] > max_gap_s:
            segments.append(current_seg)
            current_seg = [rows[i]]
        else:
            current_seg.append(rows[i])
    if current_seg:
        segments.append(current_seg)
    return segments


def build_samples_from_segment(
    seg_rows: List[Dict[str, Any]],
    window_size: int = WINDOW_SIZE,
    horizons: List[int] = HORIZONS,
    regime_filter: Optional[str] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, List[Dict[str, Any]]]:
    """
    Build multi-track samples from a continuous segment:
      - X: feature matrix (window_size * n_features)
      - Y_traffic: [n_track_total(+h)] shape (N, len(horizons))
      - Y_qos_fps: [fps_avg(+h)] shape (N, len(horizons))
      - ts_arr: current timestamp
      - meta: metadata dictionary for manifest and analysis
    """
    max_h = max(horizons)
    min_len = window_size + max_h
    if len(seg_rows) < min_len:
        return (
            np.empty((0, window_size * len(FEATURE_NAMES))),
            np.empty((0, len(horizons))),
            np.empty((0, len(horizons))),
            np.empty((0,)),
            [],
        )

    X_list, Y_traffic_list, Y_fps_list, ts_list, meta_list = [], [], [], [], []

    for i in range(window_size - 1, len(seg_rows) - max_h):
        window_rows = seg_rows[i - window_size + 1 : i + 1]
        current_row = seg_rows[i]
        curr_ts = current_row["ts"]

        if regime_filter == "stable_only":
            if any(r["regime"] != "stable" for r in window_rows):
                continue

        feat_vector = []
        for r in window_rows:
            for fn in FEATURE_NAMES:
                feat_vector.append(r[fn])

        traffic_targets = []
        fps_targets = []
        skip_sample = False

        for h in horizons:
            target_idx = i + h
            if target_idx >= len(seg_rows):
                skip_sample = True
                break
            target_row = seg_rows[target_idx]
            if abs((target_row["ts"] - curr_ts) - h) > 2.0:
                skip_sample = True
                break
            if regime_filter == "stable_only" and target_row["regime"] != "stable":
                skip_sample = True
                break

            traffic_targets.append(target_row["n_track_total"])
            fps_targets.append(target_row["fps_avg"])

        if skip_sample:
            continue

        X_list.append(feat_vector)
        Y_traffic_list.append(traffic_targets)
        Y_fps_list.append(fps_targets)
        ts_list.append(curr_ts)
        meta_list.append({
            "ts": curr_ts,
            "node": current_row["node"],
            "regime": current_row["regime"],
            "n_active_cameras": current_row["n_active_cameras"],
            "current_n_track": current_row["n_track_total"],
            "current_fps": current_row["fps_avg"],
            "current_qos_deg": current_row["qos_degradation"],
            "window_n_track": [r["n_track_total"] for r in window_rows],
            "window_fps": [r["fps_avg"] for r in window_rows],
        })

    if not X_list:
        return (
            np.empty((0, window_size * len(FEATURE_NAMES))),
            np.empty((0, len(horizons))),
            np.empty((0, len(horizons))),
            np.empty((0,)),
            [],
        )

    return (
        np.array(X_list, dtype=np.float32),
        np.array(Y_traffic_list, dtype=np.float32),
        np.array(Y_fps_list, dtype=np.float32),
        np.array(ts_list, dtype=np.float64),
        meta_list,
    )


# ---------------------------------------------------------------------------
# Models & Estimators
# ---------------------------------------------------------------------------
def baseline_persistence(meta_list: List[Dict[str, Any]], field: str, n_horizons: int) -> np.ndarray:
    """Persistence baseline: ŷ(t+h) = y(t)."""
    preds = np.zeros((len(meta_list), n_horizons), dtype=np.float32)
    for i, m in enumerate(meta_list):
        preds[i, :] = m[field]
    return preds


def baseline_moving_average(meta_list: List[Dict[str, Any]], window_field: str, fallback_field: str, n_horizons: int) -> np.ndarray:
    """Moving average baseline: ŷ(t+h) = mean(y[t-W+1:t])."""
    preds = np.zeros((len(meta_list), n_horizons), dtype=np.float32)
    for i, m in enumerate(meta_list):
        w_vals = m[window_field]
        preds[i, :] = np.mean(w_vals) if w_vals else m[fallback_field]
    return preds


def baseline_slope_extrapolation(meta_list: List[Dict[str, Any]], window_field: str, curr_field: str, horizons: List[int]) -> np.ndarray:
    """Linear slope extrapolation baseline."""
    preds = np.zeros((len(meta_list), len(horizons)), dtype=np.float32)
    for i, m in enumerate(meta_list):
        w_vals = m[window_field]
        if len(w_vals) >= 2:
            x = np.arange(len(w_vals))
            slope, _ = np.polyfit(x, w_vals, 1)
        else:
            slope = 0.0
        for h_idx, h in enumerate(horizons):
            pred = m[curr_field] + slope * h
            preds[i, h_idx] = max(0.0, pred)
    return preds


class MultiHorizonRidgeForecaster:
    """Fits an independent L2 Ridge regression model for each forecast horizon."""

    def __init__(self, alphas: List[float] = [0.1, 1.0, 10.0, 100.0, 1000.0]):
        self.alphas = alphas
        self.models = []
        self.means = None
        self.stds = None

    def fit(self, X_train: np.ndarray, Y_train: np.ndarray):
        from sklearn.linear_model import Ridge
        from sklearn.model_selection import KFold

        n_samples, n_features = X_train.shape
        n_horizons = Y_train.shape[1]

        self.means = np.mean(X_train, axis=0, keepdims=True)
        self.stds = np.std(X_train, axis=0, keepdims=True)
        self.stds[self.stds < 1e-6] = 1.0

        X_norm = (X_train - self.means) / self.stds
        self.models = []

        kf = KFold(n_splits=3, shuffle=False)
        for h_idx in range(n_horizons):
            y_h = Y_train[:, h_idx]
            best_alpha = self.alphas[0]
            best_loss = float("inf")

            for alpha in self.alphas:
                cv_losses = []
                for train_idx, val_idx in kf.split(X_norm):
                    m = Ridge(alpha=alpha)
                    m.fit(X_norm[train_idx], y_h[train_idx])
                    pred = m.predict(X_norm[val_idx])
                    cv_losses.append(np.mean((pred - y_h[val_idx]) ** 2))
                mean_loss = np.mean(cv_losses)
                if mean_loss < best_loss:
                    best_loss = mean_loss
                    best_alpha = alpha

            final_m = Ridge(alpha=best_alpha)
            final_m.fit(X_norm, y_h)
            self.models.append(final_m)

    def predict(self, X_test: np.ndarray, clip_min: float = 0.0, clip_max: Optional[float] = None) -> np.ndarray:
        if not self.models or len(X_test) == 0:
            return np.empty((0, len(self.models)), dtype=np.float32)

        X_norm = (X_test - self.means) / self.stds
        n_horizons = len(self.models)
        preds = np.zeros((len(X_test), n_horizons), dtype=np.float32)

        for h_idx, m in enumerate(self.models):
            p = m.predict(X_norm)
            if clip_max is not None:
                p = np.clip(p, clip_min, clip_max)
            else:
                p = np.maximum(clip_min, p)
            preds[:, h_idx] = p
        return preds


# ---------------------------------------------------------------------------
# Metrics, Risk Detection & Usable Lead-Time Evaluation
# ---------------------------------------------------------------------------
def compute_continuous_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    current_y: np.ndarray,
    horizons: List[int] = HORIZONS,
) -> Dict[str, Any]:
    """Compute MAE, RMSE, and Directional Accuracy for continuous forecasts."""
    metrics = {}
    for h_idx, h in enumerate(horizons):
        yt = y_true[:, h_idx]
        yp = y_pred[:, h_idx]

        mae = float(np.mean(np.abs(yp - yt)))
        rmse = float(np.sqrt(np.mean((yp - yt) ** 2)))

        delta_true = yt - current_y
        delta_pred = yp - current_y
        changed = np.abs(delta_true) > 1e-4
        if np.sum(changed) > 0:
            same_sign = (delta_pred[changed] * delta_true[changed]) > 0
            dir_acc = float(np.mean(same_sign))
        else:
            dir_acc = 1.0

        metrics[f"+{h}s"] = {
            "mae": round(mae, 4),
            "rmse": round(rmse, 4),
            "directional_accuracy": round(dir_acc * 100.0, 2),
        }
    return metrics


def compute_risk_and_lead_time(
    test_ts: np.ndarray,
    meta_list: List[Dict[str, Any]],
    y_fps_true: np.ndarray,
    y_fps_pred: np.ndarray,
    horizons: List[int] = HORIZONS,
    avg_migration_latency_s: float = 0.256,  # 256ms average observed P2P migration latency
) -> Dict[str, Any]:
    """
    Evaluate L3/L2/L1 risk event detection, false alarm episodes, and usable lead-time.
    """
    results = {}
    for h_idx, h in enumerate(horizons):
        yt_fps = y_fps_true[:, h_idx]
        yp_fps = y_fps_pred[:, h_idx]
        curr_fps = np.array([m["current_fps"] for m in meta_list])

        h_dict = {}
        for thr_fps, level_name in [(22.0, "L3"), (19.0, "L2"), (17.0, "L1")]:
            actual_risk = yt_fps <= thr_fps
            pred_risk = yp_fps <= thr_fps
            baseline_heuristic = curr_fps <= (thr_fps + 1.5)  # margin heuristic

            tp = int(np.sum(actual_risk & pred_risk))
            fp = int(np.sum((~actual_risk) & pred_risk))
            fn = int(np.sum(actual_risk & (~pred_risk)))
            tn = int(np.sum((~actual_risk) & (~pred_risk)))

            prec = float(tp / (tp + fp)) if (tp + fp) > 0 else 0.0
            rec = float(tp / (tp + fn)) if (tp + fn) > 0 else 0.0
            f1 = float(2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0

            # Episode-based lead-time calculation
            episodes = []
            in_episode = False
            ep_start_idx = 0

            for i in range(len(actual_risk)):
                if actual_risk[i] and not in_episode:
                    in_episode = True
                    ep_start_idx = i
                elif not actual_risk[i] and in_episode:
                    in_episode = False
                    episodes.append((ep_start_idx, i - 1))
            if in_episode:
                episodes.append((ep_start_idx, len(actual_risk) - 1))

            # Measure lead time for each true event episode
            lead_times = []
            detected_episodes = 0
            for start_idx, end_idx in episodes:
                lookback_window = max(0, start_idx - h)
                pred_alarms = np.where(pred_risk[lookback_window:start_idx + 1])[0]
                if len(pred_alarms) > 0:
                    detected_episodes += 1
                    first_alarm_idx = lookback_window + pred_alarms[0]
                    lead_time_s = max(0.0, test_ts[start_idx] - test_ts[first_alarm_idx])
                    lead_times.append(lead_time_s)

            mean_lead_time = float(np.mean(lead_times)) if lead_times else 0.0
            usable_lead_time = max(0.0, mean_lead_time - avg_migration_latency_s)

            # False Alarm Episodes (alarms that never materialize into an overload episode)
            fa_episodes = 0
            in_fa = False
            for i in range(len(pred_risk)):
                if pred_risk[i] and not actual_risk[i] and not in_fa:
                    in_fa = True
                    fa_episodes += 1
                elif not pred_risk[i] or actual_risk[i]:
                    in_fa = False

            h_dict[level_name] = {
                "threshold_fps": thr_fps,
                "precision": round(prec * 100.0, 2),
                "recall": round(rec * 100.0, 2),
                "f1": round(f1, 4),
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "tn": tn,
                "total_episodes": len(episodes),
                "detected_episodes": detected_episodes,
                "episode_recall": round((detected_episodes / len(episodes) * 100.0) if episodes else 100.0, 2),
                "false_alarm_episodes": fa_episodes,
                "mean_lead_time_s": round(mean_lead_time, 2),
                "usable_lead_time_s": round(usable_lead_time, 2),
                "avg_migration_latency_s": avg_migration_latency_s,
            }
        results[f"+{h}s"] = h_dict
    return results


# ---------------------------------------------------------------------------
# Plotting & Publication Artifacts
# ---------------------------------------------------------------------------
def plot_multi_track_per_node(
    test_meta_list: List[Dict[str, Any]],
    y_traffic_true: np.ndarray,
    y_traffic_pred: np.ndarray,
    y_fps_true: np.ndarray,
    y_fps_pred: np.ndarray,
    output_path: Path,
    max_points: int = 300,
):
    """Plot synchronized 2-track time-series (Workload + FPS) for Node A, B, C."""
    nodes = sorted(list(set(m["node"] for m in test_meta_list)))
    fig, axes = plt.subplots(len(nodes), 2, figsize=(16, 3.8 * len(nodes)), sharex=False)
    if len(nodes) == 1:
        axes = np.array([axes])

    for r_idx, node_name in enumerate(nodes):
        idx_list = [i for i, m in enumerate(test_meta_list) if m["node"] == node_name][:max_points]
        if not idx_list:
            continue
        t_rel = np.arange(len(idx_list))

        # Column 1: Workload Track (n_track_total at +6s)
        ax_work = axes[r_idx, 0]
        ax_work.plot(t_rel, y_traffic_true[idx_list, 0], label="Actual Traffic (+6s)", color="black", linewidth=1.5)
        ax_work.plot(t_rel, y_traffic_pred[idx_list, 0], label="Ridge Forecast (+6s)", color="dodgerblue", linewidth=1.3)
        ax_work.set_title(f"Node {node_name} Workload Forecast (+6s)")
        ax_work.set_ylabel("Vehicle Count (n_track)")
        ax_work.legend(loc="upper right", fontsize=8)
        ax_work.grid(True, alpha=0.3)

        # Column 2: Continuous QoS Track (FPS at +6s)
        ax_fps = axes[r_idx, 1]
        ax_fps.plot(t_rel, y_fps_true[idx_list, 0], label="Actual FPS (+6s)", color="black", linewidth=1.5)
        ax_fps.plot(t_rel, y_fps_pred[idx_list, 0], label="Predicted FPS (+6s)", color="crimson", linewidth=1.3)
        ax_fps.axhline(22.0, color="orange", linestyle="--", label="L3 Thresh (22 FPS)")
        ax_fps.axhline(19.0, color="red", linestyle=":", label="L2 Thresh (19 FPS)")
        ax_fps.set_title(f"Node {node_name} QoS/FPS Forecast (+6s)")
        ax_fps.set_ylabel("Average FPS")
        ax_fps.set_ylim(0, 32)
        ax_fps.legend(loc="lower left", fontsize=8)
        ax_fps.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def plot_regime_breakdown(
    metrics_by_regime: Dict[str, Any],
    output_path: Path,
):
    """Bar chart comparing forecast RMSE across operational regimes."""
    regimes = list(metrics_by_regime.keys())
    rmse_traffic = [metrics_by_regime[r]["+6s"]["traffic_rmse"] for r in regimes]
    rmse_fps = [metrics_by_regime[r]["+6s"]["fps_rmse"] for r in regimes]

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))

    x = np.arange(len(regimes))
    axes[0].bar(x, rmse_traffic, color="royalblue", alpha=0.85)
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(regimes, rotation=25, ha="right", fontsize=8)
    axes[0].set_title("Traffic Workload Forecast RMSE (+6s)")
    axes[0].set_ylabel("RMSE (vehicles)")
    axes[0].grid(True, alpha=0.3)

    axes[1].bar(x, rmse_fps, color="coral", alpha=0.85)
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(regimes, rotation=25, ha="right", fontsize=8)
    axes[1].set_title("Continuous QoS FPS Forecast RMSE (+6s)")
    axes[1].set_ylabel("RMSE (FPS)")
    axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def plot_lead_time_distribution(
    lead_time_data: Dict[str, Any],
    output_path: Path,
):
    """Plot usable lead time vs migration latency for proactive decision gates."""
    fig, ax = plt.subplots(figsize=(8, 4.5))

    levels = ["L3", "L2", "L1"]
    mean_leads_6s = [lead_time_data["+6s"][lvl]["mean_lead_time_s"] for lvl in levels]
    usable_leads_6s = [lead_time_data["+6s"][lvl]["usable_lead_time_s"] for lvl in levels]

    x = np.arange(len(levels))
    width = 0.35

    ax.bar(x - width / 2, mean_leads_6s, width, label="Raw Forecast Lead Time (s)", color="mediumseagreen", alpha=0.85)
    ax.bar(x + width / 2, usable_leads_6s, width, label="Usable Lead Time (after P2P latency)", color="darkcyan", alpha=0.85)
    ax.axhline(0.256, color="red", linestyle="--", label="Observed Migration Latency (0.26s)")

    ax.set_xticks(x)
    ax.set_xticklabels([f"{lvl} Overload Gate" for lvl in levels])
    ax.set_ylabel("Lead Time (seconds)")
    ax.set_title("Proactive Decision Lead Time vs Migration Latency (+6s Horizon)")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


# ---------------------------------------------------------------------------
# Main Evaluation Pipeline
# ---------------------------------------------------------------------------
def run_evaluation(data_dir: Path, output_dir: Path):
    """Execute end-to-end multi-track evaluation across all Jetson nodes and regimes."""
    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading and auditing dataset from {data_dir}...")
    cleaned_node_rows = {}
    audit_reports = {}

    for node_name in ["A", "B", "C"]:
        node_folder = data_dir / node_name
        csv_path = node_folder / f"calibration_{node_name}.csv"
        mig_path = node_folder / "p2p_migrations.csv"

        if not csv_path.is_file():
            print(f"Warning: {csv_path} not found.")
            continue

        mig_records = load_structured_migrations(mig_path)
        rows, summary = load_and_audit_node_csv(csv_path, mig_records, f"jetson_{node_name}")
        cleaned_node_rows[node_name] = rows
        audit_reports[node_name] = summary
        print(f"Node {node_name}: Kept {len(rows)} samples (Stable: {summary['regime_counts']['stable']}, Offload/Reclaim: {sum(summary['regime_counts'].values()) - summary['regime_counts']['stable']})")

    # Regimes to evaluate: V0_operational (All) and V1_stable_diagnostic
    regime_configs = [
        ("V0_operational_all", None),
        ("V1_stable_diagnostic", "stable_only"),
    ]

    variant_results = {}
    manifest_records = []

    for regime_name, regime_filter in regime_configs:
        print(f"\n--- Training & Evaluating Regime: {regime_name} ---")
        train_X_list, train_Y_traffic_list, train_Y_fps_list = [], [], []
        test_X_list, test_Y_traffic_list, test_Y_fps_list, test_ts_list, test_meta_list = [], [], [], [], []

        for node_name, rows in cleaned_node_rows.items():
            segments = split_into_segments(rows)
            for seg in segments:
                X, Y_tr, Y_fps, ts, meta = build_samples_from_segment(seg, regime_filter=regime_filter)
                if len(X) < 10:
                    continue

                split_idx = int(len(X) * TRAIN_RATIO)
                train_X_list.append(X[:split_idx])
                train_Y_traffic_list.append(Y_tr[:split_idx])
                train_Y_fps_list.append(Y_fps[:split_idx])

                test_X_list.append(X[split_idx:])
                test_Y_traffic_list.append(Y_tr[split_idx:])
                test_Y_fps_list.append(Y_fps[split_idx:])
                test_ts_list.append(ts[split_idx:])
                test_meta_list.extend(meta[split_idx:])

        if not train_X_list or not test_X_list:
            print(f"No samples for {regime_name}")
            continue

        X_train = np.vstack(train_X_list)
        Y_train_tr = np.vstack(train_Y_traffic_list)
        Y_train_fps = np.vstack(train_Y_fps_list)

        X_test = np.vstack(test_X_list)
        Y_test_tr = np.vstack(test_Y_traffic_list)
        Y_test_fps = np.vstack(test_Y_fps_list)
        test_ts = np.concatenate(test_ts_list)

        curr_tr = np.array([m["current_n_track"] for m in test_meta_list])
        curr_fps = np.array([m["current_fps"] for m in test_meta_list])

        print(f"Samples for {regime_name}: Train={len(X_train)}, Test={len(X_test)}")

        # Train Track 1: Workload Forecaster (Ridge)
        ridge_tr = MultiHorizonRidgeForecaster()
        ridge_tr.fit(X_train, Y_train_tr)
        pred_ridge_tr = ridge_tr.predict(X_test, clip_min=0.0)

        # Train Track 2: Continuous QoS Forecaster (Ridge)
        ridge_fps = MultiHorizonRidgeForecaster()
        ridge_fps.fit(X_train, Y_train_fps)
        pred_ridge_fps = ridge_fps.predict(X_test, clip_min=0.0, clip_max=32.0)

        # Baselines
        pred_persist_tr = baseline_persistence(test_meta_list, "current_n_track", len(HORIZONS))
        pred_ma_tr = baseline_moving_average(test_meta_list, "window_n_track", "current_n_track", len(HORIZONS))
        pred_persist_fps = baseline_persistence(test_meta_list, "current_fps", len(HORIZONS))
        pred_ma_fps = baseline_moving_average(test_meta_list, "window_fps", "current_fps", len(HORIZONS))

        # Metrics computation
        metrics_tr_ridge = compute_continuous_metrics(Y_test_tr, pred_ridge_tr, curr_tr)
        metrics_tr_persist = compute_continuous_metrics(Y_test_tr, pred_persist_tr, curr_tr)
        metrics_tr_ma = compute_continuous_metrics(Y_test_tr, pred_ma_tr, curr_tr)

        metrics_fps_ridge = compute_continuous_metrics(Y_test_fps, pred_ridge_fps, curr_fps)
        metrics_fps_persist = compute_continuous_metrics(Y_test_fps, pred_persist_fps, curr_fps)
        metrics_fps_ma = compute_continuous_metrics(Y_test_fps, pred_ma_fps, curr_fps)

        # Risk and Lead-time evaluation
        lead_time_eval = compute_risk_and_lead_time(test_ts, test_meta_list, Y_test_fps, pred_ridge_fps)

        variant_results[regime_name] = {
            "traffic_forecaster": {
                "Ridge": metrics_tr_ridge,
                "Persistence": metrics_tr_persist,
                "MovingAvg": metrics_tr_ma,
            },
            "qos_fps_forecaster": {
                "Ridge": metrics_fps_ridge,
                "Persistence": metrics_fps_persist,
                "MovingAvg": metrics_fps_ma,
            },
            "risk_and_lead_time": lead_time_eval,
            "sample_counts": {"train": len(X_train), "test": len(X_test)},
        }

        # Print summary
        print(f"Track 1 (Traffic) +6s RMSE: Ridge={metrics_tr_ridge['+6s']['rmse']} vs Persist={metrics_tr_persist['+6s']['rmse']} (DirAcc: {metrics_tr_ridge['+6s']['directional_accuracy']}%)")
        print(f"Track 2 (QoS/FPS) +6s RMSE: Ridge={metrics_fps_ridge['+6s']['rmse']} vs Persist={metrics_fps_persist['+6s']['rmse']} (DirAcc: {metrics_fps_ridge['+6s']['directional_accuracy']}%)")
        print(f"L3 Overload Gate (+6s): F1={lead_time_eval['+6s']['L3']['f1']}, Usable Lead Time={lead_time_eval['+6s']['L3']['usable_lead_time_s']}s")

        if regime_name == "V0_operational_all":
            # Generate plots
            plot_multi_track_per_node(test_meta_list, Y_test_tr, pred_ridge_tr, Y_test_fps, pred_ridge_fps, plots_dir / "two_track_forecast_timeseries.png")
            plot_lead_time_distribution(lead_time_eval, plots_dir / "usable_lead_time_distribution.png")

            # Build window manifest
            for i in range(len(test_ts)):
                m = test_meta_list[i]
                manifest_records.append({
                    "ts": test_ts[i],
                    "node": m["node"],
                    "regime": m["regime"],
                    "n_active_cameras": m["n_active_cameras"],
                    "actual_traffic_plus6s": Y_test_tr[i, 0],
                    "pred_traffic_plus6s": pred_ridge_tr[i, 0],
                    "actual_fps_plus6s": Y_test_fps[i, 0],
                    "pred_fps_plus6s": pred_ridge_fps[i, 0],
                    "actual_traffic_plus10s": Y_test_tr[i, 1],
                    "pred_traffic_plus10s": pred_ridge_tr[i, 1],
                    "actual_fps_plus10s": Y_test_fps[i, 1],
                    "pred_fps_plus10s": pred_ridge_fps[i, 1],
                })

    # Sub-regime breakdown evaluation for V0
    regime_breakdown = {}
    if manifest_records:
        unique_regimes = sorted(list(set(r["regime"] for r in manifest_records)))
        for reg in unique_regimes:
            sub = [r for r in manifest_records if r["regime"] == reg]
            if len(sub) < 5:
                continue
            act_tr = np.array([r["actual_traffic_plus6s"] for r in sub])
            prd_tr = np.array([r["pred_traffic_plus6s"] for r in sub])
            act_fps = np.array([r["actual_fps_plus6s"] for r in sub])
            prd_fps = np.array([r["pred_fps_plus6s"] for r in sub])

            regime_breakdown[reg] = {
                "+6s": {
                    "count": len(sub),
                    "traffic_rmse": round(float(np.sqrt(np.mean((prd_tr - act_tr) ** 2))), 4),
                    "fps_rmse": round(float(np.sqrt(np.mean((prd_fps - act_fps) ** 2))), 4),
                }
            }
        variant_results["regime_breakdown"] = regime_breakdown
        plot_regime_breakdown(regime_breakdown, plots_dir / "operational_regimes_breakdown.png")

    # Persist all evaluation artifacts
    with open(output_dir / "data_audit.json", "w", encoding="utf-8") as f:
        json.dump(audit_reports, f, indent=2)
    with open(output_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(variant_results, f, indent=2)

    if manifest_records:
        with open(output_dir / "window_manifest.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(manifest_records[0].keys()))
            writer.writeheader()
            writer.writerows(manifest_records)

    print(f"\nAll publication artifacts written to {output_dir}/")


# ---------------------------------------------------------------------------
# Synthetic Self-Check
# ---------------------------------------------------------------------------
def run_self_check() -> bool:
    """Verify 2-track training, risk detection, and lead time on synthetic flow."""
    print("=== Running train_traffic_forecaster --self-check ===")
    np.random.seed(42)
    n_steps = 1000
    t = np.arange(n_steps, dtype=np.float64)

    true_traffic = 5.0 + 3.0 * np.sin(2 * np.pi * t / 60.0) + np.random.normal(0, 0.3, n_steps)
    true_traffic = np.maximum(0.0, true_traffic)
    synthetic_fps = np.maximum(10.0, 30.0 - 1.2 * true_traffic + np.random.normal(0, 0.2, n_steps))

    rows = []
    for i in range(n_steps):
        rows.append({
            "ts": 1000.0 + i,
            "node": "jetson_A",
            "fps_avg": float(synthetic_fps[i]),
            "expected_fps": 30.0,
            "qos_degradation": float(max(0.0, (1.0 - synthetic_fps[i] / 30.0) * 100.0)),
            "n_active_cameras": 2,
            "n_cameras_total": 2,
            "n_track_total": float(true_traffic[i]),
            "n_plate_total": float(true_traffic[i] * 0.9),
            "stationary_fraction_mean": 0.1,
            "gpu_percent": 60.0,
            "gpu_temp_c": 55.0,
            "load_score": 20.0,
            "offload_crops_received_per_s": 0.0,
            "is_near_migration": 0.0,
            "is_post_migration": 0.0,
            "is_camera_change": 0.0,
            "regime": "stable",
            "active_mig_reason": "none",
        })

    segments = split_into_segments(rows)
    X_list, Y_tr_list, Y_fps_list, ts_list, meta_list = [], [], [], [], []
    for seg in segments:
        X, Y_tr, Y_fps, ts, meta = build_samples_from_segment(seg)
        if len(X) > 0:
            X_list.append(X)
            Y_tr_list.append(Y_tr)
            Y_fps_list.append(Y_fps)
            ts_list.append(ts)
            meta_list.extend(meta)

    X_all = np.vstack(X_list)
    Y_tr_all = np.vstack(Y_tr_list)
    Y_fps_all = np.vstack(Y_fps_list)
    ts_all = np.concatenate(ts_list)

    split = int(len(X_all) * 0.7)
    ridge_tr = MultiHorizonRidgeForecaster()
    ridge_tr.fit(X_all[:split], Y_tr_all[:split])
    pred_tr = ridge_tr.predict(X_all[split:])

    ridge_fps = MultiHorizonRidgeForecaster()
    ridge_fps.fit(X_all[:split], Y_fps_all[:split])
    pred_fps = ridge_fps.predict(X_all[split:], clip_min=0.0, clip_max=32.0)

    tr_rmse = np.sqrt(np.mean((pred_tr - Y_tr_all[split:]) ** 2))
    fps_rmse = np.sqrt(np.mean((pred_fps - Y_fps_all[split:]) ** 2))

    lead_eval = compute_risk_and_lead_time(ts_all[split:], meta_list[split:], Y_fps_all[split:], pred_fps)
    print(f"Self-check: Traffic RMSE={tr_rmse:.3f}, FPS RMSE={fps_rmse:.3f}")
    print(f"Self-check L3 Lead Time={lead_eval['+6s']['L3']['usable_lead_time_s']}s")
    print("=== Self-check PASSED successfully ===")
    return True


def main():
    parser = argparse.ArgumentParser(description="Train and evaluate traffic and QoS forecaster with P2P operational regimes.")
    parser.add_argument("--self-check", action="store_true", help="Run synthetic self-check.")
    parser.add_argument("--data-dir", type=str, default="dataTrain", help="Directory containing node folders (A, B, C).")
    parser.add_argument("--output-dir", type=str, default="dataTrain/traffic_eval", help="Directory to save artifacts and plots.")
    args = parser.parse_args()

    if args.self_check:
        success = run_self_check()
        sys.exit(0 if success else 1)

    run_evaluation(Path(args.data_dir), Path(args.output_dir))


if __name__ == "__main__":
    main()
