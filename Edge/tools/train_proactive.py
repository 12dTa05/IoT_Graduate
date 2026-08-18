#!/usr/bin/env python3
"""Train and evaluate proactive Edge Load forecasting and threshold risk classification models.

Temporary offline evaluation script for proactive edge load prediction and threshold risk models.
Does not export ONNX or deploy to edge runtime.

Tracks:
1. Load Regression (Primary): Persistence baseline vs. Ridge delta-load regression across horizons (+6, +10).
2. Load Threshold Risk Classification (Primary): Majority baseline vs. balanced Logistic Regression for horizon-specific risk
   across load thresholds L3=57, L2=65, L1=75 (any actual_load_score >= threshold in [t+1, t+h]).
3. Auxiliary FPS Regression & FPS-based risk diagnostics.

Outputs:
- metrics.json: Overall and per-node metrics per horizon and threshold, including event diagnostics.
- predictions.csv: Out-of-sample test predictions.
- data_summary.json: Data cleaning, segmenting, splitting, target sources, and configuration metadata.
- Plots:
    - load_forecast_timeseries.png
    - load_forecast_scatter.png
    - load_threshold_events.png
    - load_risk_confusion.png
    - fps_forecast_timeseries.png (auxiliary)
    - fps_forecast_scatter.png (auxiliary)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    mean_absolute_error,
    precision_recall_curve,
    precision_score,
    recall_score,
    root_mean_squared_error,
)

BASE_FEATURE_COLS = [
    "n_active_cameras",
    "n_track_total",
    "n_plate_total",
    "stationary_fraction_mean",
    "fps_avg",
    "gpu_percent",
    "cpu_percent",
    "ram_percent",
    "gpu_temp_c",
    "offload_crops_received_per_s",
]

# Explicit model feature list: 10 historical telemetry features + historical load score
MODEL_FEATURE_COLS = BASE_FEATURE_COLS + ["actual_load_score"]

TARGET_FPS = 27.0
THRESHOLDS = {
    "L3_57": 57.0,
    "L2_65": 65.0,
    "L1_75": 75.0,
}

RIDGE_ALPHAS = [0.0, 0.1, 1.0, 10.0, 100.0]
LOGISTIC_CS = [0.01, 0.1, 1.0, 10.0]


def convert_fps_to_load_score(fps: float | np.ndarray) -> float | np.ndarray:
    """Explicit load-score conversion matching Edge/health_agent.py anchors:

    FPS >= 27 -> 0; 22 -> 57; 19 -> 65; 17 -> 75; <= 0 -> 100 with piecewise linear interpolation.
    """
    is_scalar = np.isscalar(fps)
    f = np.asarray(fps, dtype=np.float64)
    c = np.clip(f, 0.0, TARGET_FPS)
    score = np.zeros_like(c)

    m1 = c >= 22.0
    m2 = (c >= 19.0) & (~m1)
    m3 = (c >= 17.0) & (~m1) & (~m2)
    m4 = c < 17.0

    score[m1] = 57.0 * (TARGET_FPS - c[m1]) / (TARGET_FPS - 22.0)
    score[m2] = 57.0 + (65.0 - 57.0) * (22.0 - c[m2]) / (22.0 - 19.0)
    score[m3] = 65.0 + (75.0 - 65.0) * (19.0 - c[m3]) / (19.0 - 17.0)
    score[m4] = 75.0 + (100.0 - 75.0) * (17.0 - c[m4]) / 17.0

    return float(np.round(score.item(), 1)) if is_scalar else np.round(score, 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Proactive offline load prediction and risk evaluation script")
    parser.add_argument("--input-dir", type=Path, default=Path("dataTrain"), help="Input directory containing calibration CSVs")
    parser.add_argument("--output-dir", type=Path, default=Path("dataTrain/proactive_eval"), help="Output directory for results and plots")
    parser.add_argument("--history", type=int, default=5, help="Number of historical time steps (default: 5)")
    parser.add_argument("--horizons", type=int, nargs="+", default=[6, 10], help="Forecast horizons in seconds (default: 6 10)")
    parser.add_argument("--fps-threshold", type=float, default=22.0, help="Auxiliary FPS risk threshold (default: 22.0)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--self-check", action="store_true", help="Run self-check on synthetic data")
    return parser.parse_args()


def discover_calibration_csvs(input_dir: Path) -> list[tuple[Path, str]]:
    discovered = []
    for p in sorted(input_dir.rglob("calibration_*.csv")):
        if not p.is_file():
            continue
        node_name = p.parent.name if len(p.parent.name) <= 10 else p.stem.replace("calibration_", "")
        discovered.append((p, node_name))
    return discovered


def clean_and_segment_csv(csv_path: Path, base_features: list[str]) -> tuple[list[pd.DataFrame], dict[str, Any]]:
    stats: dict[str, Any] = {
        "raw_rows": 0,
        "rows_after_dedup": 0,
        "rows_after_tail_trim": 0,
        "rows_after_inactive_filter": 0,
        "ts_gaps_found": 0,
        "segments_created": 0,
        "target_source": "load_score_from_fps",
        "recorded_load_score_count": 0,
        "converted_load_score_count": 0,
    }
    df = pd.read_csv(csv_path)
    stats["raw_rows"] = len(df)
    if df.empty:
        return [], stats

    cols = ["ts"] + list(base_features)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"CSV {csv_path} missing required columns: {missing}")

    has_recorded = "load_score" in df.columns
    load_cols = cols + (["load_score"] if has_recorded else [])
    df = df[load_cols].copy()

    for c in load_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=cols)

    # Compute audit conversion and primary actual_load_score
    fps_arr = df["fps_avg"].to_numpy(dtype=np.float64)
    conv_load = convert_fps_to_load_score(fps_arr)
    df["load_score_from_fps"] = conv_load

    if has_recorded:
        rec_load = df["load_score"].to_numpy(dtype=np.float64)
        is_finite_rec = np.isfinite(rec_load)
        df["actual_load_score"] = np.where(is_finite_rec, rec_load, conv_load)
        stats["recorded_load_score_count"] = int(np.sum(is_finite_rec))
        stats["converted_load_score_count"] = int(np.sum(~is_finite_rec))
        if stats["converted_load_score_count"] == 0:
            stats["target_source"] = "recorded_load_score"
        else:
            stats["target_source"] = "hybrid_recorded_and_converted"
    else:
        df["actual_load_score"] = conv_load
        stats["target_source"] = "load_score_from_fps"
        stats["converted_load_score_count"] = len(df)

    # Sort by timestamp, drop duplicates or non-monotonic
    df = df.sort_values(by="ts", kind="mergesort")
    df = df.drop_duplicates(subset=["ts"], keep="last")
    stats["rows_after_dedup"] = len(df)
    if df.empty:
        return [], stats

    # Remove tail after last n_active_cameras > 0
    active_idx = np.where(df["n_active_cameras"].to_numpy() > 0)[0]
    if len(active_idx) == 0:
        stats["rows_after_tail_trim"] = 0
        stats["rows_after_inactive_filter"] = 0
        return [], stats

    last_active = active_idx[-1]
    df = df.iloc[: last_active + 1].copy()
    stats["rows_after_tail_trim"] = len(df)

    # Remove inactive rows elsewhere (n_active_cameras <= 0)
    df = df[df["n_active_cameras"] > 0].copy()
    stats["rows_after_inactive_filter"] = len(df)
    if df.empty:
        return [], stats

    # Segment at ts gaps > 2.0s
    ts_vals = df["ts"].to_numpy()
    gaps = np.diff(ts_vals) > 2.0
    stats["ts_gaps_found"] = int(np.sum(gaps))

    split_indices = np.where(gaps)[0] + 1
    segments = []
    prev = 0
    for idx in split_indices:
        seg = df.iloc[prev:idx].copy()
        if len(seg) > 0:
            segments.append(seg)
        prev = idx
    if prev < len(df):
        segments.append(df.iloc[prev:].copy())

    stats["segments_created"] = len(segments)
    return segments, stats


def build_windows_for_segment(
    seg_df: pd.DataFrame,
    history: int,
    horizons: list[int],
    thresholds: dict[str, float],
    fps_thresh: float,
    node_name: str,
    segment_id: str | int = 0,
) -> tuple[dict[str, np.ndarray], list[pd.DataFrame]]:
    """Builds sliding windows chronologically within train/val/test splits.

    Target definitions:
    - Primary Load regression: target is actual_load_score at exact future step t + h (index i + history - 1 + h).
    - Primary Load Risk classification: for each threshold and horizon h, target is 1.0 if ANY future row in the next h steps
      (slice i + history : i + history + h, representing future steps t+1 through t+h)
      has actual_load_score >= threshold, else 0.0.
    - Auxiliary FPS targets: fps_avg at t + h and fps_avg <= fps_thresh in [t+1, t+h].
    """
    max_h = max(horizons)
    min_required_len = history + max_h
    n = len(seg_df)
    if n < min_required_len:
        return {}, []

    # 70/15/15 chronological split indices on raw segment rows
    n_train = int(n * 0.70)
    n_val = int(n * 0.15)
    n_test = n - n_train - n_val

    splits_raw = {
        "train": seg_df.iloc[:n_train],
        "val": seg_df.iloc[n_train : n_train + n_val],
        "test": seg_df.iloc[n_train + n_val :],
    }

    split_arrays = {}
    split_meta_dfs = []

    for split_name, s_df in splits_raw.items():
        s_len = len(s_df)
        if s_len < min_required_len:
            continue

        feat_vals = s_df[MODEL_FEATURE_COLS].to_numpy(dtype=np.float64)
        load_vals = s_df["actual_load_score"].to_numpy(dtype=np.float64)
        fps_vals = s_df["fps_avg"].to_numpy(dtype=np.float64)
        ts_vals = s_df["ts"].to_numpy(dtype=np.float64)

        num_windows = s_len - history - max_h + 1
        x_hist_list = []
        y_load_list = []
        y_fps_list = []
        y_risk_dict = {th_name: [] for th_name in thresholds}
        y_aux_fps_risk_list = []
        meta_records = []

        for i in range(num_windows):
            hist_feat = feat_vals[i : i + history]  # shape: (history, n_features)
            cur_load = load_vals[i + history - 1]
            cur_fps = fps_vals[i + history - 1]
            cur_ts = ts_vals[i + history - 1]

            # Future exact horizon targets
            fut_load_at_h = [load_vals[i + history - 1 + h] for h in horizons]
            fut_fps_at_h = [fps_vals[i + history - 1 + h] for h in horizons]

            rec = {
                "split": split_name,
                "node": node_name,
                "segment_id": segment_id,
                "ts": cur_ts,
                "current_load_score": cur_load,
                "current_fps": cur_fps,
            }

            for h_idx, h in enumerate(horizons):
                rec[f"true_load_h{h}"] = fut_load_at_h[h_idx]
                rec[f"persistence_load_h{h}"] = cur_load
                rec[f"true_fps_h{h}"] = fut_fps_at_h[h_idx]
                rec[f"persistence_fps_h{h}"] = cur_fps

                # Load risk targets in [t+1, t+h]
                sub_load_slice = load_vals[i + history : i + history + h]
                for th_name, th_val in thresholds.items():
                    r_val = 1.0 if np.any(sub_load_slice >= th_val) else 0.0
                    rec[f"true_risk_{th_name}_h{h}"] = r_val

                # Auxiliary FPS risk target
                sub_fps_slice = fps_vals[i + history : i + history + h]
                rec[f"aux_true_risk_fps_h{h}"] = 1.0 if np.any(sub_fps_slice <= fps_thresh) else 0.0

            x_hist_list.append(hist_feat)
            y_load_list.append(fut_load_at_h)
            y_fps_list.append(fut_fps_at_h)

            for th_name in thresholds:
                th_risks = [rec[f"true_risk_{th_name}_h{h}"] for h in horizons]
                y_risk_dict[th_name].append(th_risks)

            y_aux_fps_risk_list.append([rec[f"aux_true_risk_fps_h{h}"] for h in horizons])
            meta_records.append(rec)

        split_arrays[split_name] = {
            "X": np.array(x_hist_list, dtype=np.float64),  # (num_windows, history, n_feat)
            "y_load": np.array(y_load_list, dtype=np.float64),  # (num_windows, n_horizons)
            "y_fps": np.array(y_fps_list, dtype=np.float64),
            "y_risk_by_threshold": {
                th_name: np.array(y_risk_dict[th_name], dtype=np.float64) for th_name in thresholds
            },
            "y_aux_fps_risk": np.array(y_aux_fps_risk_list, dtype=np.float64),
            "cur_load": np.array([r["current_load_score"] for r in meta_records], dtype=np.float64),
            "cur_fps": np.array([r["current_fps"] for r in meta_records], dtype=np.float64),
        }
        split_meta_dfs.append(pd.DataFrame(meta_records))

    return split_arrays, split_meta_dfs


def fit_and_eval_models(
    splits_data: dict[str, dict[str, Any]],
    test_meta_df: pd.DataFrame,
    horizons: list[int],
    thresholds: dict[str, float],
    fps_threshold: float,
) -> tuple[dict[str, Any], pd.DataFrame, dict[str, Any]]:
    X_train_raw = splits_data["train"]["X"]  # (N_tr, history, n_feat)
    y_train_load = splits_data["train"]["y_load"]
    y_train_fps = splits_data["train"]["y_fps"]
    cur_load_tr = splits_data["train"]["cur_load"]
    cur_fps_tr = splits_data["train"]["cur_fps"]

    X_val_raw = splits_data["val"]["X"]
    y_val_load = splits_data["val"]["y_load"]
    y_val_fps = splits_data["val"]["y_fps"]
    cur_load_val = splits_data["val"]["cur_load"]
    cur_fps_val = splits_data["val"]["cur_fps"]

    X_test_raw = splits_data["test"]["X"]
    y_test_load = splits_data["test"]["y_load"]
    y_test_fps = splits_data["test"]["y_fps"]
    cur_load_test = splits_data["test"]["cur_load"]
    cur_fps_test = splits_data["test"]["cur_fps"]

    # Normalization fitted on Train only (no leakage)
    N_tr = X_train_raw.shape[0]
    X_tr_flat = X_train_raw.reshape(N_tr, -1)
    X_val_flat = X_val_raw.reshape(X_val_raw.shape[0], -1)
    X_test_flat = X_test_raw.reshape(X_test_raw.shape[0], -1)

    feat_mean = np.mean(X_tr_flat, axis=0, keepdims=True)
    feat_std = np.std(X_tr_flat, axis=0, keepdims=True)
    feat_std[feat_std < 1e-6] = 1.0

    X_tr_norm = (X_tr_flat - feat_mean) / feat_std
    X_val_norm = (X_val_flat - feat_mean) / feat_std
    X_test_norm = (X_test_flat - feat_mean) / feat_std

    # Track 1 Primary: Load Regression (Delta-Load = Target_Load - Current_Load)
    selected_load_alphas = []
    pred_test_load_ridge = np.zeros_like(y_test_load)

    for h_idx, h in enumerate(horizons):
        y_tr_delta = y_train_load[:, h_idx] - cur_load_tr
        y_val_delta = y_val_load[:, h_idx] - cur_load_val
        y_val_actual = y_val_load[:, h_idx]

        best_alpha = RIDGE_ALPHAS[0]
        best_val_rmse = float("inf")
        best_model = None

        for alpha in RIDGE_ALPHAS:
            model = Ridge(alpha=alpha)
            model.fit(X_tr_norm, y_tr_delta)
            val_delta_pred = model.predict(X_val_norm)
            val_load_pred = np.clip(cur_load_val + val_delta_pred, 0.0, 100.0)
            rmse = root_mean_squared_error(y_val_actual, val_load_pred)
            if rmse < best_val_rmse:
                best_val_rmse = rmse
                best_alpha = alpha
                best_model = model

        selected_load_alphas.append(best_alpha)
        delta_test_pred = best_model.predict(X_test_norm)
        pred_test_load_ridge[:, h_idx] = np.clip(cur_load_test + delta_test_pred, 0.0, 100.0)

    # Auxiliary FPS Regression
    selected_fps_alphas = []
    pred_test_fps_ridge = np.zeros_like(y_test_fps)

    for h_idx, h in enumerate(horizons):
        y_tr_delta_fps = y_train_fps[:, h_idx] - cur_fps_tr
        y_val_actual_fps = y_val_fps[:, h_idx]

        best_alpha_fps = RIDGE_ALPHAS[0]
        best_val_rmse_fps = float("inf")
        best_model_fps = None

        for alpha in RIDGE_ALPHAS:
            model_fps = Ridge(alpha=alpha)
            model_fps.fit(X_tr_norm, y_tr_delta_fps)
            val_delta_fps = model_fps.predict(X_val_norm)
            val_fps_pred = cur_fps_val + val_delta_fps
            rmse_fps = root_mean_squared_error(y_val_actual_fps, val_fps_pred)
            if rmse_fps < best_val_rmse_fps:
                best_val_rmse_fps = rmse_fps
                best_alpha_fps = alpha
                best_model_fps = model_fps

        selected_fps_alphas.append(best_alpha_fps)
        delta_test_fps = best_model_fps.predict(X_test_norm)
        pred_test_fps_ridge[:, h_idx] = cur_fps_test + delta_test_fps

    # Track 2 Primary: Load Threshold Risk Classification per Threshold and Horizon
    selected_logreg_cs: dict[str, dict[str, float]] = {th: {} for th in thresholds}
    pred_test_risk_maj: dict[str, np.ndarray] = {th: np.zeros((X_test_norm.shape[0], len(horizons))) for th in thresholds}
    pred_test_risk_prob: dict[str, np.ndarray] = {th: np.zeros((X_test_norm.shape[0], len(horizons))) for th in thresholds}
    pred_test_risk_class: dict[str, np.ndarray] = {th: np.zeros((X_test_norm.shape[0], len(horizons))) for th in thresholds}

    for th_name in thresholds:
        y_tr_risk_th = splits_data["train"]["y_risk_by_threshold"][th_name]
        y_val_risk_th = splits_data["val"]["y_risk_by_threshold"][th_name]
        y_test_risk_th = splits_data["test"]["y_risk_by_threshold"][th_name]

        for h_idx, h in enumerate(horizons):
            y_tr_r = y_tr_risk_th[:, h_idx]
            y_val_r = y_val_risk_th[:, h_idx]
            y_test_r = y_test_risk_th[:, h_idx]

            # Majority class baseline
            maj_class = 1.0 if np.mean(y_tr_r) >= 0.5 else 0.0
            pred_test_risk_maj[th_name][:, h_idx] = maj_class

            unique_tr_classes = np.unique(y_tr_r)
            if len(unique_tr_classes) > 1:
                best_val_f1 = -1.0
                best_c = LOGISTIC_CS[0]
                best_clf = None

                for c in LOGISTIC_CS:
                    clf = LogisticRegression(C=c, class_weight="balanced", max_iter=1000, random_state=42)
                    clf.fit(X_tr_norm, y_tr_r)
                    val_prob = clf.predict_proba(X_val_norm)[:, 1]
                    val_pred = (val_prob >= 0.5).astype(float)
                    f1 = f1_score(y_val_r, val_pred, zero_division=0)
                    if f1 > best_val_f1:
                        best_val_f1 = f1
                        best_c = c
                        best_clf = clf

                selected_logreg_cs[th_name][f"h{h}"] = float(best_c)
                test_prob = best_clf.predict_proba(X_test_norm)[:, 1]
                pred_test_risk_prob[th_name][:, h_idx] = test_prob
                pred_test_risk_class[th_name][:, h_idx] = (test_prob >= 0.5).astype(float)
            else:
                selected_logreg_cs[th_name][f"h{h}"] = 1.0
                single_val = float(unique_tr_classes[0])
                pred_test_risk_prob[th_name][:, h_idx] = single_val
                pred_test_risk_class[th_name][:, h_idx] = single_val

    # Auxiliary FPS Risk Classification
    pred_test_aux_fps_risk_prob = np.zeros((X_test_norm.shape[0], len(horizons)))
    pred_test_aux_fps_risk_class = np.zeros((X_test_norm.shape[0], len(horizons)))
    pred_test_aux_fps_risk_maj = np.zeros((X_test_norm.shape[0], len(horizons)))

    for h_idx, h in enumerate(horizons):
        y_tr_fps_r = splits_data["train"]["y_aux_fps_risk"][:, h_idx]
        y_val_fps_r = splits_data["val"]["y_aux_fps_risk"][:, h_idx]
        maj_fps_r = 1.0 if np.mean(y_tr_fps_r) >= 0.5 else 0.0
        pred_test_aux_fps_risk_maj[:, h_idx] = maj_fps_r

        if len(np.unique(y_tr_fps_r)) > 1:
            clf_fps = LogisticRegression(C=1.0, class_weight="balanced", max_iter=1000, random_state=42)
            clf_fps.fit(X_tr_norm, y_tr_fps_r)
            test_fps_p = clf_fps.predict_proba(X_test_norm)[:, 1]
            pred_test_aux_fps_risk_prob[:, h_idx] = test_fps_p
            pred_test_aux_fps_risk_class[:, h_idx] = (test_fps_p >= 0.5).astype(float)
        else:
            s_val = float(y_tr_fps_r[0]) if len(y_tr_fps_r) > 0 else 0.0
            pred_test_aux_fps_risk_prob[:, h_idx] = s_val
            pred_test_aux_fps_risk_class[:, h_idx] = s_val

    # Populate test predictions DataFrame
    pred_df = test_meta_df.copy()
    for h_idx, h in enumerate(horizons):
        pred_df[f"pred_ridge_load_h{h}"] = pred_test_load_ridge[:, h_idx]
        pred_df[f"pred_ridge_fps_h{h}"] = pred_test_fps_ridge[:, h_idx]
        pred_df[f"aux_pred_risk_prob_fps_h{h}"] = pred_test_aux_fps_risk_prob[:, h_idx]
        pred_df[f"aux_pred_risk_class_fps_h{h}"] = pred_test_aux_fps_risk_class[:, h_idx]
        pred_df[f"aux_pred_risk_majority_fps_h{h}"] = pred_test_aux_fps_risk_maj[:, h_idx]

        for th_name in thresholds:
            pred_df[f"pred_risk_majority_{th_name}_h{h}"] = pred_test_risk_maj[th_name][:, h_idx]
            pred_df[f"pred_risk_prob_{th_name}_h{h}"] = pred_test_risk_prob[th_name][:, h_idx]
            pred_df[f"pred_risk_class_{th_name}_h{h}"] = pred_test_risk_class[th_name][:, h_idx]

    # Metrics dictionary structure
    metrics: dict[str, Any] = {
        "parameters": {
            "selected_load_alphas": {f"h{h}": a for h, a in zip(horizons, selected_load_alphas)},
            "selected_logreg_C": selected_logreg_cs,
            "thresholds": thresholds,
            "fps_threshold": fps_threshold,
            "horizons": horizons,
        },
        "track1_load_regression": {
            "overall": {},
            "per_node": {},
        },
        "track2_load_classification": {
            "overall": {},
            "per_node": {},
        },
        "event_diagnostics": {
            "overall": {},
            "per_node": {},
        },
        "auxiliary_fps_regression": {
            "overall": {},
            "per_node": {},
        },
        "auxiliary_fps_risk_classification": {
            "overall": {},
            "per_node": {},
        },
        "caveats": [
            "Temporary offline evaluation on historical calibration segments; does not represent live edge runtime performance.",
            "No ONNX export or runtime deployment.",
            "Normalizations fitted strictly on training splits without data leakage.",
            "Primary model predicts edge load score (0-100) at horizons +6s and +10s.",
            "Target load score is actual measured load_score from CSV when finite, falling back to piecewise FPS anchor conversion (27->0, 22->57, 19->65, 17->75, 0->100).",
            "Track 2 risk classification targets future actual_load_score >= threshold (57/65/75) within [t+1, t+h].",
            "Event lead times are evaluated strictly within segment boundaries and bounded by horizon h (0 <= lead <= h seconds).",
        ],
    }

    # Track 1 Primary Load Regression Metrics
    for h_idx, h in enumerate(horizons):
        true_load = pred_df[f"true_load_h{h}"].to_numpy()
        pers_load = pred_df[f"persistence_load_h{h}"].to_numpy()
        ridge_load = pred_df[f"pred_ridge_load_h{h}"].to_numpy()
        cur_load = pred_df["current_load_score"].to_numpy()

        true_dir = np.sign(true_load - cur_load)
        pers_dir = np.sign(pers_load - cur_load)
        ridge_dir = np.sign(ridge_load - cur_load)

        pers_th_acc = {th_name: float(np.mean((pers_load >= th_val) == (true_load >= th_val))) for th_name, th_val in thresholds.items()}
        ridge_th_acc = {th_name: float(np.mean((ridge_load >= th_val) == (true_load >= th_val))) for th_name, th_val in thresholds.items()}

        metrics["track1_load_regression"]["overall"][f"h{h}"] = {
            "persistence": {
                "mae": float(mean_absolute_error(true_load, pers_load)),
                "rmse": float(root_mean_squared_error(true_load, pers_load)),
                "directional_accuracy": float(np.mean(true_dir == pers_dir)),
                "threshold_crossing_accuracy": pers_th_acc,
            },
            "ridge": {
                "mae": float(mean_absolute_error(true_load, ridge_load)),
                "rmse": float(root_mean_squared_error(true_load, ridge_load)),
                "directional_accuracy": float(np.mean(true_dir == ridge_dir)),
                "threshold_crossing_accuracy": ridge_th_acc,
            },
        }

        # Auxiliary FPS regression
        true_fps = pred_df[f"true_fps_h{h}"].to_numpy()
        pers_fps = pred_df[f"persistence_fps_h{h}"].to_numpy()
        ridge_fps = pred_df[f"pred_ridge_fps_h{h}"].to_numpy()
        cur_fps = pred_df["current_fps"].to_numpy()

        metrics["auxiliary_fps_regression"]["overall"][f"h{h}"] = {
            "persistence": {
                "mae": float(mean_absolute_error(true_fps, pers_fps)),
                "rmse": float(root_mean_squared_error(true_fps, pers_fps)),
                "directional_accuracy": float(np.mean(np.sign(true_fps - cur_fps) == np.sign(pers_fps - cur_fps))),
            },
            "ridge": {
                "mae": float(mean_absolute_error(true_fps, ridge_fps)),
                "rmse": float(root_mean_squared_error(true_fps, ridge_fps)),
                "directional_accuracy": float(np.mean(np.sign(true_fps - cur_fps) == np.sign(ridge_fps - cur_fps))),
            },
        }

    # Track 2 Primary Load Classification Metrics Overall
    for th_name in thresholds:
        metrics["track2_load_classification"]["overall"][th_name] = {}
        for h_idx, h in enumerate(horizons):
            y_true_r = pred_df[f"true_risk_{th_name}_h{h}"].to_numpy()
            y_pred_maj = pred_df[f"pred_risk_majority_{th_name}_h{h}"].to_numpy()
            y_pred_cls = pred_df[f"pred_risk_class_{th_name}_h{h}"].to_numpy()
            y_prob_r = pred_df[f"pred_risk_prob_{th_name}_h{h}"].to_numpy()

            pr_auc = None
            if len(np.unique(y_true_r)) > 1:
                p_vals, r_vals, _ = precision_recall_curve(y_true_r, y_prob_r)
                pr_auc = float(np.sum((r_vals[:-1] - r_vals[1:]) * (p_vals[:-1] + p_vals[1:]) / 2.0))

            cm_maj = confusion_matrix(y_true_r, y_pred_maj, labels=[0, 1])
            cm_log = confusion_matrix(y_true_r, y_pred_cls, labels=[0, 1])

            metrics["track2_load_classification"]["overall"][th_name][f"h{h}"] = {
                "majority": {
                    "precision": float(precision_score(y_true_r, y_pred_maj, zero_division=0)),
                    "recall": float(recall_score(y_true_r, y_pred_maj, zero_division=0)),
                    "f1": float(f1_score(y_true_r, y_pred_maj, zero_division=0)),
                    "confusion": {"tn": int(cm_maj[0, 0]), "fp": int(cm_maj[0, 1]), "fn": int(cm_maj[1, 0]), "tp": int(cm_maj[1, 1])},
                },
                "logistic_regression": {
                    "precision": float(precision_score(y_true_r, y_pred_cls, zero_division=0)),
                    "recall": float(recall_score(y_true_r, y_pred_cls, zero_division=0)),
                    "f1": float(f1_score(y_true_r, y_pred_cls, zero_division=0)),
                    "pr_auc": pr_auc,
                    "confusion": {"tn": int(cm_log[0, 0]), "fp": int(cm_log[0, 1]), "fn": int(cm_log[1, 0]), "tp": int(cm_log[1, 1])},
                },
            }

    # Per-node Metrics
    nodes = sorted(pred_df["node"].unique())
    for node in nodes:
        node_df = pred_df[pred_df["node"] == node]
        metrics["track1_load_regression"]["per_node"][node] = {}
        metrics["track2_load_classification"]["per_node"][node] = {th: {} for th in thresholds}
        metrics["auxiliary_fps_regression"]["per_node"][node] = {}

        for h in horizons:
            t_load = node_df[f"true_load_h{h}"].to_numpy()
            p_load = node_df[f"persistence_load_h{h}"].to_numpy()
            r_load = node_df[f"pred_ridge_load_h{h}"].to_numpy()
            c_load = node_df["current_load_score"].to_numpy()

            p_th_acc = {th_name: float(np.mean((p_load >= th_val) == (t_load >= th_val))) for th_name, th_val in thresholds.items()}
            r_th_acc = {th_name: float(np.mean((r_load >= th_val) == (t_load >= th_val))) for th_name, th_val in thresholds.items()}

            metrics["track1_load_regression"]["per_node"][node][f"h{h}"] = {
                "persistence": {
                    "mae": float(mean_absolute_error(t_load, p_load)),
                    "rmse": float(root_mean_squared_error(t_load, p_load)),
                    "directional_accuracy": float(np.mean(np.sign(t_load - c_load) == np.sign(p_load - c_load))),
                    "threshold_crossing_accuracy": p_th_acc,
                },
                "ridge": {
                    "mae": float(mean_absolute_error(t_load, r_load)),
                    "rmse": float(root_mean_squared_error(t_load, r_load)),
                    "directional_accuracy": float(np.mean(np.sign(t_load - c_load) == np.sign(r_load - c_load))),
                    "threshold_crossing_accuracy": r_th_acc,
                },
            }

            # Auxiliary FPS per-node
            t_fps = node_df[f"true_fps_h{h}"].to_numpy()
            p_fps = node_df[f"persistence_fps_h{h}"].to_numpy()
            r_fps = node_df[f"pred_ridge_fps_h{h}"].to_numpy()
            c_fps = node_df["current_fps"].to_numpy()

            metrics["auxiliary_fps_regression"]["per_node"][node][f"h{h}"] = {
                "persistence": {
                    "mae": float(mean_absolute_error(t_fps, p_fps)),
                    "rmse": float(root_mean_squared_error(t_fps, p_fps)),
                    "directional_accuracy": float(np.mean(np.sign(t_fps - c_fps) == np.sign(p_fps - c_fps))),
                },
                "ridge": {
                    "mae": float(mean_absolute_error(t_fps, r_fps)),
                    "rmse": float(root_mean_squared_error(t_fps, r_fps)),
                    "directional_accuracy": float(np.mean(np.sign(t_fps - c_fps) == np.sign(r_fps - c_fps))),
                },
            }

            # Load Risk per-node per-threshold
            for th_name in thresholds:
                ny_true = node_df[f"true_risk_{th_name}_h{h}"].to_numpy()
                ny_pred_maj = node_df[f"pred_risk_majority_{th_name}_h{h}"].to_numpy()
                ny_pred_cls = node_df[f"pred_risk_class_{th_name}_h{h}"].to_numpy()
                ny_prob = node_df[f"pred_risk_prob_{th_name}_h{h}"].to_numpy()

                n_pr_auc = None
                if len(np.unique(ny_true)) > 1:
                    p_v, r_v, _ = precision_recall_curve(ny_true, ny_prob)
                    n_pr_auc = float(np.sum((r_v[:-1] - r_v[1:]) * (p_v[:-1] + p_v[1:]) / 2.0))

                n_cm_maj = confusion_matrix(ny_true, ny_pred_maj, labels=[0, 1])
                n_cm_log = confusion_matrix(ny_true, ny_pred_cls, labels=[0, 1])

                metrics["track2_load_classification"]["per_node"][node][th_name][f"h{h}"] = {
                    "majority": {
                        "precision": float(precision_score(ny_true, ny_pred_maj, zero_division=0)),
                        "recall": float(recall_score(ny_true, ny_pred_maj, zero_division=0)),
                        "f1": float(f1_score(ny_true, ny_pred_maj, zero_division=0)),
                        "confusion": {"tn": int(n_cm_maj[0, 0]), "fp": int(n_cm_maj[0, 1]), "fn": int(n_cm_maj[1, 0]), "tp": int(n_cm_maj[1, 1])},
                    },
                    "logistic_regression": {
                        "precision": float(precision_score(ny_true, ny_pred_cls, zero_division=0)),
                        "recall": float(recall_score(ny_true, ny_pred_cls, zero_division=0)),
                        "f1": float(f1_score(ny_true, ny_pred_cls, zero_division=0)),
                        "pr_auc": n_pr_auc,
                        "confusion": {"tn": int(n_cm_log[0, 0]), "fp": int(n_cm_log[0, 1]), "fn": int(n_cm_log[1, 0]), "tp": int(n_cm_log[1, 1])},
                    },
                }

    # Compute threshold event diagnostics across thresholds and horizons
    metrics["event_diagnostics"] = compute_threshold_event_diagnostics(pred_df, horizons, thresholds)

    return metrics, pred_df, {"mean": feat_mean, "std": feat_std}


def compute_threshold_event_diagnostics(
    pred_df: pd.DataFrame,
    horizons: list[int],
    thresholds: dict[str, float],
) -> dict[str, Any]:
    """Computes bounded offline lead time and false alarms per threshold and horizon, strictly within segment boundaries."""
    diag: dict[str, Any] = {"overall": {}, "per_node": {}}
    nodes = sorted(pred_df["node"].unique())

    for th_name in thresholds:
        diag["overall"][th_name] = {}
        for node in nodes:
            if node not in diag["per_node"]:
                diag["per_node"][node] = {}
            if th_name not in diag["per_node"][node]:
                diag["per_node"][node][th_name] = {}

        for h in horizons:
            overall_leads = []
            overall_true_events = 0
            overall_matched_events = 0
            overall_missed_events = 0
            overall_pred_alarms = 0
            overall_false_alarms = 0

            for node in nodes:
                node_df = pred_df[pred_df["node"] == node]
                node_leads = []
                node_true_events = 0
                node_matched_events = 0
                node_missed_events = 0
                node_pred_alarms = 0
                node_false_alarms = 0

                segments = node_df["segment_id"].unique()
                for seg_id in segments:
                    seg_df = node_df[node_df["segment_id"] == seg_id].sort_values("ts").reset_index(drop=True)
                    true_r = seg_df[f"true_risk_{th_name}_h{h}"].to_numpy()
                    pred_r = seg_df[f"pred_risk_class_{th_name}_h{h}"].to_numpy()
                    ts = seg_df["ts"].to_numpy()
                    n_rows = len(true_r)
                    if n_rows == 0:
                        continue

                    # 1. Identify contiguous true risk episodes in this segment
                    true_episodes: list[tuple[int, int]] = []
                    in_ep = False
                    start_idx = 0
                    for i in range(n_rows):
                        if true_r[i] == 1.0 and not in_ep:
                            in_ep = True
                            start_idx = i
                        elif true_r[i] == 0.0 and in_ep:
                            in_ep = False
                            true_episodes.append((start_idx, i - 1))
                    if in_ep:
                        true_episodes.append((start_idx, n_rows - 1))

                    node_true_events += len(true_episodes)

                    # For each true risk episode, look for the first alarm within [event_start - h, event_start]
                    for s_idx, e_idx in true_episodes:
                        event_start_ts = ts[s_idx]
                        alarm_mask = (pred_r == 1.0) & (ts <= event_start_ts) & ((event_start_ts - ts) <= float(h))
                        if np.any(alarm_mask):
                            first_alarm_ts = float(np.min(ts[alarm_mask]))
                            lead_s = min(float(h), max(0.0, event_start_ts - first_alarm_ts))
                            node_leads.append(lead_s)
                            overall_leads.append(lead_s)
                            node_matched_events += 1
                        else:
                            node_missed_events += 1

                    # 2. Identify contiguous predicted alarm episodes in this segment
                    pred_episodes: list[tuple[int, int]] = []
                    in_alarm = False
                    start_a = 0
                    for i in range(n_rows):
                        if pred_r[i] == 1.0 and not in_alarm:
                            in_alarm = True
                            start_a = i
                        elif pred_r[i] == 0.0 and in_alarm:
                            in_alarm = False
                            pred_episodes.append((start_a, i - 1))
                    if in_alarm:
                        pred_episodes.append((start_a, n_rows - 1))

                    node_pred_alarms += len(pred_episodes)

                    # False alarm if no true risk in [alarm_ts, alarm_ts + h]
                    for sa, ea in pred_episodes:
                        alarm_ts = ts[sa]
                        eval_window_mask = (ts >= alarm_ts) & (ts <= alarm_ts + float(h))
                        if np.sum(true_r[eval_window_mask]) == 0:
                            node_false_alarms += 1

                overall_true_events += node_true_events
                overall_matched_events += node_matched_events
                overall_missed_events += node_missed_events
                overall_pred_alarms += node_pred_alarms
                overall_false_alarms += node_false_alarms

                diag["per_node"][node][th_name][f"h{h}"] = {
                    "true_events_count": node_true_events,
                    "matched_events_count": node_matched_events,
                    "missed_events_count": node_missed_events,
                    "predicted_alarm_episodes": node_pred_alarms,
                    "false_alarm_episodes": node_false_alarms,
                    "false_alarm_rate": float(node_false_alarms / node_pred_alarms) if node_pred_alarms > 0 else 0.0,
                    "mean_lead_s": float(np.mean(node_leads)) if node_leads else 0.0,
                    "median_lead_s": float(np.median(node_leads)) if node_leads else 0.0,
                    "max_lead_s": float(np.max(node_leads)) if node_leads else 0.0,
                }

            diag["overall"][th_name][f"h{h}"] = {
                "true_events_count": overall_true_events,
                "matched_events_count": overall_matched_events,
                "missed_events_count": overall_missed_events,
                "predicted_alarm_episodes": overall_pred_alarms,
                "false_alarm_episodes": overall_false_alarms,
                "false_alarm_rate": float(overall_false_alarms / overall_pred_alarms) if overall_pred_alarms > 0 else 0.0,
                "mean_lead_s": float(np.mean(overall_leads)) if overall_leads else 0.0,
                "median_lead_s": float(np.median(overall_leads)) if overall_leads else 0.0,
                "max_lead_s": float(np.max(overall_leads)) if overall_leads else 0.0,
            }

    return diag


def plot_results(
    pred_df: pd.DataFrame,
    horizons: list[int],
    thresholds: dict[str, float],
    fps_threshold: float,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    nodes = sorted(pred_df["node"].unique())

    # 1. Primary load_forecast_timeseries.png
    fig, axes = plt.subplots(nrows=len(nodes), ncols=len(horizons), figsize=(6 * len(horizons), 3.5 * len(nodes)), sharex=False, squeeze=False)
    for r_idx, node in enumerate(nodes):
        ndf = pred_df[pred_df["node"] == node].reset_index(drop=True)
        plot_ndf = ndf.iloc[:300] if len(ndf) > 300 else ndf
        rel_time = plot_ndf["ts"].to_numpy() - plot_ndf["ts"].iloc[0]

        for c_idx, h in enumerate(horizons):
            ax = axes[r_idx, c_idx]
            ax.plot(rel_time, plot_ndf[f"true_load_h{h}"], label="Actual Load Score", color="black", lw=1.5)
            ax.plot(rel_time, plot_ndf[f"pred_ridge_load_h{h}"], label="Ridge Forecast", color="tab:blue", ls="--", lw=1.2)
            ax.plot(rel_time, plot_ndf[f"persistence_load_h{h}"], label="Persistence", color="tab:orange", ls=":", lw=1.0)
            ax.axhline(57.0, color="goldenrod", linestyle="-.", alpha=0.75, label="L3 (57)")
            ax.axhline(65.0, color="darkorange", linestyle="--", alpha=0.75, label="L2 (65)")
            ax.axhline(75.0, color="red", linestyle=":", alpha=0.75, label="L1 (75)")
            ax.set_title(f"Node {node} - Load Forecast (+{h}s)")
            ax.set_xlabel("Relative Time (s)")
            ax.set_ylabel("Load Score (0-100)")
            ax.set_ylim(-5, 105)
            ax.grid(True, alpha=0.3)
            if r_idx == 0 and c_idx == 0:
                ax.legend(loc="upper right", fontsize=7)

    plt.tight_layout()
    plt.savefig(output_dir / "load_forecast_timeseries.png", dpi=200)
    plt.close()

    # 2. Primary load_forecast_scatter.png
    fig, axes = plt.subplots(nrows=1, ncols=len(horizons), figsize=(5.5 * len(horizons), 5), squeeze=False)
    for c_idx, h in enumerate(horizons):
        ax = axes[0, c_idx]
        actual = pred_df[f"true_load_h{h}"].to_numpy()
        ridge_pred = pred_df[f"pred_ridge_load_h{h}"].to_numpy()
        pers_pred = pred_df[f"persistence_load_h{h}"].to_numpy()

        ax.scatter(actual, ridge_pred, alpha=0.35, s=12, label="Ridge", color="tab:blue")
        ax.scatter(actual, pers_pred, alpha=0.15, s=8, label="Persistence", color="tab:orange")
        ax.plot([0, 100], [0, 100], "r--", lw=1.2, label="Ideal")
        ax.axvline(57.0, color="goldenrod", linestyle=":", alpha=0.5)
        ax.axhline(57.0, color="goldenrod", linestyle=":", alpha=0.5)
        ax.set_xlim(-2, 102)
        ax.set_ylim(-2, 102)
        ax.set_title(f"Horizon +{h}s: Pred vs Actual Load Score")
        ax.set_xlabel("Actual Load Score")
        ax.set_ylabel("Predicted Load Score")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper left")

    plt.tight_layout()
    plt.savefig(output_dir / "load_forecast_scatter.png", dpi=200)
    plt.close()

    # 3. Primary load_threshold_events.png
    fig, axes = plt.subplots(nrows=len(nodes), ncols=len(horizons), figsize=(6 * len(horizons), 3.2 * len(nodes)), sharex=False, squeeze=False)
    for r_idx, node in enumerate(nodes):
        ndf = pred_df[pred_df["node"] == node].reset_index(drop=True)
        plot_ndf = ndf.iloc[:300] if len(ndf) > 300 else ndf
        rel_time = plot_ndf["ts"].to_numpy() - plot_ndf["ts"].iloc[0]

        for c_idx, h in enumerate(horizons):
            ax = axes[r_idx, c_idx]
            actual_load = plot_ndf[f"true_load_h{h}"].to_numpy()
            pred_load = plot_ndf[f"pred_ridge_load_h{h}"].to_numpy()
            ax.plot(rel_time, actual_load, label="Actual Load", color="black", lw=1.3)
            ax.plot(rel_time, pred_load, label="Ridge Load", color="tab:blue", ls="--", lw=1.1)

            # Shade regions where actual load exceeds L3=57
            ax.fill_between(rel_time, 0, 100, where=(actual_load >= 57.0), color="crimson", alpha=0.15, label="Actual Overload (>=57)")
            # Alarm points
            alarm_mask = plot_ndf[f"pred_risk_class_L3_57_h{h}"].to_numpy() == 1.0
            if np.any(alarm_mask):
                ax.scatter(rel_time[alarm_mask], np.full(np.sum(alarm_mask), 57.0), color="crimson", marker="^", s=25, label="L3 Alarm", zorder=4)

            ax.axhline(57.0, color="goldenrod", linestyle="-.", alpha=0.6)
            ax.axhline(65.0, color="darkorange", linestyle="--", alpha=0.6)
            ax.axhline(75.0, color="red", linestyle=":", alpha=0.6)
            ax.set_title(f"Node {node} - Overload Events (+{h}s)")
            ax.set_xlabel("Relative Time (s)")
            ax.set_ylabel("Load Score")
            ax.set_ylim(-5, 105)
            ax.grid(True, alpha=0.3)
            if r_idx == 0 and c_idx == 0:
                ax.legend(loc="upper right", fontsize=7)

    plt.tight_layout()
    plt.savefig(output_dir / "load_threshold_events.png", dpi=200)
    plt.close()

    # 4. Primary load_risk_confusion.png (Grid for all thresholds and horizons)
    th_list = list(thresholds.keys())
    fig, axes = plt.subplots(nrows=len(th_list), ncols=len(horizons), figsize=(4.5 * len(horizons), 3.8 * len(th_list)), squeeze=False)
    for r_idx, th_name in enumerate(th_list):
        for c_idx, h in enumerate(horizons):
            ax = axes[r_idx, c_idx]
            cm = confusion_matrix(pred_df[f"true_risk_{th_name}_h{h}"], pred_df[f"pred_risk_class_{th_name}_h{h}"], labels=[0, 1])
            im = ax.imshow(cm, interpolation="nearest", cmap="Blues")
            ax.figure.colorbar(im, ax=ax)
            ax.set(
                xticks=[0, 1],
                yticks=[0, 1],
                xticklabels=["Normal (0)", f"Risk (1)"],
                yticklabels=["Normal (0)", f"Risk (1)"],
                title=f"{th_name} Risk (+{h}s)",
                ylabel="True Label",
                xlabel="Predicted Label",
            )
            thresh = cm.max() / 2.0
            for i in range(cm.shape[0]):
                for j in range(cm.shape[1]):
                    ax.text(j, i, format(cm[i, j], "d"), ha="center", va="center", color="white" if cm[i, j] > thresh else "black")

    plt.tight_layout()
    plt.savefig(output_dir / "load_risk_confusion.png", dpi=200)
    plt.close()

    # 5. Auxiliary fps_forecast_timeseries.png
    fig, axes = plt.subplots(nrows=len(nodes), ncols=len(horizons), figsize=(6 * len(horizons), 3.5 * len(nodes)), sharex=False, squeeze=False)
    for r_idx, node in enumerate(nodes):
        ndf = pred_df[pred_df["node"] == node].reset_index(drop=True)
        plot_ndf = ndf.iloc[:300] if len(ndf) > 300 else ndf
        rel_time = plot_ndf["ts"].to_numpy() - plot_ndf["ts"].iloc[0]

        for c_idx, h in enumerate(horizons):
            ax = axes[r_idx, c_idx]
            ax.plot(rel_time, plot_ndf[f"true_fps_h{h}"], label="Actual FPS", color="black", lw=1.5)
            ax.plot(rel_time, plot_ndf[f"pred_ridge_fps_h{h}"], label="Ridge Forecast", color="tab:blue", ls="--", lw=1.2)
            ax.plot(rel_time, plot_ndf[f"persistence_fps_h{h}"], label="Persistence", color="tab:orange", ls=":", lw=1.0)
            ax.axhline(fps_threshold, color="red", linestyle="-.", alpha=0.7, label=f"Threshold ({fps_threshold})")
            ax.set_title(f"Node {node} - FPS Forecast (+{h}s)")
            ax.set_xlabel("Relative Time (s)")
            ax.set_ylabel("FPS")
            ax.grid(True, alpha=0.3)
            if r_idx == 0 and c_idx == 0:
                ax.legend(loc="upper right", fontsize=8)

    plt.tight_layout()
    plt.savefig(output_dir / "fps_forecast_timeseries.png", dpi=200)
    plt.close()

    # 6. Auxiliary fps_forecast_scatter.png
    fig, axes = plt.subplots(nrows=1, ncols=len(horizons), figsize=(5 * len(horizons), 4.5), squeeze=False)
    for c_idx, h in enumerate(horizons):
        ax = axes[0, c_idx]
        actual = pred_df[f"true_fps_h{h}"].to_numpy()
        ridge_pred = pred_df[f"pred_ridge_fps_h{h}"].to_numpy()
        pers_pred = pred_df[f"persistence_fps_h{h}"].to_numpy()

        ax.scatter(actual, ridge_pred, alpha=0.3, s=12, label="Ridge", color="tab:blue")
        ax.scatter(actual, pers_pred, alpha=0.15, s=8, label="Persistence", color="tab:orange")
        min_val = min(float(np.min(actual)), float(np.min(ridge_pred))) - 1
        max_val = max(float(np.max(actual)), float(np.max(ridge_pred))) + 1
        ax.plot([min_val, max_val], [min_val, max_val], "r--", lw=1.2, label="Ideal")
        ax.set_xlim(min_val, max_val)
        ax.set_ylim(min_val, max_val)
        ax.set_title(f"Horizon +{h}s: Pred vs Actual FPS")
        ax.set_xlabel("Actual FPS")
        ax.set_ylabel("Predicted FPS")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper left")

    plt.tight_layout()
    plt.savefig(output_dir / "fps_forecast_scatter.png", dpi=200)
    plt.close()


def run_pipeline(
    input_dir: Path,
    output_dir: Path,
    history: int,
    horizons: list[int],
    thresholds: dict[str, float],
    fps_threshold: float,
    seed: int,
    is_self_check: bool = False,
) -> dict[str, Any]:
    np.random.seed(seed)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "cleaning_counts": {},
        "target_sources": {},
        "segments": {},
        "split_window_counts": {},
        "base_feature_list": BASE_FEATURE_COLS,
        "model_feature_list": MODEL_FEATURE_COLS,
        "horizons": horizons,
        "thresholds": thresholds,
        "parameters": {
            "history": history,
            "fps_threshold": fps_threshold,
            "seed": seed,
            "is_self_check": is_self_check,
        },
        "caveats": [
            "Temporary offline evaluation on historical calibration segments; does not represent live edge runtime performance.",
            "No ONNX export or runtime deployment.",
            "Normalizations fitted strictly on training splits without data leakage.",
            "Primary target is edge actual_load_score (0-100), predicting +6s and +10s future values.",
            "Track 2 risk classification evaluates multiple load thresholds: L3 (57), L2 (65), L1 (75).",
            "Event lead times are evaluated strictly within segment boundaries and bounded by horizon h (0 <= lead <= h seconds).",
        ],
    }

    if is_self_check:
        n_samples = 400
        ts_arr = np.arange(1000.0, 1000.0 + n_samples, 1.0)
        ts_arr[200:] += 10.0
        fps_base = 25.0 + 5.0 * np.sin(np.linspace(0, 10, n_samples)) + np.random.normal(0, 1.0, n_samples)
        fps_base[50:100] = 18.0 + np.random.normal(0, 0.5, 50)
        fps_base[250:280] = 19.0 + np.random.normal(0, 0.5, 30)
        fps_base = np.clip(fps_base, 5.0, 30.0)
        load_score_syn = convert_fps_to_load_score(fps_base)

        synthetic_data = {
            "ts": ts_arr,
            "n_active_cameras": np.full(n_samples, 2),
            "n_track_total": np.random.uniform(0.5, 2.0, n_samples),
            "n_plate_total": np.random.uniform(0.1, 1.0, n_samples),
            "stationary_fraction_mean": np.random.uniform(0.0, 0.5, n_samples),
            "fps_avg": fps_base,
            "gpu_percent": np.clip(100.0 - fps_base * 2.5, 5.0, 99.0),
            "cpu_percent": np.random.uniform(10.0, 30.0, n_samples),
            "ram_percent": np.random.uniform(20.0, 40.0, n_samples),
            "gpu_temp_c": np.random.uniform(40.0, 55.0, n_samples),
            "offload_crops_received_per_s": np.zeros(n_samples),
            "load_score": load_score_syn,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            syn_csv = Path(tmpdir) / "calibration_SYN.csv"
            pd.DataFrame(synthetic_data).to_csv(syn_csv, index=False)
            csv_list = [(syn_csv, "SYN")]
            return _execute_data_flow(csv_list, output_dir, history, horizons, thresholds, fps_threshold, summary)
    else:
        csv_list = discover_calibration_csvs(input_dir)
        if not csv_list:
            raise FileNotFoundError(f"No calibration_*.csv files discovered in {input_dir}")
        return _execute_data_flow(csv_list, output_dir, history, horizons, thresholds, fps_threshold, summary)


def _execute_data_flow(
    csv_list: list[tuple[Path, str]],
    output_dir: Path,
    history: int,
    horizons: list[int],
    thresholds: dict[str, float],
    fps_threshold: float,
    summary: dict[str, Any],
) -> dict[str, Any]:
    all_splits: dict[str, list[dict[str, np.ndarray]]] = {"train": [], "val": [], "test": []}
    all_test_meta_dfs: list[pd.DataFrame] = []

    for csv_path, node_name in csv_list:
        segs, stats = clean_and_segment_csv(csv_path, BASE_FEATURE_COLS)
        summary["cleaning_counts"][node_name] = stats
        summary["target_sources"][node_name] = stats.get("target_source", "unknown")
        summary["segments"][node_name] = {
            "total_segments": len(segs),
            "segment_lengths": [len(s) for s in segs],
        }

        node_window_counts = {"train": 0, "val": 0, "test": 0, "skipped_short_segments": 0}
        for s_idx, seg_df in enumerate(segs):
            seg_id = f"{node_name}_seg{s_idx}"
            split_arrays, split_meta_dfs = build_windows_for_segment(
                seg_df, history, horizons, thresholds, fps_threshold, node_name, segment_id=seg_id
            )
            if not split_arrays:
                node_window_counts["skipped_short_segments"] += 1
                continue
            for split_name in ["train", "val", "test"]:
                if split_name in split_arrays:
                    all_splits[split_name].append(split_arrays[split_name])
                    node_window_counts[split_name] += len(split_arrays[split_name]["X"])
            for m_df in split_meta_dfs:
                if not m_df.empty and (m_df["split"] == "test").all():
                    all_test_meta_dfs.append(m_df)

        summary["split_window_counts"][node_name] = node_window_counts

    # Aggregate split data across segments
    combined_splits = {}
    for split_name in ["train", "val", "test"]:
        combined_splits[split_name] = {
            "X": np.concatenate([d["X"] for d in all_splits[split_name]], axis=0),
            "y_load": np.concatenate([d["y_load"] for d in all_splits[split_name]], axis=0),
            "y_fps": np.concatenate([d["y_fps"] for d in all_splits[split_name]], axis=0),
            "y_risk_by_threshold": {
                th: np.concatenate([d["y_risk_by_threshold"][th] for d in all_splits[split_name]], axis=0)
                for th in thresholds
            },
            "y_aux_fps_risk": np.concatenate([d["y_aux_fps_risk"] for d in all_splits[split_name]], axis=0),
            "cur_load": np.concatenate([d["cur_load"] for d in all_splits[split_name]], axis=0),
            "cur_fps": np.concatenate([d["cur_fps"] for d in all_splits[split_name]], axis=0),
        }

    combined_test_meta = pd.concat(all_test_meta_dfs, ignore_index=True)

    metrics, pred_df, _ = fit_and_eval_models(combined_splits, combined_test_meta, horizons, thresholds, fps_threshold)

    # Save outputs
    with open(output_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    with open(output_dir / "data_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    pred_df.to_csv(output_dir / "predictions.csv", index=False)

    plot_results(pred_df, horizons, thresholds, fps_threshold, output_dir)

    return {"metrics": metrics, "summary": summary}


def main():
    args = parse_args()
    results = run_pipeline(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        history=args.history,
        horizons=args.horizons,
        thresholds=THRESHOLDS,
        fps_threshold=args.fps_threshold,
        seed=args.seed,
        is_self_check=args.self_check,
    )
    print(f"Evaluation complete. Summary metrics:")
    print(json.dumps(results["metrics"], indent=2))


if __name__ == "__main__":
    main()
