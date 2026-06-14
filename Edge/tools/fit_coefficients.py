#!/usr/bin/env python3
"""
Edge/tools/fit_coefficients.py

Phase 1 — Offline Regression: fit α₁, (α₂), β, γ from calibration CSV.

Reads the CSV produced by profile_collect.py, fits two models:
    Model A (linear):    ΔLoad = α₁·N_track + β·N_plate + γ·S
    Model B (quadratic): ΔLoad = α₁·N_track + α₂·N_track² + β·N_plate + γ·S

Compares hold-out RMSE and chooses the winner by data, not assumption.
Prints the coefficients and writes them to edge_node.yml p2p.proactive section.

Usage:
    python3 tools/fit_coefficients.py \\
        --csv       logs/calibration.csv \\
        --wbase     12.5 \\
        --output    configs/edge_node.yml \\
        --test-frac 0.2

    --csv       Calibration CSV from profile_collect.py
    --wbase     W_base GPU%% (idle load; read from logs/wbase.txt or measure manually)
    --output    edge_node.yml to patch with fitted coefficients
    --test-frac Fraction of data held out for RMSE evaluation (default 0.2)
    --target    Target column for regression (default: delta_load)
    --dry-run   Print coefficients but do not write to edge_node.yml

Requirements: scikit-learn, numpy, pandas  (pip install scikit-learn pandas)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------

def _load_data(csv_path: Path, wbase: float, target_col: str):
    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas required — pip install pandas", file=sys.stderr)
        sys.exit(1)

    df = pd.read_csv(csv_path)
    required = ["n_track_total", "n_track_sq_total", "n_plate_total",
                "stationary_fraction_mean", target_col]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"ERROR: CSV missing columns: {missing}", file=sys.stderr)
        sys.exit(1)

    # If delta_load column was computed with a different wbase, recompute
    if target_col == "delta_load" and wbase > 0:
        df["delta_load"] = df["gpu_percent"] - wbase

    # Drop rows with NaN in required columns
    df = df.dropna(subset=required)
    print(f"[fit] Loaded {len(df)} rows from {csv_path}")
    return df


def _fit_model(X, y, feature_names):
    """Fit OLS regression, return (coefs_dict, train_rmse)."""
    try:
        from sklearn.linear_model import LinearRegression
        import numpy as np
    except ImportError:
        print("ERROR: scikit-learn required — pip install scikit-learn", file=sys.stderr)
        sys.exit(1)

    import numpy as np
    model = LinearRegression(fit_intercept=False)
    model.fit(X, y)
    preds = model.predict(X)
    rmse  = float(np.sqrt(((y - preds) ** 2).mean()))
    coefs = {name: float(c) for name, c in zip(feature_names, model.coef_)}
    return coefs, rmse, model


def _evaluate(model, X_test, y_test):
    import numpy as np
    preds = model.predict(X_test)
    rmse  = float(np.sqrt(((y_test - preds) ** 2).mean()))
    return rmse


def _patch_yaml(yml_path: Path, coeffs: dict, w_base: float) -> None:
    """
    Write fitted coefficients into the proactive: block of edge_node.yml.
    If the block already exists it is replaced; if not, it is appended.
    Uses plain text replacement to avoid disturbing YAML comments.
    """
    text = yml_path.read_text(encoding="utf-8") if yml_path.exists() else ""

    block_lines = [
        "  # -----------------------------------------------------------------------",
        "  # Proactive load model — coefficients fitted by tools/fit_coefficients.py",
        "  # -----------------------------------------------------------------------",
        "  proactive:",
        "    enabled: false          # set true to drive offload from risk_index",
        f"    w_base: {round(w_base, 2)}",
        f"    alpha1: {round(coeffs.get('alpha1', 0.0), 6)}",
        f"    alpha2: {round(coeffs.get('alpha2', 0.0), 6)}    "
        f"# 0.0 = linear model chosen by held-out RMSE",
        f"    beta:   {round(coeffs.get('beta', 0.0), 6)}",
        f"    gamma:  {round(coeffs.get('gamma', 0.0), 6)}",
        "    cycle_window_s: 90.0    # sliding average ≈ one signal cycle",
        "    risk_threshold: 0.85    # U >= this triggers offload",
        "    theta_thermal:",
        "      t_low:   75.0         # °C — ramp start",
        "      t_high:  90.0         # °C — ramp end (full penalty)",
        "      max_mult: 1.25        # multiplier at t_high",
    ]
    block = "\n".join(block_lines) + "\n"

    import re
    # Replace existing proactive: block if present
    pattern = re.compile(
        r"  # -+\n  # Proactive load model.*?(?=\n  [a-z#]|\Z)",
        re.DOTALL,
    )
    if pattern.search(text):
        text = pattern.sub(block.rstrip("\n"), text)
    else:
        # Append after the p2p: block
        text = text.rstrip("\n") + "\n\n" + block

    yml_path.write_text(text, encoding="utf-8")
    print(f"[fit] Patched {yml_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Fit proactive load model coefficients")
    ap.add_argument("--csv",       type=Path, required=True)
    ap.add_argument("--wbase",     type=float, default=0.0,
                    help="W_base GPU%% (idle load, from logs/wbase.txt)")
    ap.add_argument("--output",    type=Path,
                    default=Path(__file__).resolve().parents[1] / "configs" / "edge_node.yml")
    ap.add_argument("--test-frac", type=float, default=0.2)
    ap.add_argument("--target",    default="delta_load")
    ap.add_argument("--dry-run",   action="store_true")
    args = ap.parse_args()

    try:
        import numpy as np
        from sklearn.model_selection import train_test_split
    except ImportError:
        print("ERROR: numpy + scikit-learn required", file=sys.stderr)
        sys.exit(1)

    df = _load_data(args.csv, args.wbase, args.target)

    feats_linear = ["n_track_total", "n_plate_total", "stationary_fraction_mean"]
    feats_quad   = ["n_track_total", "n_track_sq_total", "n_plate_total",
                    "stationary_fraction_mean"]

    X_lin  = df[feats_linear].values
    X_quad = df[feats_quad].values
    y      = df[args.target].values

    X_lin_tr,  X_lin_te,  y_tr, y_te = train_test_split(
        X_lin, y, test_size=args.test_frac, random_state=42)
    X_quad_tr, X_quad_te, _,    _    = train_test_split(
        X_quad, y, test_size=args.test_frac, random_state=42)

    coefs_lin,  train_rmse_lin,  model_lin  = _fit_model(
        X_lin_tr,  y_tr, ["alpha1", "beta", "gamma"])
    coefs_quad, train_rmse_quad, model_quad = _fit_model(
        X_quad_tr, y_tr, ["alpha1", "alpha2", "beta", "gamma"])

    test_rmse_lin  = _evaluate(model_lin,  X_lin_te,  y_te)
    test_rmse_quad = _evaluate(model_quad, X_quad_te, y_te)

    print("\n── Model A (linear) ──────────────────────────────────────────────")
    for k, v in coefs_lin.items():
        print(f"   {k:8s} = {v:+.6f}")
    print(f"   Train RMSE = {train_rmse_lin:.4f}   Test RMSE = {test_rmse_lin:.4f}")

    print("\n── Model B (quadratic) ───────────────────────────────────────────")
    for k, v in coefs_quad.items():
        print(f"   {k:8s} = {v:+.6f}")
    print(f"   Train RMSE = {train_rmse_quad:.4f}   Test RMSE = {test_rmse_quad:.4f}")

    # Decision: keep α₂ only if quadratic improves test RMSE by >5%
    improvement = (test_rmse_lin - test_rmse_quad) / max(test_rmse_lin, 1e-9)
    if improvement > 0.05:
        chosen, label = coefs_quad, "quadratic (α₂ retained)"
    else:
        coefs_quad_zeroed = dict(coefs_quad)
        coefs_quad_zeroed["alpha2"] = 0.0
        chosen, label = coefs_quad_zeroed, "linear (α₂=0, improvement insufficient)"
        # Use linear coefficients for α₁/β/γ
        chosen["alpha1"] = coefs_lin["alpha1"]
        chosen["beta"]   = coefs_lin["beta"]
        chosen["gamma"]  = coefs_lin["gamma"]

    print(f"\n✓ Chosen: {label}")
    print(f"  alpha1={chosen['alpha1']:+.6f}  alpha2={chosen['alpha2']:+.6f}  "
          f"beta={chosen['beta']:+.6f}  gamma={chosen['gamma']:+.6f}")
    print(f"  w_base={args.wbase}")

    if not args.dry_run:
        _patch_yaml(args.output, chosen, args.wbase)
    else:
        print("[fit] --dry-run: edge_node.yml not modified")


if __name__ == "__main__":
    main()
