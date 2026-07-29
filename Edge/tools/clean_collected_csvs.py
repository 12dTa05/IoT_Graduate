#!/usr/bin/env python3
"""Clean performance collector CSVs into per-case and combined datasets.

Defaults target the csv_collected/ → Edge/logs/cleaned/ pipeline.
Excludes p2p_migrations.csv; skips unrecognized schemas.
Produces <case>_clean.csv (per-case) and load_prediction_clean.csv (combined).

Expected input schema (profile_collect.py output):
    ts, gpu_percent, cpu_percent, ram_percent, gpu_temp_c,
    fps_avg, n_active_cameras,
    n_track_total, n_track_sq_total, n_plate_total,
    stationary_fraction_mean, load_score, delta_load
"""

import argparse
import csv
import pathlib
from collections import deque


PERF_HEADER = frozenset({
    "ts", "gpu_percent", "cpu_percent", "ram_percent", "gpu_temp_c",
    "fps_avg", "n_active_cameras",
    "n_track_total", "n_track_sq_total", "n_plate_total",
    "stationary_fraction_mean", "load_score", "delta_load",
})

OUT_HEADER = [
    "case_name", "sample_index", "ts", "elapsed_s",
    "gpu_percent", "cpu_percent", "ram_percent", "gpu_temp_c",
    "fps_avg", "n_active_cameras",
    "n_track_total", "n_track_sq_total", "n_plate_total",
    "stationary_fraction_mean", "load_score_raw", "delta_load",
    "load_score_smoothed", "fps_deficit", "fps_drop", "fps_severe_drop",
]

TARGET_FPS = 25.0


def _float_or_none(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _smooth(values: list[float | None], window: int = 3) -> list[float | None]:
    """Causal trailing mean (min_periods=1)."""
    if not values:
        return []
    result: list[float | None] = []
    buf: deque[float] = deque()
    buf_sum = 0.0
    for v in values:
        if v is None:
            result.append(None)
            continue
        buf.append(v)
        buf_sum += v
        if len(buf) > window:
            buf_sum -= buf.popleft()
        result.append(buf_sum / len(buf))
    return result


def _perf_schema(path: pathlib.Path) -> bool:
    try:
        first = path.open().readline(512).strip()
    except OSError:
        return False
    return frozenset(c.strip() for c in first.split(",")) == PERF_HEADER


def _nice_num(v: float | int | None) -> str:
    if v is None:
        return ""
    if isinstance(v, int):
        return str(v)
    if v == int(v):
        return str(int(v))
    return f"{v:.2g}"


def process_file(path: pathlib.Path) -> tuple[list[dict], str]:
    case_name = path.stem
    with path.open() as fh:
        raw = list(csv.DictReader(fh))

    # stable-sort by ts
    raw.sort(key=lambda r: float(r.get("ts", 0)))

    kept = [r for r in raw if _float_or_none(r.get("fps_avg", "")) is not None
            and float(r["fps_avg"]) > 0]
    if not kept:
        print(f"  [{case_name}] no valid rows (fps_avg <= 0 or missing), skipping")
        return [], case_name

    t0 = float(kept[0]["ts"])
    rows: list[dict] = []
    for idx, r in enumerate(kept):
        ts = float(r["ts"])
        fps = float(r["fps_avg"])
        load = _float_or_none(r.get("load_score", ""))
        row = {
            "case_name": case_name,
            "sample_index": idx,
            "ts": ts,
            "elapsed_s": round(ts - t0, 3),
            "gpu_percent": _float_or_none(r.get("gpu_percent", "")),
            "cpu_percent": _float_or_none(r.get("cpu_percent", "")),
            "ram_percent": _float_or_none(r.get("ram_percent", "")),
            "gpu_temp_c": _float_or_none(r.get("gpu_temp_c", "")),
            "fps_avg": fps,
            "n_active_cameras": r.get("n_active_cameras", "").strip(),
            "n_track_total": r.get("n_track_total", "").strip(),
            "n_track_sq_total": r.get("n_track_sq_total", "").strip(),
            "n_plate_total": r.get("n_plate_total", "").strip(),
            "stationary_fraction_mean": r.get("stationary_fraction_mean", "").strip(),
            "delta_load": _float_or_none(r.get("delta_load", "")),
            "load_score_raw": load,
            "load_score_smoothed": None,  # filled below
            "fps_deficit": max(0.0, TARGET_FPS - fps),
            "fps_drop": 1 if fps < TARGET_FPS else 0,
            "fps_severe_drop": 1 if fps < 22.5 else 0,
        }
        rows.append(row)

    # Causal trailing 3-row mean over load_score_raw
    scores: list[float | None] = [r["load_score_raw"] for r in rows]
    smoothed = _smooth(scores, 3)
    for row, sv in zip(rows, smoothed):
        row["load_score_smoothed"] = sv

    # Format
    for row in rows:
        row["gpu_percent"] = _nice_num(row["gpu_percent"])
        row["cpu_percent"] = _nice_num(row["cpu_percent"])
        row["ram_percent"] = _nice_num(row["ram_percent"])
        row["gpu_temp_c"] = _nice_num(row["gpu_temp_c"])
        row["fps_avg"] = _nice_num(row["fps_avg"])
        row["load_score_raw"] = _nice_num(row["load_score_raw"])
        row["load_score_smoothed"] = _nice_num(row["load_score_smoothed"])
        row["fps_deficit"] = round(float(row["fps_deficit"]), 2)
        row["fps_drop"] = int(row["fps_drop"])
        row["fps_severe_drop"] = int(row["fps_severe_drop"])

    return rows, case_name


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Clean performance collector CSVs into prediction-ready datasets. "
        "Filters to performance-schema files only (excludes p2p_migrations.csv). "
        "Writes <case>_clean.csv per case and load_prediction_clean.csv combined."
    )
    ap.add_argument("--input-dir", default="csv_collected",
                    help="Directory of raw collector CSVs (default: csv_collected)")
    ap.add_argument("--output-dir", default="Edge/logs/cleaned",
                    help="Directory for cleaned output (default: Edge/logs/cleaned)")
    args = ap.parse_args()

    input_dir = pathlib.Path(args.input_dir)
    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.is_dir():
        raise SystemExit(f"{args.input_dir}: not a directory")

    csv_files = sorted(input_dir.glob("*.csv"))
    perf_files = [f for f in csv_files
                  if f.name != "p2p_migrations.csv" and _perf_schema(f)]

    if not perf_files:
        print("No performance-schema CSVs found.")
        return

    print(f"Processing {len(perf_files)} files: {[f.name for f in perf_files]}")
    all_rows: list[dict] = []

    for fp in perf_files:
        rows, case = process_file(fp)
        if not rows:
            continue
        out = output_dir / f"{case}_clean.csv"
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUT_HEADER, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"  → {out.name} ({len(rows)} rows)")
        all_rows.extend(rows)

    if all_rows:
        combined = output_dir / "load_prediction_clean.csv"
        with combined.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUT_HEADER, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_rows)
        print(f"Combined: {combined} ({len(all_rows)} rows)")
    else:
        print("No rows across any file — nothing written.")


if __name__ == "__main__":
    main()