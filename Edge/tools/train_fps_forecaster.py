#!/usr/bin/env python3
"""Train an FPS forecaster from raw telemetry CSVs and export ONNX.

Input:   19-column raw profile-collector CSVs (see profile_collect.py schema),
         recursively discovered from --input-dir. p2p_migrations.csv & manifest.json
         are skipped. Each CSV is one independent session -- windows never cross files.

Features (10): n_active_cameras, n_track_total, n_plate_total,
                stationary_fraction_mean, fps_avg,
                gpu_percent, cpu_percent, ram_percent, gpu_temp_c,
                offload_crops_received_per_s

Target:    fps_avg at configured horizons (default 6 s, 10 s).

Cadence:   Raw telemetry arrives at source cadence (default 0.5 s). The runtime
            HEALTH_INTERVAL is 1.0 s. This tool aggregates source rows (mean over
            consecutive groups of 2) into model samples at the sample interval.
           Source files whose median ts-delta differs more than 5 % from
           --source-interval-s are rejected (log_just_A/surge-spike.csv at 1 s
           is excluded; surge_spike.csv at 0.5 s passes).

Model:     small MLP with train-only normalisation baked into model buffers so
           exported ONNX accepts raw (batch, history, 10) and emits raw FPS
           (batch, n_horizons).

Baseline:  persistence (last known fps_avg repeated for every horizon).

Metrics:   overall + per-session MAE/RMSE, written to metrics.json + predictions.csv
           + actual-vs-predicted plot PNG. Deterministic seed.

Usage:
    conda run -n DoAn python3 Edge/tools/train_fps_forecaster.py --input-dir data/csv_collected
    conda run -n DoAn python3 Edge/tools/train_fps_forecaster.py --self-check
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import nn

# -- constants --------------------------------------------------------------

FEATURE_COLS = [
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
N_FEATURES = len(FEATURE_COLS)  # 10

TARGET_COL = "fps_avg"

SKIP_FILENAMES = {"p2p_migrations.csv", "manifest.json"}

# -- MLP model with baked-in normalisation --------------------------------

class FPSForecaster(nn.Module):
    """MLP that normalizes raw inputs with buffers set after training.

    Exported ONNX accepts raw (batch, history, n_features) and emits
    (batch, n_horizons).
    """

    def __init__(self, n_horizons: int, history: int, n_features: int):
        super().__init__()
        flat_dim = history * n_features
        self.net = nn.Sequential(
            nn.Linear(flat_dim, 32),
            nn.ReLU(),
            nn.Linear(32, 16),
            nn.ReLU(),
            nn.Linear(16, n_horizons),
        )
        self.register_buffer("feat_mean", torch.zeros(1, 1, n_features))
        self.register_buffer("feat_std", torch.ones(1, 1, n_features))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_n = (x - self.feat_mean) / self.feat_std
        return self.net(x_n.flatten(1))


# -- data loading ---------------------------------------------------------

def _discover_csvs(input_dir: Path) -> list[tuple[Path, str]]:
    """Recursively discover CSV files, skipping p2p + manifest."""
    sessions: list[tuple[Path, str]] = []
    for p in sorted(input_dir.rglob("*.csv")):
        if p.name in SKIP_FILENAMES:
            continue
        sessions.append((p, p.stem))
    if not sessions:
        raise SystemExit(f"ERROR: no CSV files found under {input_dir}")
    return sessions


def _reject_if_bad_cadence(
    df: pd.DataFrame,
    path: Path,
    source_interval_s: float,
    tolerance: float = 0.05,
) -> bool:
    """Sort by ts, reject if non-monotonic or median delta > tol from source interval.
    Returns True if file should be rejected."""
    ts = df["ts"].values
    if not np.all(np.diff(ts) >= 0):
        # Not sorted after sorting? Check if monotonic after reindex.
        # Actually, we sort the whole df by ts and then check.
        # But _load_session will do that; here we get pre-sorted.
        return True
    deltas = np.diff(ts)
    if len(deltas) == 0:
        return True
    median_delta = float(np.median(deltas))
    if median_delta <= 0:
        return True
    deviation = abs(median_delta - source_interval_s) / source_interval_s
    if deviation > tolerance:
        print(
            f"[cadence] REJECT {path.name}: median delta={median_delta:.4f}s "
            f"({deviation*100:.1f}% off expected {source_interval_s}s) "
            f"-- skipping"
        )
        return True
    return False


def _aggregate_to_model_cadence(
    df: pd.DataFrame,
    source_interval_s: float,
    sample_interval_s: float,
) -> tuple[pd.DataFrame, dict]:
    """Aggregate source rows into model-cadence samples by mean within gapless segments.
    Returns (aggregated_df, stats_dict) where stats has source_rows, agg_bins, dropped_rows, gaps.
    Segments are consecutive source rows where each dt ≈ source_interval_s (±50%).
    """
    group_size = int(round(sample_interval_s / source_interval_s))
    if group_size < 2:
        # ponytail: single-row cadence, no holes, just mark dominant cadence
        return df, {"source_rows": len(df), "agg_bins": len(df), "dropped_rows": 0, "n_gaps": 0, "n_segments": 1}

    gap_tolerance = source_interval_s * 1.5
    ts = df["ts"].values
    deltas = np.diff(ts)
    # segment boundaries: where delta exceeds tolerance
    gap_mask = deltas > gap_tolerance
    n_gaps = int(np.sum(gap_mask))
    # segment start indices (0 + every index after a gap)
    seg_starts = np.concatenate([[0], np.where(gap_mask)[0] + 1])
    seg_ends = np.concatenate([np.where(gap_mask)[0] + 1, [len(df)]])
    n_segments = len(seg_starts)

    numeric_cols = [c for c in FEATURE_COLS if c != TARGET_COL] + [TARGET_COL]
    has_sid = "_session_id" in df.columns
    total_source = len(df)
    total_bins = 0
    dropped_rows = 0
    segments = []

    for start, end in zip(seg_starts, seg_ends):
        chunk = df.iloc[start:end]
        n_chunk = len(chunk)
        full_groups = n_chunk // group_size
        if full_groups == 0:
            dropped_rows += n_chunk
            continue
        trimmed = chunk.iloc[:full_groups * group_size]
        means = trimmed[numeric_cols].values.reshape(full_groups, group_size, -1).mean(axis=1)
        seg_df = pd.DataFrame(means, columns=numeric_cols)
        seg_df = seg_df[FEATURE_COLS]
        if has_sid:
            seg_df["_session_id"] = trimmed["_session_id"].iloc[::group_size].values
        ts_vals = trimmed["ts"].values.reshape(full_groups, group_size)
        seg_df["_anchor_ts"] = ts_vals.max(axis=1)
        seg_df["_segment_id"] = f"{chunk['_session_id'].iloc[0]}_seg{len(segments)}" if has_sid else str(len(segments))
        segments.append(seg_df)
        total_bins += full_groups
        dropped_rows += n_chunk - full_groups * group_size

    stats = {
        "source_rows": total_source,
        "agg_bins": total_bins,
        "dropped_rows": dropped_rows,
        "n_gaps": n_gaps,
        "n_segments": n_segments,
    }
    if not segments:
        return pd.DataFrame(columns=FEATURE_COLS + ["_anchor_ts", "_segment_id"]), stats
    return pd.concat(segments, ignore_index=True), stats


def _load_session(
    path: Path,
    sid: str,
    source_interval_s: float,
) -> pd.DataFrame | None:
    """Load one CSV, sort by ts, reject if cadence mismatch or columns missing."""
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    missing = [c for c in FEATURE_COLS if c not in df.columns]
    if missing or TARGET_COL not in df.columns or "ts" not in df.columns:
        return None

    # Sort by timestamp
    df = df.sort_values("ts").reset_index(drop=True)

    # Cadence check
    if _reject_if_bad_cadence(df, path, source_interval_s):
        return None

    # Non‑monotonic after sort is impossible by construction, but double-check
    ts = df["ts"].values
    if not np.all(np.diff(ts) >= 0):
        print(f"[reject] {path.name}: non-monotonic timestamps -- skipping")
        return None

    df = df.dropna(subset=FEATURE_COLS + [TARGET_COL]).reset_index(drop=True)
    if len(df) < 30:
        return None
    df["_session_id"] = sid
    return df


def _make_windows(
    arr_features: np.ndarray,
    arr_target: np.ndarray,
    history: int,
    horizon_steps: list[int],
) -> tuple[np.ndarray, np.ndarray]:
    """One session at a time. Returns (x [N,hist,feat], y [N,n_horiz])."""
    max_horizon = max(horizon_steps)
    end = len(arr_features) - max_horizon
    xs_rows, ys_rows = [], []
    for i in range(history - 1, end):
        xs_rows.append(arr_features[i - history + 1 : i + 1])
        ys_rows.append([arr_target[i + h] for h in horizon_steps])
    if not xs_rows:
        nf = len(FEATURE_COLS)
        nh = len(horizon_steps)
        return (
            np.empty((0, history, nf), dtype=np.float32),
            np.empty((0, nh), dtype=np.float32),
        )
    return (
        np.asarray(xs_rows, dtype=np.float32),
        np.asarray(ys_rows, dtype=np.float32),
    )


# -- persistence baseline -------------------------------------------------

def persistence_predict(x: np.ndarray, horizon_indices: list[int]) -> np.ndarray:
    """Last fps_avg (index 4) repeated for every horizon."""
    fps_idx = FEATURE_COLS.index("fps_avg")
    last_fps = x[:, -1, fps_idx]  # (N,)
    return np.tile(last_fps[:, None], (1, len(horizon_indices)))


# -- Ridge regression (NumPy only, delta-FPS target) -----------------------

ALPHA_GRID = [0.0, 0.1, 1.0, 10.0, 100.0]
"""Alpha grid for Ridge L2 regularisation. Best alpha selected per horizon
on the validation partition by lowest per-horizon RMSE (independent selection,
not mean multi-horizon). Documented in metrics.json."""


def _train_ridge(
    X_train_flat: np.ndarray,
    y_train: np.ndarray,
    last_fps_train: np.ndarray,
    X_val_flat: np.ndarray,
    y_val: np.ndarray,
    last_fps_val: np.ndarray | None = None,
    alpha_grid: list[float] | None = None,
) -> tuple[np.ndarray, float]:
    """Fit per-horizon Ridge on delta-FPS target. Returns (coef [flat_dim, n_horiz], best_alpha).
    Inputs are already feature-scaled; last_fps_* arrays are ground-truth fps at anchor timestep."""
    if alpha_grid is None:
        alpha_grid = ALPHA_GRID
    n_h = y_train.shape[1]
    flat_dim = X_train_flat.shape[1]
    delta_train = y_train - last_fps_train[:, np.newaxis]

    has_val = X_val_flat is not None and y_val is not None and last_fps_val is not None and len(X_val_flat) > 0
    best_coefs = np.zeros((flat_dim, n_h), dtype=np.float64)
    best_alpha = np.zeros(n_h, dtype=np.float64)
    I = np.eye(flat_dim, dtype=np.float64)
    XtX = X_train_flat.T @ X_train_flat
    Xty = X_train_flat.T @ delta_train

    for h in range(n_h):
        best_alpha[h] = alpha_grid[0]
        # Use lstsq for alpha=0 (XtX may be singular), solve for alpha>0 (regularized)
        if alpha_grid[0] == 0.0:
            best_w, _, _, _ = np.linalg.lstsq(X_train_flat, delta_train[:, h], rcond=None)
        else:
            best_w = np.linalg.solve(XtX + alpha_grid[0] * I, Xty[:, h])
        best_rmse = float("inf")
        if has_val:
            best_rmse = float(math.sqrt(np.mean((y_val[:, h] - (last_fps_val + X_val_flat @ best_w)) ** 2, dtype=np.float64)))
        for a in alpha_grid[1:]:
            if a == 0.0:
                w, _, _, _ = np.linalg.lstsq(X_train_flat, delta_train[:, h], rcond=None)
            else:
                w = np.linalg.solve(XtX + a * I, Xty[:, h])
            if has_val:
                rmse = float(math.sqrt(np.mean((y_val[:, h] - (last_fps_val + X_val_flat @ w)) ** 2, dtype=np.float64)))
            else:
                rmse = 0.0
            if rmse < best_rmse:
                best_rmse = rmse
                best_alpha[h] = a
                best_w = w
        best_coefs[:, h] = best_w
    return best_coefs, best_alpha


def _ridge_predict(X_flat: np.ndarray, last_fps: np.ndarray, best_coefs: np.ndarray) -> np.ndarray:
    """Predict FPS from scaled features + anchor fps using Ridge delta model."""
    delta = X_flat @ best_coefs
    return last_fps[:, np.newaxis] + delta


# -- metrics ---------------------------------------------------------------

def _metrics_dict(y_true: np.ndarray, y_mlp: np.ndarray, y_persist: np.ndarray,
                  y_ridge: np.ndarray | None = None) -> dict:
    mae_mlp = float(np.mean(np.abs(y_true - y_mlp), dtype=np.float64))
    rmse_mlp = float(math.sqrt(np.mean((y_true - y_mlp) ** 2, dtype=np.float64)))
    mae_p = float(np.mean(np.abs(y_true - y_persist), dtype=np.float64))
    rmse_p = float(math.sqrt(np.mean((y_true - y_persist) ** 2, dtype=np.float64)))
    result = {
        "mlp_mae": round(mae_mlp, 4),
        "mlp_rmse": round(rmse_mlp, 4),
        "persistence_mae": round(mae_p, 4),
        "persistence_rmse": round(rmse_p, 4),
        "n_samples": len(y_true),
    }
    if y_ridge is not None:
        mae_r = float(np.mean(np.abs(y_true - y_ridge), dtype=np.float64))
        rmse_r = float(math.sqrt(np.mean((y_true - y_ridge) ** 2, dtype=np.float64)))
        result["ridge_mae"] = round(mae_r, 4)
        result["ridge_rmse"] = round(rmse_r, 4)
    return result


# -- plot ------------------------------------------------------------------

def _plot_actual_vs_predicted(
    y_true: np.ndarray,
    y_mlp: np.ndarray,
    y_persist: np.ndarray,
    horizon_labels: list[str],
    out_path: Path,
    y_ridge: np.ndarray | None = None,
):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[plot] matplotlib not available -- skipping PNG")
        return
    n_h = y_true.shape[1]
    fig, axes = plt.subplots(1, n_h, figsize=(5 * n_h, 4), squeeze=False)
    for h in range(n_h):
        ax = axes[0][h]
        ax.scatter(y_true[:, h], y_mlp[:, h], alpha=0.4, s=8, label="MLP")
        ax.scatter(y_true[:, h], y_persist[:, h], alpha=0.4, s=8, label="Persistence")
        if y_ridge is not None:
            ax.scatter(y_true[:, h], y_ridge[:, h], alpha=0.4, s=8, label="Ridge")
        vals = [y_true[:, h], y_mlp[:, h], y_persist[:, h]]
        if y_ridge is not None:
            vals.append(y_ridge[:, h])
        vmin = min(v.min() for v in vals)
        vmax = max(v.max() for v in vals)
        dur = abs(vmax - vmin) * 0.05 or 0.5
        ax.plot([vmin - dur, vmax + dur], [vmin - dur, vmax + dur],
                "k--", linewidth=0.5)
        ax.set_xlabel("Actual FPS")
        ax.set_ylabel("Predicted FPS")
        ax.set_title(horizon_labels[h])
        ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[plot] wrote {out_path}")


# -- self-check ------------------------------------------------------------

def self_check():
    """Create temp CSVs on disk; verify: 1s skip, 0.5->2.0 agg,
    default shape (5,2), source-session isolation, horizons (6/10),
    audit timestamps in preds CSV, Ridge metrics & preds exist,
    gap bridging prevention + target-anchor offset tolerance."""
    print("=== SELF-CHECK ===")
    tmp = Path(tempfile.mkdtemp(prefix="fps_forecaster_self_check_"))

    rng = np.random.default_rng(12345)

    # --- Create 3 valid sessions at 0.5 s cadence ---
    NROWS = 340  # yields ~85 aggregates each, enough for training
    for sess in range(3):
        rows = []
        t0 = sess * 200.0
        fps_base = 20.0 + sess * 2.0
        for i in range(NROWS):
            noise = rng.uniform(-0.5, 0.5)
            rows.append({
                "ts": round(t0 + i * 0.5, 3),
                "gpu_percent": round(np.clip(50. + noise * 10, 0, 100), 1),
                "cpu_percent": round(np.clip(30. + noise * 10, 0, 100), 1),
                "ram_percent": round(np.clip(45. + noise * 5, 0, 100), 1),
                "gpu_temp_c": round(np.clip(55. + noise * 3, 40, 72), 1),
                "fps_avg": round(np.clip(fps_base + noise * 2, 9, 30), 1),
                "n_active_cameras": 1 + (sess % 2),
                "n_track_total": round(np.clip(5. + noise * 3, 0, 20), 1),
                "n_plate_total": round(np.clip(3. + noise * 2, 0, 15), 1),
                "stationary_fraction_mean": round(np.clip(0.2 + noise * 0.1, 0, 1), 3),
                "offload_crops_received_per_s": round(max(0., noise * 2), 2),
            })
        path = tmp / f"session_{sess}.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

# --- Create session with intentional gap at 0.5 s cadence ---
    #   Two segments of 200 rows each (~100s each), 5 s gap in between.
    #   Aggregation: 200//4=50 bins per segment, test=50*0.15=7 bins after 70/15/15
    #   ponytail: need test >= history+max_horizon (10) so increase to meaningful sizes
    #
    #   With 200 rows (50 bins): test=7, windows=7-5=2 — too few. Bump to 400 rows.
    #   400 rows → 100 bins → test=15 → windows=15-6=9 ✓
    N_GAP_ROWS = 400  # 0.5s each → 200s, → 100 bins after aggregation
    rows_gap = []
    fps_base = 22.0
    for segment_start in [0.0, 205.0]:  # 5s gap between segments (200s → gap → 205s)
        for i in range(N_GAP_ROWS):
            rows_gap.append({
                "ts": round(segment_start + i * 0.5, 3),
                "gpu_percent": round(np.clip(50. + rng.uniform(-0.5,0.5)*10, 0, 100), 1),
                "cpu_percent": round(np.clip(30. + rng.uniform(-0.5,0.5)*10, 0, 100), 1),
                "ram_percent": round(np.clip(45. + rng.uniform(-0.5,0.5)*5, 0, 100), 1),
                "gpu_temp_c": round(np.clip(55. + rng.uniform(-0.5,0.5)*3, 40, 72), 1),
                "fps_avg": round(np.clip(fps_base + rng.uniform(-0.5,0.5)*2, 9, 30), 1),
                "n_active_cameras": 2,
                "n_track_total": round(np.clip(5. + rng.uniform(-0.5,0.5)*3, 0, 20), 1),
                "n_plate_total": round(np.clip(3. + rng.uniform(-0.5,0.5)*2, 0, 15), 1),
                "stationary_fraction_mean": round(np.clip(0.2 + rng.uniform(-0.5,0.5)*0.1, 0, 1), 3),
                "offload_crops_received_per_s": round(max(0., rng.uniform(-0.5,0.5)*2), 2),
            })
    path_gap = tmp / "session_gap.csv"
    with open(path_gap, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_gap[0].keys()))
        w.writeheader()
        w.writerows(rows_gap)

    # --- Create a 1 s cadence session that MUST be rejected ---
    rows_1s = []
    for i in range(200):
        noise = rng.uniform(-0.5, 0.5)
        rows_1s.append({
            "ts": round(1000.0 + i * 1.0, 3),
            "gpu_percent": round(np.clip(50. + noise * 19, 0, 100), 1),
            "cpu_percent": round(np.clip(30. + noise * 10, 0, 100), 1),
            "ram_percent": round(np.clip(45. + noise * 5, 0, 100), 1),
            "gpu_temp_c": round(np.clip(55. + noise * 3, 40, 72), 1),
            "fps_avg": round(np.clip(20. + noise * 2, 9, 30), 1),
            "n_active_cameras": 1,
            "n_track_total": round(np.clip(5. + noise * 3, 0, 20), 1),
            "n_plate_total": round(np.clip(3. + noise * 2, 0, 15), 1),
            "stationary_fraction_mean": round(np.clip(0.2 + noise * 0.1, 0, 1), 3),
            "offload_crops_received_per_s": round(max(0., noise * 2), 2),
        })
    path_1s = tmp / "surge-spike.csv"
    with open(path_1s, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_1s[0].keys()))
        w.writeheader()
        w.writerows(rows_1s)

    # dummy skip files
    (tmp / "p2p_migrations.csv").write_text("a\n1\n")
    (tmp / "manifest.json").write_text("{}")

    # Run training with new defaults: history=5, horizons=[6,10],
    # source_interval=0.5, sample_interval=2.0
    args = argparse.Namespace(
        input_dir=tmp,
        output_onnx=tmp / "fps_forecast.onnx",
        output_metrics=tmp / "metrics.json",
        output_predictions=tmp / "predictions.csv",
        output_plot=tmp / "plot.png",
        history=5,
        horizons=[6.0, 10.0],
        source_interval_s=0.5,
        sample_interval_s=2.0,
        epochs=50,
        lr=1e-3,
        seed=42,
    )
    _run_training(args)

    # Assertion 1: 1s session was skipped (only 3 sessions loaded)
    metrics = json.loads(args.output_metrics.read_text())
    assert args.output_predictions.exists(), "predictions CSV not written"
    pred_df = pd.read_csv(args.output_predictions)
    sids = set(pred_df["session_id"].unique())
    assert len(sids) == 4, (
        f"Expected 4 sessions (1s rejected, gap session included), got {len(sids)}: {sids}"
    )

    # Assertion 2: 0.5 → 2.0 aggregation (group_size=4)
    # After aggregation, a session of 340 source rows → 85 samples
    # Each session has 85 rows at 2.0s cadence; with history=5 and max_horizon=5 (10/2.0),
    #   windows = 85 - 5 = 80 qualifying positions; 80 samples per session test.
    # Quick sanity: model shape (history, n_horizons) = (5, 2)
    reverse_engineer_steps = [int(round(h / 2.0)) for h in [6.0, 10.0]]
    assert reverse_engineer_steps == [3, 5]
    assert metrics["history_window_samples"] == 5
    assert metrics["n_horizons"] == 2
    assert metrics["source_cadence_s"] == 0.5
    assert metrics["model_cadence_s"] == 2.0
    effective_hist = metrics["effective_history_s"]
    assert effective_hist == 10.0, f"effective history should be 5*2.0=10.0s, got {effective_hist}"

    # Assertion 3: default shape (5, 2) — history=5, 2 horizons
    model_shape = FPSForecaster(2, 5, N_FEATURES)
    out = model_shape(torch.randn(1, 5, N_FEATURES))
    assert out.shape == (1, 2), f"bad shape: {out.shape}"

    # Assertion 4: source-session isolation → every session_id maps to one source file
    assert len(pred_df) > 0, "no predictions"
    session_source_counts = pred_df.groupby("session_id").size()
    # each session contributes windows (4 total: 3 regular 0.5s + 1 gap session)
    assert len(session_source_counts) >= 4, (
        f"expected >=4 sessions in preds (3 regular + gap), got {len(session_source_counts)}"
    )
    # verify gap session appears specifically
    assert "session_gap" in session_source_counts.index, "gap session missing from preds"

    # Assertion 5: horizons output = [6, 10]
    horizon_labels = metrics["horizon_labels"]
    assert horizon_labels == ["t+6.0s", "t+10.0s"], f"unexpected horizons: {horizon_labels}"

    # Assertion 6: predictions CSV has audit timestamps
    assert "anchor_ts" in pred_df.columns, "missing anchor_ts"
    assert "target_ts_h0" in pred_df.columns, "missing target_ts_h0"
    assert "target_ts_h1" in pred_df.columns, "missing target_ts_h1"
    assert "last_fps" in pred_df.columns, "missing last_fps"
    # anchor_ts should be increasing per session (chronological)
    for sid in sids:
        subset = pred_df[pred_df["session_id"] == sid]
        assert (subset["anchor_ts"].diff().dropna() > 0).all(), f"non-monotonic anchor_ts for {sid}"

    # Assertion 7: Ridge metrics exist in JSON
    ridge_info = metrics.get("ridge")
    assert ridge_info is not None, "ridge section missing"
    assert "best_alphas" in ridge_info
    assert float(ridge_info["best_alphas"]["h3"]) >= 0.0
    # Ridge columns exist in predictions CSV
    assert "t+6.0s_ridge" in pred_df.columns, "missing ridge pred column"
    assert "t+10.0s_ridge" in pred_df.columns, "missing ridge pred column"
    assert "t+6.0s_persistence" in pred_df.columns, "missing persistence column"
    assert "t+10.0s_persistence" in pred_df.columns, "missing persistence column"

    # Assertion 8: gap bridging prevention — no prediction window bridges a source-cadence gap
    # The gap session has 2 segments of 400 source rows each, 5 s gap at ~200s→~205s.
    # After aggregation at 2 s cadence: 100 bins per segment.
    # Seg0 anchors cover ~0-198s (max anchor=last bin end ~200). Seg1 anchors cover ~205-403s.
    # No window should anchor in seg0 and target in seg1.
    gap_preds = pred_df[pred_df["session_id"] == "session_gap"].copy()
    assert len(gap_preds) > 0, "gap session has no predictions"

    # (a) gap session present, both segments yield predictions
    seg0 = gap_preds[gap_preds["anchor_ts"] < 200.0]
    seg1 = gap_preds[gap_preds["anchor_ts"] > 200.0]
    assert len(seg0) > 0, "no seg0 predictions (pre-gap)"
    assert len(seg1) > 0, "no seg1 predictions (post-gap)"

    # (b) per-row target_ts offsets ≈ horizon values (±3 s tolerance for aggregation bin boundaries)
    for h_idx, horizon_s in enumerate([6.0, 10.0]):
        offset = gap_preds[f"target_ts_h{h_idx}"] - gap_preds["anchor_ts"]
        max_err = (offset - horizon_s).abs().max()
        assert max_err < 3.0, f"target-anchor offset for t+{horizon_s}s deviates by {max_err:.2f}s"

    # (c) no window bridges the gap: every seg0 target_ts < first seg1 anchor
    max_seg0_target = seg0["target_ts_h1"].max()  # t+10s
    min_seg1_anchor = seg1["anchor_ts"].min()
    assert max_seg0_target < min_seg1_anchor, (
        f"Window bridges gap! max seg0 target={max_seg0_target:.2f} >= min seg1 anchor={min_seg1_anchor:.2f}"
    )
    # (c2) segment_id metadata proves all predictions on one side or the other
    assert "segment_id" in gap_preds.columns, "no segment_id in predictions"
    gap_seg_ids = set(gap_preds["segment_id"].unique())
    assert len(gap_seg_ids) == 2, f"expected 2 segment_ids, got {gap_seg_ids}"
    # seg0 and seg1 each have exactly one segment_id
    seg0_ids = set(seg0["segment_id"].unique())
    seg1_ids = set(seg1["segment_id"].unique())
    assert len(seg0_ids) == 1 and len(seg1_ids) == 1, "segments split incorrectly"
    assert seg0_ids != seg1_ids, "both sides share same segment_id"

    # data_stats recorded gaps
    data_stats = metrics.get("data_stats", {})
    assert data_stats.get("cadence_gaps_detected", 0) >= 1, "no gap recorded in data_stats"

    # ONNX exists
    assert args.output_onnx.exists(), "ONNX not written"
    print(f"ONNX: {args.output_onnx.stat().st_size} bytes written")

    print("SELF-CHECK: ALL PASSED")
    shutil.rmtree(tmp, ignore_errors=False)


# -- _run_training ---------------------------------------------------------

def _run_training(args) -> None:
    import matplotlib
    matplotlib.use("Agg")

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    source_interval = args.source_interval_s
    sample_interval = args.sample_interval_s

    # Validate horizons are exact multiples of sample interval
    for h in args.horizons:
        ratio = h / sample_interval
        if abs(round(ratio) - ratio) > 1e-9:
            raise SystemExit(
                f"Horizon {h}s is not an exact multiple of "
                f"sample-interval ({sample_interval}s). "
                f"Allowed: {sample_interval}s, {2*sample_interval}s, ... "
                f"({ratio} steps)"
            )
    horizon_steps = [int(round(h / sample_interval)) for h in args.horizons]
    if any(h < 1 for h in horizon_steps):
        raise SystemExit(
            f"Horizons must be >= sample-interval ({sample_interval}s)"
        )
    horizon_labels = [f"t+{h}s" for h in args.horizons]

    group_size = int(round(sample_interval / source_interval))
    print(
        f"[cadence] source={source_interval}s → model={sample_interval}s "
        f"(aggregate {group_size}→1, history={args.history} samples = "
        f"{args.history * sample_interval:.1f}s effective)"
    )

    # 1) discover & load
    sessions = _discover_csvs(args.input_dir)
    print(f"[load] found {len(sessions)} session CSV(s)")

    all_stats = {"total_source_rows": 0, "total_agg_bins": 0, "total_dropped_rows": 0, "total_gaps": 0}
    all_dfs: dict[str, pd.DataFrame] = {}
    for path, sid in sessions:
        df = _load_session(path, sid, source_interval)
        if df is None:
            continue
        df, stats = _aggregate_to_model_cadence(df, source_interval, sample_interval)
        all_stats["total_source_rows"] += stats["source_rows"]
        all_stats["total_agg_bins"] += stats["agg_bins"]
        all_stats["total_dropped_rows"] += stats["dropped_rows"]
        all_stats["total_gaps"] += stats["n_gaps"]
        if len(df) < 8:
            print(f"[load] {sid}: too few rows after aggregation ({len(df)}) -- skip")
            continue
        all_dfs[sid] = df

    print(f"[load] {len(all_dfs)} valid session(s) after cadence check + aggregation")
    print(f"       {all_stats['total_source_rows']} source rows → {all_stats['total_agg_bins']} agg bins, "
          f"{all_stats['total_dropped_rows']} rows dropped from incomplete tails, "
          f"{all_stats['total_gaps']} cadence gap(s)")
    if not all_dfs:
        raise SystemExit("No valid sessions — check cadence or adjust --source-interval-s")

    # 2) session-safe chronological 70/15/15 split WITHIN each segment
    def _split_session(df: pd.DataFrame, history: int, max_horizon: int, group_col: str):
        """Split df respecting segment boundaries. Returns 3 lists of DataFrames (train/val/test)."""
        train_list, val_list, test_list = [], [], []
        for _, grp in df.groupby(group_col, sort=False):
            n = len(grp)
            if n < history + max_horizon:
                continue
            t_end = int(n * 0.70)
            v_end = int(n * 0.85)
            train_list.append(grp.iloc[:t_end])
            val_list.append(grp.iloc[t_end:v_end])
            test_list.append(grp.iloc[v_end:])
        return train_list, val_list, test_list

    max_horizon = max(horizon_steps)
    train_parts, val_parts, test_parts = [], [], []
    for df in all_dfs.values():
        trs, vas, tes = _split_session(df, args.history, max_horizon, "_segment_id")
        train_parts.extend(trs)
        val_parts.extend(vas)
        test_parts.extend(tes)

    # 3) window each partition session-by-session
    history_win = args.history
    horizon_step_count = len(horizon_steps)

    def _window_sessions(
        parts: list[pd.DataFrame],
    ) -> tuple[np.ndarray, np.ndarray, list[str], list[float], list[list[float]], list[str]]:
        xs, ys, sids, anchor_tss, target_tss, seg_ids = [], [], [], [], [], []
        for df in parts:
            sid = df["_session_id"].iloc[0]
            seg_id = df["_segment_id"].iloc[0]
            feats = df[FEATURE_COLS].values.astype(np.float32)
            target = df[TARGET_COL].values.astype(np.float32)
            anchors = df["_anchor_ts"].values
            x_s, y_s = _make_windows(feats, target, history_win, horizon_steps)
            n_wins = len(x_s)
            if n_wins > 0:
                xs.append(x_s)
                ys.append(y_s)
                sids.extend([sid] * n_wins)
                seg_ids.extend([seg_id] * n_wins)
                # anchor_ts = last window row's bin-end ts
                for i in range(history_win - 1, history_win - 1 + n_wins):
                    anchor_tss.append(float(anchors[i]))
                # target_ts per horizon
                for i in range(history_win - 1, history_win - 1 + n_wins):
                    target_tss.append([float(anchors[i + h]) for h in horizon_steps])
        empty_x = np.empty((0, history_win, N_FEATURES), dtype=np.float32)
        empty_y = np.empty((0, horizon_step_count), dtype=np.float32)
        if not xs:
            return empty_x, empty_y, [], [], []
        return np.concatenate(xs), np.concatenate(ys), sids, anchor_tss, target_tss, seg_ids

    X_train, y_train, sids_train, anchor_ts_train, target_ts_train, seg_ids_train = _window_sessions(train_parts)
    X_val, y_val, sids_val, anchor_ts_val, target_ts_val, seg_ids_val = _window_sessions(val_parts)
    X_test, y_test, sids_test, anchor_ts_test, target_ts_test, seg_ids_test = _window_sessions(test_parts)

    if len(X_train) == 0:
        raise SystemExit("No training windows — reduce --history or collect more data")

    print(f"[data] train {X_train.shape[0]}, val {X_val.shape[0]}, test {X_test.shape[0]}")

    # 4) train-only norm stats
    feat_mean = X_train.mean(axis=(0, 1), keepdims=True)  # (1, 1, n_feat)
    feat_std = X_train.std(axis=(0, 1), keepdims=True) + 1e-7

    # 5) MLP model
    n_horizons = len(horizon_steps)
    model = FPSForecaster(n_horizons, args.history, N_FEATURES)
    model.feat_mean.copy_(torch.from_numpy(feat_mean))
    model.feat_std.copy_(torch.from_numpy(feat_std))

    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    loss_fn = nn.MSELoss()

    X_train_t = torch.from_numpy(X_train)
    y_train_t = torch.from_numpy(y_train)
    has_val = len(X_val) > 0
    X_val_t = torch.from_numpy(X_val) if has_val else None
    y_val_t = torch.from_numpy(y_val) if has_val else None

    best_val_loss = float("inf")
    best_state = None

    for epoch in range(1, args.epochs + 1):
        model.train()
        opt.zero_grad()
        pred = model(X_train_t)
        loss = loss_fn(pred, y_train_t)
        loss.backward()
        opt.step()

        if has_val and epoch % 5 == 0:
            model.eval()
            with torch.no_grad():
                val_loss = loss_fn(model(X_val_t), y_val_t).item()
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if epoch == 1 or epoch == args.epochs or epoch % 50 == 0:
            rmse = float(torch.sqrt(loss).item())
            print(f"  epoch {epoch:4d}  train_rmse={rmse:.4f}")

    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()

    # 5b) Ridge: train on scaled features, delta-FPS target, select alpha on val
    # Scale data with train-only stats
    feat_mean_2d = feat_mean.squeeze(0)  # (1, n_feat) after squeeze(axis=0)
    feat_std_2d = feat_std.squeeze(0)
    X_train_scaled = (X_train.copy() - feat_mean_2d) / feat_std_2d
    X_val_scaled = (X_val - feat_mean_2d) / feat_std_2d if has_val else None
    X_test_scaled = (X_test - feat_mean_2d) / feat_std_2d

    X_train_flat = X_train_scaled.reshape(len(X_train), -1)  # (N, hist*n_feat)
    X_val_flat = X_val_scaled.reshape(len(X_val), -1) if has_val else None
    X_test_flat = X_test_scaled.reshape(len(X_test), -1)

    # last_fps: fps_avg at the anchor timestep (last history position)
    fps_idx = FEATURE_COLS.index("fps_avg")
    last_fps_train = X_train[:, -1, fps_idx]  # raw FPS at anchor
    last_fps_val = X_val[:, -1, fps_idx] if has_val else None
    last_fps_test = X_test[:, -1, fps_idx]

    ridge_coefs, ridge_alphas = _train_ridge(
        X_train_flat, y_train, last_fps_train,
        X_val_flat, y_val, last_fps_val, ALPHA_GRID,
    )
    y_pred_ridge = _ridge_predict(X_test_flat, last_fps_test, ridge_coefs)

    # 6) MLP + persistence predictions
    X_test_t = torch.from_numpy(X_test)
    with torch.no_grad():
        y_pred_mlp = model(X_test_t).numpy()
    y_pred_persist = persistence_predict(X_test, horizon_steps)
    y_true = y_test

    # 7) metrics: overall + per-session per-horizon
    unique_sids = sorted(set(sids_test))
    all_entries = []

    for h_idx, h_label in enumerate(horizon_labels):
        entry = _metrics_dict(
            y_true[:, h_idx], y_pred_mlp[:, h_idx], y_pred_persist[:, h_idx],
            y_pred_ridge[:, h_idx],
        )
        entry["horizon_s"] = args.horizons[h_idx]
        entry["horizon_label"] = h_label
        entry["ridge_alpha"] = float(ridge_alphas[h_idx])

        per_sess = {}
        for sid in unique_sids:
            mask = np.array([s == sid for s in sids_test])
            if not mask.any():
                continue
            per_sess[sid] = _metrics_dict(
                y_true[mask, h_idx],
                y_pred_mlp[mask, h_idx],
                y_pred_persist[mask, h_idx],
                y_pred_ridge[mask, h_idx],
            )
        entry["per_session"] = per_sess
        all_entries.append(entry)

    metrics_doc = {
        "model": "FPSForecaster (MLP) + Ridge regression",
        "input_features": FEATURE_COLS,
        "target": TARGET_COL,
        "target_definition": "2-second-bin-mean FPS (mean fps_avg over 4 consecutive 0.5s source rows)",
        "source_cadence_s": source_interval,
        "model_cadence_s": sample_interval,
        "aggregation_factor": group_size,
        "history_window_samples": args.history,
        "effective_history_s": args.history * sample_interval,
        "horizon_labels": horizon_labels,
        "horizon_steps": horizon_steps,
        "n_horizons": n_horizons,
        "seed": args.seed,
        "baselines": ["persistence"],
        "ridge": {
            "target": "delta-FPS (future - last_fps), then prediction = last_FPS + predicted_delta",
            "alpha_grid": ALPHA_GRID,
            "alpha_selection": "independent per horizon: minimum horizon RMSE on validation partition",
            "best_alphas": {f"h{horizon_steps[h]}": float(ridge_alphas[h]) for h in range(n_horizons)},
        },
        "horizons": all_entries,
        "session_ids": unique_sids,
        "data_stats": {
            "source_rows": all_stats["total_source_rows"],
            "model_cadence_bins": all_stats["total_agg_bins"],
            "dropped_rows_incomplete_tail": all_stats["total_dropped_rows"],
            "cadence_gaps_detected": all_stats["total_gaps"],
        },
    }

    args.output_metrics.parent.mkdir(parents=True, exist_ok=True)
    args.output_metrics.write_text(json.dumps(metrics_doc, indent=2))
    print(f"\n[metrics] wrote {args.output_metrics}")

    # 8) predictions CSV: one row per test window, per-horizon timestamps
    if len(sids_test) > 0:
        rows = []
        for i in range(len(sids_test)):
            row = {
                "session_id": sids_test[i],
                "segment_id": seg_ids_test[i],
                "sample_idx": i,
                "anchor_ts": round(anchor_ts_test[i], 3),
                "last_fps": round(float(last_fps_test[i]), 2),
            }
            # per-horizon target timestamps
            for h_idx in range(n_horizons):
                row[f"target_ts_h{h_idx}"] = round(target_ts_test[i][h_idx], 3)
            # actual and model predictions per horizon
            for h_idx in range(n_horizons):
                row[f"t+{args.horizons[h_idx]}s_actual"] = float(y_true[i, h_idx])
                row[f"t+{args.horizons[h_idx]}s_mlp"] = float(y_pred_mlp[i, h_idx])
                row[f"t+{args.horizons[h_idx]}s_ridge"] = float(y_pred_ridge[i, h_idx])
                row[f"t+{args.horizons[h_idx]}s_persistence"] = float(y_pred_persist[i, h_idx])
            rows.append(row)
        with open(args.output_predictions, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"[preds] wrote {args.output_predictions}")
    else:
        print("[preds] no test samples -- skipping predictions CSV")

    # 9) plot (add ridge to plots)
    if args.output_plot:
        _plot_actual_vs_predicted(
            y_true, y_pred_mlp, y_pred_persist, horizon_labels, args.output_plot,
            y_ridge=y_pred_ridge,
        )
        # also generate a ridge-only scatter for metrics audit
        try:
            import matplotlib
            import matplotlib.pyplot as plt
            fig, axes = plt.subplots(1, n_horizons, figsize=(5 * n_horizons, 4), squeeze=False)
            for h in range(n_horizons):
                ax = axes[0][h]
                ax.scatter(y_true[:, h], y_pred_ridge[:, h], alpha=0.4, s=8, label="Ridge")
                ax.scatter(y_true[:, h], y_pred_persist[:, h], alpha=0.4, s=8, label="Persistence")
                vmin = min(y_true[:, h].min(), y_pred_ridge[:, h].min(), y_pred_persist[:, h].min())
                vmax = max(y_true[:, h].max(), y_pred_ridge[:, h].max(), y_pred_persist[:, h].max())
                dur = abs(vmax - vmin) * 0.05 or 0.5
                ax.plot([vmin - dur, vmax + dur], [vmin - dur, vmax + dur], "k--", linewidth=0.5)
                ax.set_xlabel("Actual FPS")
                ax.set_ylabel("Predicted FPS")
                ax.set_title(f"{horizon_labels[h]} (Ridge)")
                ax.legend()
            fig.tight_layout()
            ridge_plot = args.output_plot.parent / (args.output_plot.stem + "_ridge" + args.output_plot.suffix)
            fig.savefig(ridge_plot, dpi=150)
            plt.close(fig)
            print(f"[plot] wrote {ridge_plot}")
        except Exception:
            pass

    # 10) ONNX export
    args.output_onnx.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, args.history, N_FEATURES, dtype=torch.float32)
    torch.onnx.export(
        model,
        dummy,
        args.output_onnx,
        input_names=["features"],
        output_names=["fps_predictions"],
        dynamic_axes={"features": {0: "batch"}, "fps_predictions": {0: "batch"}},
        opset_version=17,
    )
    print(f"[onnx] wrote {args.output_onnx}")

    # 11) summary
    print("\n=== SUMMARY ===")
    for entry in all_entries:
        delta_msvp = entry["mlp_rmse"] - entry["persistence_rmse"]
        label = entry["horizon_label"]
        print(
            f"  {label:>6s}: MLP MAE={entry['mlp_mae']:.3f} "
            f"RMSE={entry['mlp_rmse']:.3f}  "
            f"Ridge MAE={entry['ridge_mae']:.3f} "
            f"RMSE={entry['ridge_rmse']:.3f}  "
            f"Per MAE={entry['persistence_mae']:.3f} "
            f"RMSE={entry['persistence_rmse']:.3f}  "
            f"dRMSE_ridge={entry['ridge_rmse'] - entry['persistence_rmse']:+.3f} "
            f"(alpha={entry['ridge_alpha']:.1f})"
        )


# -- main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Train FPS forecaster")
    ap.add_argument(
        "--input-dir", type=Path,
        help="Directory with raw telemetry CSVs (recursive)",
    )
    ap.add_argument(
        "--output-onnx", type=Path,
        default=Path("models/fps_forecast.onnx"),
    )
    ap.add_argument(
        "--output-metrics", type=Path,
        default=Path("metrics/fps_forecast_metrics.json"),
    )
    ap.add_argument(
        "--output-predictions", type=Path,
        default=Path("metrics/fps_forecast_predictions.csv"),
    )
    ap.add_argument(
        "--output-plot", type=Path,
        default=Path("metrics/fps_forecast_plot.png"),
    )
    ap.add_argument(
        "--source-interval-s", type=float, default=0.5,
        help="Source telemetry cadence in seconds (default: 0.5)",
    )
    ap.add_argument(
        "--sample-interval-s", type=float, default=1.0,
        help="Model sample interval in seconds (default: 1.0, matches runtime HEALTH_INTERVAL)",
    )
    ap.add_argument("--history", type=int, default=5,
                    help="History length in model samples (5 @ 2 s = 10 s effective)")
    ap.add_argument("--horizons", type=float, nargs="+", default=[6.0, 10.0],
                    help="Forecast horizons in seconds (must be multiples of sample interval)")
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    if args.self_check:
        self_check()
        return

    if args.input_dir is None:
        ap.error("--input-dir is required (or use --self-check)")

    if args.history < 2:
        raise SystemExit("--history must be >= 2")
    if args.source_interval_s <= 0:
        raise SystemExit("--source-interval-s must be positive")
    if args.sample_interval_s <= 0:
        raise SystemExit("--sample-interval-s must be positive")
    if args.sample_interval_s < args.source_interval_s:
        raise SystemExit("--sample-interval-s must be >= --source-interval-s")

    _run_training(args)


if __name__ == "__main__":
    main()