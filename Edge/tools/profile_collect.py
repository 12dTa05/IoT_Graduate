#!/usr/bin/env python3
"""
Edge/tools/profile_collect.py

Phase 1 — Offline System Profiling (data collection for coefficient regression).

Reads the live feature + FPS file written by SpeedProbe and the hardware metrics
from jtop in lock-step, producing a time-stamped CSV suitable for
fit_coefficients.py AND train_dl_model.py.

Usage (run on the target Jetson while the DeepStream pipeline is active):

    python3 tools/profile_collect.py --output logs/calibration.csv --duration 600

    --output    Path to output CSV (default: logs/calibration.csv)
    --duration  Collection window in seconds (default: 600 = 10 min)
    --interval  Sampling interval in seconds (default: 2.0, matches HEALTH_INTERVAL)

The script also measures W_base: if --wbase is passed, launch the pipeline with
zero video sources, run for --wbase-duration seconds, and record the idle GPU/CPU/
RAM mean as the base load.  Example:
    python3 tools/profile_collect.py --wbase --wbase-duration 60 --output logs/wbase.txt

Collection -> Training contract:
  • load_score IS the training target — composite health_agent._compute_load_score
    (weighted GPU/CPU/RAM + FPS-drop penalty, scale 0-100). Every row has it.
  • FPS serves as the QoS validation signal (compare trained model predictions
    against TARGET_FPS at runtime).
  • Raw gpu_percent remains diagnostic only; use --target gpu_percent in
    train_dl_model.py if you want a raw-load model instead.

Output CSV columns:
    ts, gpu_percent, cpu_percent, ram_percent, gpu_temp_c,
    fps_avg,
    n_active_cameras,
    n_track_total, n_track_sq_total, n_plate_total, stationary_fraction_mean,
    load_score        (composite health_agent._compute_load_score: 0–100),
    delta_load        (gpu_percent - W_base; only populated if --wbase-ref given)

n_active_cameras is the count of cameras/sources that have fps > 0 at sample time.
It is used as a node-level model feature alongside the traffic aggregates so the
DL predictor can account for scale without per-camera padding.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

# Allow running from project root or from tools/
_EDGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_EDGE_DIR))

from speedflow_python.settings import FPS_STATS_FILE
from health_agent import _read_fps_stats, _read_feature_stats, _compute_load_score

# ---------------------------------------------------------------------------
# jtop helper — graceful fallback if jtop unavailable
# ---------------------------------------------------------------------------

def _open_jtop():
    try:
        from jtop import jtop as JTop
        import threading
        j = JTop()
        j.start()
        ev = threading.Event()
        def _w():
            try:
                if j.ok(): ev.set()
            except Exception: pass
        t = threading.Thread(target=_w, daemon=True)
        t.start(); t.join(timeout=10)
        if ev.is_set():
            return j
        j.close()
    except Exception:
        pass
    return None


def _read_hw(jtop_session) -> dict:
    """Return {gpu_percent, cpu_percent, ram_percent, gpu_temp_c}."""
    if jtop_session is None:
        return {"gpu_percent": 0.0, "cpu_percent": 0.0,
                "ram_percent": 0.0, "gpu_temp_c": 0.0}
    try:
        gpu_pct = 0.0
        for gv in jtop_session.gpu.values():
            gpu_pct = float(gv.get("status", {}).get("load", 0.0)); break

        cpu_total = jtop_session.cpu.get("total", {})
        cpu_pct   = 100.0 - float(cpu_total.get("idle", 100.0))

        mem = jtop_session.memory
        ram_tot = mem["RAM"]["tot"]
        ram_pct = float(mem["RAM"]["used"]) / ram_tot * 100.0 if ram_tot > 0 else 0.0

        temp_c = 0.0
        temp_dict = jtop_session.temperature
        for key in ("gpu", "tj", "cpu"):
            info = temp_dict.get(key)
            if isinstance(info, dict):
                t = info.get("temp", -1)
                if 0 < t < 120:
                    temp_c = float(t); break

        return {"gpu_percent": round(gpu_pct, 1), "cpu_percent": round(cpu_pct, 1),
                "ram_percent": round(ram_pct, 1), "gpu_temp_c": round(temp_c, 1)}
    except Exception:
        return {"gpu_percent": 0.0, "cpu_percent": 0.0,
                "ram_percent": 0.0, "gpu_temp_c": 0.0}


# ---------------------------------------------------------------------------
# Main collection loop
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "ts",
    "gpu_percent", "cpu_percent", "ram_percent", "gpu_temp_c",
    "fps_avg",
    "n_active_cameras",
    "n_track_total", "n_track_sq_total", "n_plate_total",
    "stationary_fraction_mean",
    "load_score",
    "delta_load",
]


def collect(output: Path, duration: float, interval: float, wbase_ref: float) -> None:
    jtop = _open_jtop()
    if jtop is None:
        print("[profile_collect] WARNING: jtop unavailable — hw metrics will be 0",
              file=sys.stderr)

    output.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output.exists() or output.stat().st_size == 0

    deadline = time.monotonic() + duration
    rows_written = 0

    print(f"[profile_collect] Collecting {duration:.0f}s → {output}  (Ctrl+C to stop early)")

    with open(output, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()

        while time.monotonic() < deadline:
            t0 = time.monotonic()
            ts = time.time()

            hw = _read_hw(jtop)
            fps_dict  = _read_fps_stats()
            feat_dict = _read_feature_stats()

            fps_vals = [v for v in fps_dict.values() if v > 0.0]
            fps_avg  = sum(fps_vals) / len(fps_vals) if fps_vals else 0.0

            # n_active_cameras: sources that are delivering frames (fps > 0)
            n_active_cameras = len(fps_vals)

            # Aggregate features across active cameras only (fps > 0),
            # matching the runtime DLPredictor's filtered feature_stats.
            _active_ids = {k for k, v in fps_dict.items() if v > 0.0}
            n_track_total     = 0.0
            n_plate_total     = 0.0
            stat_frac_vals    = []
            for cam_id, cam_feats in feat_dict.items():
                if cam_id not in _active_ids:
                    continue
                n_track_total  += cam_feats.get("n_track",             0.0)
                n_plate_total  += cam_feats.get("n_plate",             0.0)
                stat_frac_vals.append(cam_feats.get("stationary_fraction", 0.0))

            stat_mean = (sum(stat_frac_vals) / len(stat_frac_vals)
                         if stat_frac_vals else 0.0)
            delta = round(hw["gpu_percent"] - wbase_ref, 2)

            load_score, _preset = _compute_load_score(hw, fps_dict)

            writer.writerow({
                "ts":                      round(ts, 3),
                "gpu_percent":             hw["gpu_percent"],
                "cpu_percent":             hw["cpu_percent"],
                "ram_percent":             hw["ram_percent"],
                "gpu_temp_c":              hw["gpu_temp_c"],
                "fps_avg":                 round(fps_avg,      2),
                "n_active_cameras":        n_active_cameras,
                "n_track_total":           round(n_track_total, 2),
                "n_track_sq_total":        round(n_track_total ** 2, 2),
                "n_plate_total":           round(n_plate_total, 2),
                "stationary_fraction_mean": round(stat_mean,  3),
                "load_score":              load_score,
                "delta_load":              delta,
            })
            rows_written += 1

            elapsed = time.monotonic() - t0
            sleep_s = max(0.0, interval - elapsed)
            time.sleep(sleep_s)

    if jtop:
        try: jtop.close()
        except Exception: pass

    print(f"[profile_collect] Done — {rows_written} rows written to {output}")


def measure_wbase(output: Path, duration: float, interval: float) -> float:
    """
    Measure idle (W_base) GPU load.  Call this with the pipeline running on
    zero sources (or not running at all) to get the framework baseline.
    Returns the mean GPU%.
    """
    jtop = _open_jtop()
    samples = []
    deadline = time.monotonic() + duration
    print(f"[profile_collect] Measuring W_base for {duration:.0f}s ...")
    while time.monotonic() < deadline:
        hw = _read_hw(jtop)
        samples.append(hw["gpu_percent"])
        time.sleep(interval)
    if jtop:
        try: jtop.close()
        except Exception: pass
    mean_gpu = sum(samples) / len(samples) if samples else 0.0
    print(f"[profile_collect] W_base = {mean_gpu:.2f}% GPU (n={len(samples)})")
    if output:
        output.write_text(f"w_base_gpu_percent: {round(mean_gpu, 2)}\n")
    return mean_gpu


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Profile data collector for Phase-1 regression")
    ap.add_argument("--output",         type=Path, default=Path("logs/calibration.csv"))
    ap.add_argument("--duration",       type=float, default=600.0,
                    help="Collection duration in seconds (default 600)")
    ap.add_argument("--interval",       type=float, default=2.0,
                    help="Sampling interval in seconds (default 2.0)")
    ap.add_argument("--wbase",          action="store_true",
                    help="Measure W_base instead of collecting calibration data")
    ap.add_argument("--wbase-duration", type=float, default=60.0,
                    help="W_base measurement window (default 60s)")
    ap.add_argument("--wbase-output",   type=Path, default=Path("logs/wbase.txt"),
                    help="File to write W_base result")
    ap.add_argument("--wbase-ref",      type=float, default=0.0,
                    help="Known W_base GPU%% to subtract as delta_load")
    args = ap.parse_args()

    if args.wbase:
        measure_wbase(args.wbase_output, args.wbase_duration, args.interval)
    else:
        collect(args.output, args.duration, args.interval, args.wbase_ref)


if __name__ == "__main__":
    main()
