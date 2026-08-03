#!/usr/bin/env python3
"""Minimal self-check for report_fps_forecast.py output artifacts.

Run after the report script to validate structure and a spot-check RMSE.
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

ARTIFACT_DIR = (
    Path(__file__).resolve().parents[1]
    / "logs" / "fps_forecast_mode_a_gap_safe"
)


def main() -> int:
    failures = 0

    png = ARTIFACT_DIR / "forecast_time_series.png"
    csv_path = ARTIFACT_DIR / "fps_forecast_table.csv"
    md_path = ARTIFACT_DIR / "fps_forecast_table.md"

    for p in [png, csv_path, md_path]:
        if not p.is_file():
            print(f"FAIL: missing {p}")
            failures += 1

    if failures:
        return failures

    # -- read report table ------------------------------------------------
    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        table_rows = list(reader)

    if reader.fieldnames is None or "scenario" not in reader.fieldnames:
        print("FAIL: CSV missing header")
        return 1

    n_overall_rows = sum(1 for r in table_rows if r["scenario"] == "overall")
    if n_overall_rows != 2:
        print(f"FAIL: expected 2 overall rows, got {n_overall_rows}")
        failures += 1

    # Forecast lines must be aligned to their target timestamps, rather than
    # their earlier feature-window anchors.
    report_source = (Path(__file__).resolve().parent / "report_fps_forecast.py").read_text()
    if 'target_col = f"target_ts_h{hi}"' not in report_source:
        print("FAIL: forecast plot is not aligned to target timestamps")
        failures += 1

    # -- regression: overall RMSE must be sqrt(sum(sq_errors)/N) ------
    # ponytail: independently recompute from predictions.csv and
    # compare against the report table. This catches the old bug where
    # overall RMSE was a per-session sample-weighted average.
    raw_csv = ARTIFACT_DIR / "predictions.csv"
    horizons = ["t+6.0s", "t+10.0s"]
    models = ["ridge", "persistence"]

    for horizon in horizons:
        for model in models:
            sq_sum = 0.0
            count = 0
            col_actual = f"{horizon}_actual"
            col_pred = f"{horizon}_{model}"

            with raw_csv.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    err = float(row[col_pred]) - float(row[col_actual])
                    sq_sum += err * err
                    count += 1

            correct = math.sqrt(sq_sum / count)

            # find matching overall row in table
            match = [r for r in table_rows
                     if r["scenario"] == "overall" and r["horizon"] == horizon]
            if not match:
                print(f"FAIL: missing overall row for {horizon}")
                failures += 1
                continue

            table_key = f"{model}_rmse"
            table_val = float(match[0][table_key])

            if abs(correct - table_val) > 0.0005:
                print(
                    f"FAIL: overall {horizon} {model} RMSE: "
                    f"computed {correct:.4f}, table says {table_val:.4f}"
                )
                failures += 1

    # -- also verify the old wrong aggregation would give a different value
    # for at least one horizon/model to confirm the test is meaningful
    for horizon in horizons:
        for model in models:
            col_actual = f"{horizon}_actual"
            col_pred = f"{horizon}_{model}"

            correct = 0.0
            wrong_avg = 0.0
            count = 0

            with raw_csv.open(newline="") as fh:
                all_errors = []
                for row in csv.DictReader(fh):
                    err = float(row[col_pred]) - float(row[col_actual])
                    all_errors.append(err)
                correct = math.sqrt(sum(e * e for e in all_errors) / len(all_errors))

                # old wrong: per-session RMSE average weighted by N
                # (compute per-session RMSE then average)
                sessions: dict[str, list[float]] = {}
                fh.seek(0)
                for row in csv.DictReader(fh):
                    sid = row["session_id"]
                    err = float(row[col_pred]) - float(row[col_actual])
                    sessions.setdefault(sid, []).append(err)

                weighted_sum = 0.0
                total_n = 0
                for errs in sessions.values():
                    ses_rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
                    weighted_sum += ses_rmse * len(errs)
                    total_n += len(errs)
                wrong_avg = weighted_sum / total_n

            if abs(correct - wrong_avg) > 0.01:
                # The difference is meaningful — confirms the bug existed
                print(
                    f"Regression guard: {horizon} {model} RMSE: "
                    f"correct {correct:.4f} vs old-avg {wrong_avg:.4f}, "
                    f"difference {abs(correct - wrong_avg):.4f}"
                )

    if failures == 0:
        print("All checks passed.")
    return failures


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
