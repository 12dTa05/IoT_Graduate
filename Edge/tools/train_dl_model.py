#!/usr/bin/env python3
"""Train a tiny 3-feature load predictor and export ONNX.

Input:  profile CSV produced by tools/profile_collect.py.
Output: models/load_predictor.onnx (3-feature sliding-window MLP).

Collection -> Training contract:
  • The collector writes load_score (composite health_agent._compute_load_score:
    weighted GPU/CPU/RAM + FPS-drop penalty, scale 0-100).
  • This script *defaults* to load_score as the prediction target.
    load_score IS the QoS-aware composite — it captures both resource load AND
    pipeline throughput (FPS) in a single value.
  • Use --target to override (accepts any column in the CSV).
  • GPU performance validation: check FPS at runtime against TARGET_FPS.
    Raw gpu_percent is only a diagnostic/metric — use --target gpu_percent
    if you deliberately want a raw-load model.

Fallback convention: if a CSV lacks load_score (legacy datasets), the script
falls through: actual_load -> gpu_percent. This is backward-compatible with
older calibration CSV formats.

Runtime DLPredictor does not add W_base post-hoc; the training target already
determines what the model predicts (raw or composite load).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional


FEATURE_ALIASES = {
    "n_track_mean": ["n_track_mean", "n_track_total"],
    "n_plate_mean": ["n_plate_mean", "n_plate_total"],
    "stationary_fraction_mean": ["stationary_fraction_mean", "stationary_fraction"],
}
TARGET_ALIASES = ["load_score", "actual_load", "gpu_percent"]
# load_score is the canonical composite target (GPU/CPU/RAM + FPS penalty).
# actual_load and gpu_percent are legacy fallbacks for CSVs without load_score.


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
    feature_cols = [
        _pick_column(df.columns, aliases, canonical)
        for canonical, aliases in FEATURE_ALIASES.items()
    ]
    target_col = target or _pick_column(df.columns, TARGET_ALIASES, "target")
    df = df.dropna(subset=feature_cols + [target_col]).reset_index(drop=True)
    if len(df) == 0:
        raise SystemExit("ERROR: no usable rows after dropping NaNs")
    return df, feature_cols, target_col


def _make_windows(df, feature_cols, target_col: str, window_k: int, horizon_rows: int):
    import numpy as np

    features = df[feature_cols].astype("float32").values
    target = df[target_col].astype("float32").values
    xs, ys = [], []
    end = len(df) - horizon_rows
    for i in range(window_k - 1, end):
        xs.append(features[i - window_k + 1:i + 1])
        ys.append(target[i + horizon_rows])
    if not xs:
        raise SystemExit(
            f"ERROR: not enough rows for window_k={window_k}, horizon_rows={horizon_rows}"
        )
    return np.asarray(xs, dtype="float32"), np.asarray(ys, dtype="float32").reshape(-1, 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Train/export 3-feature load predictor ONNX")
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--output", type=Path, default=Path("models/load_predictor.onnx"))
    ap.add_argument("--target", default=None,
                    help="Target column. Default: load_score (composite QoS load). "
                         "Falls back: actual_load -> gpu_percent if column missing.")
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
        nn.Linear(args.window_k * 3, 16),
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
    dummy = torch.zeros(1, args.window_k, 3, dtype=torch.float32)
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

    print(f"[train_dl] rows={len(df)} samples={len(x_np)}")
    print(f"[train_dl] features={feature_cols} target={target_col}")
    print(f"[train_dl] wrote {args.output}")


if __name__ == "__main__":
    main()
