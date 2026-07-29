#!/usr/bin/env python3
"""Train a 4-feature node-level load predictor and export ONNX.

Input:  raw collector CSV from profile_collect.py, or cleaned CSV from
        Edge/tools/clean_collected_csvs.py (both accepted).
Output: models/load_predictor.onnx (4-feature sliding-window MLP).

Model input features (node-level, no per-camera division):
    n_active_cameras        — number of sources delivering frames (fps > 0)
    n_track_total           — total tracked vehicles across all cameras
    n_plate_total           — total plate detections across all cameras
    stationary_fraction_mean — mean fraction of stationary vehicles

Runtime DLPredictor uses the same four features in the same order.
Do not pass --camera-count; per-camera division is not used.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional


FEATURE_COLS = [
    "n_active_cameras",
    "n_track_total",
    "n_plate_total",
    "stationary_fraction_mean",
]
N_FEATURES = len(FEATURE_COLS)

TARGET_ALIASES = ["load_score_smoothed", "load_score_raw", "load_score",
                  "actual_load", "gpu_percent"]
# load_score_smoothed is the canonical target from clean_collected_csvs.py.
# Remaining aliases are legacy fallbacks.


def _pick_column(columns, names: List[str], label: str) -> str:
    for name in names:
        if name in columns:
            return name
    raise SystemExit(f"ERROR: CSV missing {label}; tried {names}")


def _load_csv(csv_path: Path, target: Optional[str]):
    try:
        import pandas as pd
    except ImportError:
        raise SystemExit("ERROR: pandas required — pip install pandas")

    df = pd.read_csv(csv_path)

    missing = [c for c in FEATURE_COLS if c not in df.columns]
    if missing:
        raise SystemExit(
            f"ERROR: CSV missing required feature column(s): {missing}\n"
            f"Re-collect data with the current profile_collect.py which writes "
            f"n_active_cameras, then re-clean with clean_collected_csvs.py."
        )

    target_col = target or _pick_column(df.columns, TARGET_ALIASES, "target")
    df = df.dropna(subset=FEATURE_COLS + [target_col]).reset_index(drop=True)
    if len(df) == 0:
        raise SystemExit("ERROR: no usable rows after dropping NaNs")
    return df, FEATURE_COLS, target_col


def _make_windows(df, feature_cols, target_col: str, window_k: int, horizon_rows: int):
    import numpy as np

    has_case = "case_name" in df.columns
    groups = df.groupby("case_name") if has_case else [(None, df)]

    all_xs, all_ys = [], []
    for _, group in groups:
        features = group[feature_cols].astype("float32").values
        target = group[target_col].astype("float32").values
        end = len(group) - horizon_rows
        for i in range(window_k - 1, end):
            all_xs.append(features[i - window_k + 1:i + 1])
            all_ys.append(target[i + horizon_rows])

    if not all_xs:
        raise SystemExit(
            f"ERROR: not enough rows for window_k={window_k}, horizon_rows={horizon_rows}"
        )
    return np.asarray(all_xs, dtype="float32"), np.asarray(all_ys, dtype="float32").reshape(-1, 1)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Train/export 4-feature node-level load predictor ONNX.\n\n"
            "Features: n_active_cameras, n_track_total, n_plate_total, "
            "stationary_fraction_mean.\n"
            "No per-camera division is applied; totals are node-level inputs."
        )
    )
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--output", type=Path, default=Path("models/load_predictor.onnx"))
    ap.add_argument("--target", default=None,
                    help="Target column. Default: load_score_smoothed. "
                         "Falls back: load_score_raw -> load_score -> actual_load -> gpu_percent.")
    ap.add_argument("--window-k", type=int, default=5)
    ap.add_argument("--horizon-rows", type=int, default=1,
                    help="Predict this many rows ahead; use rows equivalent to your horizon_s")
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--lr", type=float, default=1e-3)
    args = ap.parse_args()

    if args.window_k < 1 or args.horizon_rows < 1:
        raise SystemExit("ERROR: --window-k and --horizon-rows must be >= 1")

    try:
        import numpy as np
        import torch
        from torch import nn
    except ImportError:
        raise SystemExit("ERROR: numpy + torch required — pip install numpy torch")

    df, feature_cols, target_col = _load_csv(args.csv, args.target)
    x_np, y_np = _make_windows(df, feature_cols, target_col, args.window_k, args.horizon_rows)

    x = torch.from_numpy(x_np)
    y = torch.from_numpy(y_np)

    model = nn.Sequential(
        nn.Flatten(),
        nn.Linear(args.window_k * N_FEATURES, 16),
        nn.ReLU(),
        nn.Linear(16, 1),
    )
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    loss_fn = nn.MSELoss()

    for epoch in range(1, args.epochs + 1):
        opt.zero_grad()
        pred = model(x)
        loss = loss_fn(pred, y)
        loss.backward()
        opt.step()
        if epoch == 1 or epoch == args.epochs or epoch % 50 == 0:
            rmse = float(torch.sqrt(loss).detach().cpu())
            print(f"epoch={epoch:4d} rmse={rmse:.4f}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    model.eval()
    dummy = torch.zeros(1, args.window_k, N_FEATURES, dtype=torch.float32)
    try:
        torch.onnx.export(
            model,
            dummy,
            args.output,
            input_names=["features"],
            output_names=["load"],
            dynamic_axes={"features": {0: "batch"}, "load": {0: "batch"}},
            opset_version=17,
        )
    except Exception as exc:
        raise SystemExit(f"ERROR: ONNX export failed: {exc}")

    case_note = f" cases={df.case_name.nunique()}" if "case_name" in df.columns else ""
    print(f"[train_dl] rows={len(df)}{case_note} samples={len(x_np)}")
    print(f"[train_dl] features={feature_cols} target={target_col}")
    print(f"[train_dl] wrote {args.output}")


if __name__ == "__main__":
    main()
