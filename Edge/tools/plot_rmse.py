#!/usr/bin/env python3
"""
Edge/tools/plot_rmse.py

Phase 4 — Chart 1: Prediction vs Ground Truth (RMSE evaluation).

Reads the calibration CSV produced by profile_collect.py, re-runs the
ProactiveModel formula using the coefficients fitted by fit_coefficients.py
(loaded from edge_node.yml), and plots:

  • Scatter: predicted risk_index_instant vs normalised actual GPU load
  • Time-series: predicted U (smoothed) vs actual GPU load over time
  • RMSE annotation

Usage:
    python3 tools/plot_rmse.py \\
        --csv   logs/calibration.csv \\
        --cfg   configs/edge_node.yml \\
        --out   logs/chart1_rmse.png

Requirements: matplotlib, pandas, numpy  (pip install matplotlib pandas numpy)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

try:
    from ._plot_helpers import _require, _build_feature_stats, _build_metrics, ProactiveModel, CycleSmoother
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _plot_helpers import _require, _build_feature_stats, _build_metrics, ProactiveModel, CycleSmoother

pd  = _require("pandas")
np  = _require("numpy")
plt = _require("matplotlib.pyplot")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cfg(yml_path: Path) -> dict:
    try:
        import yaml
        raw = yaml.safe_load(yml_path.read_text()) or {}
        return raw.get("proactive", {})
    except Exception as exc:
        print(f"ERROR loading {yml_path}: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Chart 1: Prediction vs Ground Truth")
    ap.add_argument("--csv", type=Path, required=True,
                    help="Calibration CSV from profile_collect.py")
    ap.add_argument("--cfg", type=Path,
                    default=Path(__file__).resolve().parents[1] / "configs" / "edge_node.yml")
    ap.add_argument("--out", type=Path, default=Path("logs/chart1_rmse.png"))
    ap.add_argument("--wbase", type=float, default=None,
                    help="Override W_base (default: read from edge_node.yml)")
    args = ap.parse_args()

    proactive_cfg = _load_cfg(args.cfg)
    if not proactive_cfg:
        print("WARNING: no proactive: block in edge_node.yml — using defaults", file=sys.stderr)

    if args.wbase is not None:
        proactive_cfg["w_base"] = args.wbase

    # Temporarily force enabled=True so ProactiveModel.compute() returns fields
    proactive_cfg["enabled"] = True

    df = pd.read_csv(args.csv)
    print(f"[plot_rmse] Loaded {len(df)} rows from {args.csv}")

    model = ProactiveModel(proactive_cfg)
    window_s = float(proactive_cfg.get("cycle_window_s", 90.0))
    smoother = CycleSmoother(window_s)

    predicted_instant = []
    predicted_smooth  = []
    actual_norm       = []        # GPU% / 100 as ground truth
    timestamps        = []

    for _, row in df.iterrows():
        metrics      = _build_metrics(row)
        feat_stats   = _build_feature_stats(row)
        ts           = float(row.get("ts", 0.0))

        result       = model.compute(metrics, feat_stats, ts=ts)
        u_instant    = result["risk_index_instant"]
        u_smooth     = smoother.update(u_instant, ts=ts)
        ground_truth = metrics["gpu_percent"] / 100.0

        predicted_instant.append(u_instant)
        predicted_smooth.append(u_smooth)
        actual_norm.append(ground_truth)
        timestamps.append(ts)

    predicted_instant = np.array(predicted_instant)
    predicted_smooth  = np.array(predicted_smooth)
    actual_norm       = np.array(actual_norm)

    rmse_instant = float(np.sqrt(np.mean((predicted_instant - actual_norm) ** 2)))
    rmse_smooth  = float(np.sqrt(np.mean((predicted_smooth  - actual_norm) ** 2)))

    print(f"[plot_rmse] RMSE (instant): {rmse_instant:.4f}")
    print(f"[plot_rmse] RMSE (smoothed): {rmse_smooth:.4f}")

    # ── Figure ────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle("Chart 1 — Proactive Model: Prediction vs Ground Truth", fontsize=13)

    # Left: scatter (instant)
    ax = axes[0]
    ax.scatter(actual_norm, predicted_instant, s=8, alpha=0.5,
               color="#2196F3", label="Predicted U (instant)")
    lo, hi = min(actual_norm.min(), predicted_instant.min()), \
             max(actual_norm.max(), predicted_instant.max())
    ax.plot([lo, hi], [lo, hi], "r--", linewidth=1.2, label="Ideal (y=x)")
    ax.set_xlabel("Ground Truth (GPU% / 100)")
    ax.set_ylabel("Predicted U (instant)")
    ax.set_title(f"Scatter  —  RMSE = {rmse_instant:.4f}")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    # Right: time-series
    ax2 = axes[1]
    t_rel = np.array(timestamps) - timestamps[0]   # seconds from start
    ax2.plot(t_rel, actual_norm,       color="#F44336", linewidth=1.0,
             label="Ground Truth (GPU% / 100)")
    ax2.plot(t_rel, predicted_smooth,  color="#2196F3", linewidth=1.2,
             label=f"Predicted U (smoothed {window_s:.0f}s)")
    ax2.set_xlabel("Time (s)")
    ax2.set_ylabel("Normalised Load / Risk Index")
    ax2.set_title(f"Time Series  —  RMSE (smoothed) = {rmse_smooth:.4f}")
    ax2.legend(fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.set_ylim(0, 1.05)

    plt.tight_layout()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(args.out, dpi=150)
    print(f"[plot_rmse] Saved → {args.out}")
    plt.show()


if __name__ == "__main__":
    main()
