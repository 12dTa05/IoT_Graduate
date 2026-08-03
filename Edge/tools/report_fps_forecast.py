#!/usr/bin/env python3
"""Bounded Step-1 evaluation report for FPS forecasting.

Owns only this script and generated artifacts under
Edge/logs/fps_forecast_mode_a_gap_safe/.
Does NOT modify training/model/runtime code.

Produces:
  - Time-series PNG of actual FPS + Ridge/persistence forecasts per scenario
  - CSV table of per-scenario and overall metrics
  - Compact Markdown summary with evidence caveat
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# -- lazily import heavy deps after arg validation ------------------------
# ponytail: Agg backend prevents display-server trips on headless systems
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

DEFAULT_METRICS = (
    Path(__file__).resolve().parents[1]
    / "logs" / "fps_forecast_mode_a_gap_safe" / "metrics.json"
)
DEFAULT_PREDICTIONS = (
    Path(__file__).resolve().parents[1]
    / "logs" / "fps_forecast_mode_a_gap_safe" / "predictions.csv"
)
DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parents[1]
    / "logs" / "fps_forecast_mode_a_gap_safe"
)

HORIZON_COLS = ["t+6.0s", "t+10.0s"]

# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _validate_predictions(path: Path) -> list[dict]:
    if not path.is_file():
        _fail(f"predictions file not found: {path}")

    rows: list[dict] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames
        if fieldnames is None:
            _fail("predictions CSV has no header row")

        required = {
            "session_id", "sample_idx", "anchor_ts",
            "t+6.0s_actual", "t+6.0s_ridge", "t+6.0s_persistence",
            "t+10.0s_actual", "t+10.0s_ridge", "t+10.0s_persistence",
        }
        missing = required - set(fieldnames)
        if missing:
            _fail(f"predictions CSV missing columns: {', '.join(sorted(missing))}")

        for row in reader:
            rows.append(row)

    if not rows:
        _fail("predictions CSV contains no data rows")
    return rows


def _validate_metrics(path: Path) -> dict:
    if not path.is_file():
        _fail(f"metrics file not found: {path}")
    with path.open() as fh:
        data = json.load(fh)
    needed = ["session_ids", "horizons"]
    for key in needed:
        if key not in data:
            _fail(f"metrics.json missing key: '{key}'")
    for h in data["horizons"]:
        if "horizon_label" not in h:
            _fail("metrics.json horizon entry missing 'horizon_label'")
        if "per_session" not in h:
            _fail("metrics.json horizon entry missing 'per_session'")
    return data


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _first_anchor_ts(group: list[dict]) -> float:
    return float(group[0]["anchor_ts"])


# ---------------------------------------------------------------------------
# PNG time-series plots
# ---------------------------------------------------------------------------

def plot_forecasts(
    rows: list[dict],
    output_dir: Path,
) -> None:
    sessions = sorted(set(r["session_id"] for r in rows))
    n_sessions = len(sessions)
    n_horizons = len(HORIZON_COLS)

    fig, axes = plt.subplots(
        nrows=n_horizons,
        ncols=n_sessions,
        figsize=(3.5 * n_sessions, 4 * n_horizons),
        squeeze=False,
    )

    for hi, horizon in enumerate(HORIZON_COLS):
        col_actual = f"{horizon}_actual"
        col_ridge = f"{horizon}_ridge"
        col_pers = f"{horizon}_persistence"

        for ci, session in enumerate(sessions):
            ax = axes[hi][ci]
            group = [r for r in rows if r["session_id"] == session]
            group.sort(key=lambda r: float(r["anchor_ts"]))
            target_col = f"target_ts_h{hi}"
            if target_col not in group[0]:
                _fail(f"predictions CSV missing column: {target_col}")
            t0 = float(group[0][target_col])

            # Forecasts and their actual outcomes belong at the target time,
            # not the earlier feature-window anchor.
            xs = [float(r[target_col]) - t0 for r in group]
            actuals = [float(r[col_actual]) for r in group]
            ridge = [float(r[col_ridge]) for r in group]
            persi = [float(r[col_pers]) for r in group]

            # Markers at exact sample points — no interpolation, no invention.
            ax.plot(xs, actuals, "o-", color="black", label="Actual", ms=5, linewidth=0.8)
            ax.plot(xs, ridge, "s--", color="steelblue", label="Ridge", ms=5, linewidth=0.8)
            ax.plot(xs, persi, "^:", color="darkorange", label="Persistence", ms=5, linewidth=0.8)

            ax.set_title(f"{session} / {horizon}")
            ax.set_xlabel("Relative seconds from first forecast target")
            ax.set_ylabel("FPS (2 s bin mean)")
            ax.legend(fontsize="x-small")

    fig.suptitle(
        "FPS Forecasts — chronological held-out windows per scenario",
        fontsize=13,
        y=1.01,
    )
    fig.tight_layout()
    out_path = output_dir / "forecast_time_series.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out_path}")


# ---------------------------------------------------------------------------
# table computation
# ---------------------------------------------------------------------------

def compute_metrics(rows: list[dict], sessions: list[str]) -> list[dict]:
    """Return per-session and overall table rows with win/tie/loss verdicts."""

    def _verdict(ridge_val: float, pers_val: float) -> str:
        if ridge_val < pers_val:
            return "win"
        if ridge_val > pers_val:
            return "loss"
        return "tie"

    table_rows: list[dict] = []

    for horizon_label in HORIZON_COLS:
        col_actual = f"{horizon_label}_actual"
        col_ridge = f"{horizon_label}_ridge"
        col_pers = f"{horizon_label}_persistence"

        # ponytail: collect raw errors for correct overall RMSE = sqrt(sum(sq_err)/N)
        all_sq_ridge = 0.0
        all_sq_pers = 0.0
        all_abs_ridge = 0.0
        all_abs_pers = 0.0
        all_n = 0

        for sid in sessions:
            group = [r for r in rows if r["session_id"] == sid]
            actuals = np.array([float(r[col_actual]) for r in group])
            ridge_preds = np.array([float(r[col_ridge]) for r in group])
            pers_preds = np.array([float(r[col_pers]) for r in group])

            errors_ridge = ridge_preds - actuals
            errors_pers = pers_preds - actuals

            mae_ridge = float(np.mean(np.abs(errors_ridge)))
            rmse_ridge = float(np.sqrt(np.mean(errors_ridge ** 2)))
            mae_pers = float(np.mean(np.abs(errors_pers)))
            rmse_pers = float(np.sqrt(np.mean(errors_pers ** 2)))

            table_rows.append({
                "scenario": sid,
                "horizon": horizon_label,
                "n_samples": len(group),
                "ridge_mae": round(mae_ridge, 4),
                "ridge_rmse": round(rmse_ridge, 4),
                "persistence_mae": round(mae_pers, 4),
                "persistence_rmse": round(rmse_pers, 4),
                "mae_verdict": _verdict(mae_ridge, mae_pers),
                "rmse_verdict": _verdict(rmse_ridge, rmse_pers),
            })

            gn = len(group)
            all_n += gn
            all_abs_ridge += sum(abs(errors_ridge))
            all_abs_pers += sum(abs(errors_pers))
            all_sq_ridge += float(np.sum(errors_ridge ** 2))
            all_sq_pers += float(np.sum(errors_pers ** 2))

        # overall row: MAE = sum(abs) / N; RMSE = sqrt(sum(sq) / N)
        table_rows.append({
            "scenario": "overall",
            "horizon": horizon_label,
            "n_samples": all_n,
            "ridge_mae": round(all_abs_ridge / all_n, 4),
            "ridge_rmse": round(float(np.sqrt(all_sq_ridge / all_n)), 4),
            "persistence_mae": round(all_abs_pers / all_n, 4),
            "persistence_rmse": round(float(np.sqrt(all_sq_pers / all_n)), 4),
            "mae_verdict": _verdict(all_abs_ridge, all_abs_pers),
            "rmse_verdict": _verdict(float(np.sqrt(all_sq_ridge / all_n)), float(np.sqrt(all_sq_pers / all_n))),
        })

    return table_rows


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_csv(table_rows: list[dict], output_dir: Path) -> None:
    fields = [
        "scenario", "horizon", "n_samples",
        "ridge_mae", "ridge_rmse",
        "persistence_mae", "persistence_rmse",
        "mae_verdict", "rmse_verdict",
    ]
    out_path = output_dir / "fps_forecast_table.csv"
    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(table_rows)
    print(f"Wrote {out_path}")


# ---------------------------------------------------------------------------
# Markdown output
# ---------------------------------------------------------------------------

def write_markdown(table_rows: list[dict], output_dir: Path) -> None:
    out_path = output_dir / "fps_forecast_table.md"
    with out_path.open("w") as fh:
        fh.write("# FPS Forecast Performance — Chronological Held-Out Windows\n\n")
        fh.write(
            "Evidence caveat: six recorded Mode-A sessions / 41 test windows, "
            "temporal holdout only — "
            "these numbers do not constitute deployment or generalization evidence.\n\n"
        )

        fh.write(
            "| Scenario | Horizon | N | Ridge MAE | Ridge RMSE | "
            "Persist. MAE | Persist. RMSE | MAE Verdict | RMSE Verdict |\n"
        )
        fh.write("|" + "---|" * 9 + "\n")

        for r in table_rows:
            fh.write(
                f"| {r['scenario']} | {r['horizon']} | {r['n_samples']} | "
                f"{r['ridge_mae']} | {r['ridge_rmse']} | "
                f"{r['persistence_mae']} | {r['persistence_rmse']} | "
                f"{r['mae_verdict']} | {r['rmse_verdict']} |\n"
            )
    print(f"Wrote {out_path}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="FPS forecast Step-1 report")
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS,
                        help="Path to metrics.json")
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS,
                        help="Path to predictions.csv")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="Output directory for report artifacts")

    args = parser.parse_args()

    # validate inputs
    _validate_metrics(args.metrics)
    rows = _validate_predictions(args.predictions)

    # ensure output directory exists
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # 2) time-series PNG
    plot_forecasts(rows, args.output_dir)

    # 3) + 4) compute & write table
    sessions = sorted(set(r["session_id"] for r in rows))
    table_rows = compute_metrics(rows, sessions)
    write_csv(table_rows, args.output_dir)
    write_markdown(table_rows, args.output_dir)

    print("Report complete.")


if __name__ == "__main__":
    main()
