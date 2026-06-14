#!/usr/bin/env python3
"""
Edge/tools/plot_burst.py

Phase 4 — Chart 2: Throughput Under Traffic Bursts
(Proactive vs Reactive under red-phase load surge).

Reads the calibration CSV and the migration log (logs/p2p_migrations.csv),
then produces a publication-ready figure showing:

  Panel A (top): N_track and stationary_fraction over time — the "traffic
      stimulus" driven by the signal cycle.

  Panel B (middle): Load signals over time:
      • Reactive baseline (load_score / 100) — what the existing system uses
      • Proactive U (risk_index, cycle-smoothed)
      Vertical lines mark detected red-phase peaks (N_track local maxima).

  Panel C (bottom): Offload trigger timeline:
      • Reactive trigger events (load_score crossed overload_threshold)
      • Proactive trigger events (U crossed risk_threshold)
      • Migration events from p2p_migrations.csv (if available)
  Annotation: "Proactive triggers X s earlier on average."

Usage:
    python3 tools/plot_burst.py \\
        --csv       logs/calibration.csv \\
        --mig       logs/p2p_migrations.csv \\
        --cfg       configs/edge_node.yml \\
        --out       logs/chart2_burst.png \\
        --cycle     90        # signal cycle in seconds (for annotation)

Requirements: matplotlib, pandas, numpy, scipy  (pip install matplotlib pandas numpy scipy)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

def _require(pkg: str):
    try:
        return __import__(pkg)
    except ImportError:
        print(f"ERROR: {pkg} required — pip install {pkg}", file=sys.stderr)
        sys.exit(1)

pd  = _require("pandas")
np  = _require("numpy")
plt = _require("matplotlib.pyplot")

import importlib.util as _ilu
_lm_path = Path(__file__).resolve().parents[1] / "speedflow_python" / "load_model.py"
_spec = _ilu.spec_from_file_location("load_model", _lm_path)
_lm   = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_lm)
ProactiveModel = _lm.ProactiveModel
CycleSmoother  = _lm.CycleSmoother


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cfg(yml_path: Path) -> dict:
    try:
        import yaml
        return (yaml.safe_load(yml_path.read_text()) or {})
    except Exception as exc:
        print(f"ERROR loading {yml_path}: {exc}", file=sys.stderr)
        sys.exit(1)


def _build_feature_stats(row) -> dict:
    return {"cam_merged": {
        "n_track":             float(row.get("n_track_total",           0.0)),
        "n_plate":             float(row.get("n_plate_total",           0.0)),
        "stationary_fraction": float(row.get("stationary_fraction_mean",0.0)),
    }}


def _build_metrics(row) -> dict:
    return {
        "gpu_percent": float(row.get("gpu_percent", 0.0)),
        "cpu_percent": float(row.get("cpu_percent", 0.0)),
        "ram_percent": float(row.get("ram_percent", 0.0)),
        "gpu_temp_c":  float(row.get("gpu_temp_c",  0.0)),
    }


def _find_red_peaks(n_track: np.ndarray, t_rel: np.ndarray,
                    prominence: float = 5.0) -> np.ndarray:
    """Return timestamps of N_track local maxima (red-phase peaks)."""
    try:
        from scipy.signal import find_peaks
        peaks, _ = find_peaks(n_track, prominence=prominence,
                              distance=max(1, len(n_track) // 20))
        return t_rel[peaks]
    except ImportError:
        # Fallback: simple threshold crossings
        threshold = np.mean(n_track) + 0.5 * np.std(n_track)
        above = n_track > threshold
        crossings = np.where(np.diff(above.astype(int)) == 1)[0]
        return t_rel[crossings]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Chart 2: Proactive vs Reactive under bursts")
    ap.add_argument("--csv",   type=Path, required=True)
    ap.add_argument("--mig",   type=Path, default=None,
                    help="Migration log CSV (logs/p2p_migrations.csv)")
    ap.add_argument("--cfg",   type=Path,
                    default=Path(__file__).resolve().parents[1] / "configs" / "edge_node.yml")
    ap.add_argument("--out",   type=Path, default=Path("logs/chart2_burst.png"))
    ap.add_argument("--cycle", type=float, default=90.0,
                    help="Expected signal cycle period in seconds (default 90)")
    ap.add_argument("--wbase", type=float, default=None)
    args = ap.parse_args()

    full_cfg      = _load_cfg(args.cfg)
    proactive_cfg = full_cfg.get("proactive", {})
    p2p_cfg       = full_cfg.get("p2p", {})
    proactive_cfg["enabled"] = True   # force on for simulation
    if args.wbase is not None:
        proactive_cfg["w_base"] = args.wbase

    overload_thr  = float(p2p_cfg.get("overload_threshold",  65.0))
    overload_dur  = float(p2p_cfg.get("overload_duration_s", 35.0))
    risk_thr      = float(proactive_cfg.get("risk_threshold", 0.85))
    window_s      = float(proactive_cfg.get("cycle_window_s", 90.0))

    df = pd.read_csv(args.csv)
    print(f"[plot_burst] Loaded {len(df)} rows from {args.csv}")

    model    = ProactiveModel(proactive_cfg)
    smoother = CycleSmoother(window_s)

    t_rel_arr     = []
    n_track_arr   = []
    stat_frac_arr = []
    reactive_arr  = []   # load_score / 100
    proactive_arr = []   # U (cycle-smoothed)

    # For lead-time computation
    reactive_overload_start  = None
    proactive_overload_start = None
    lead_times = []

    reactive_triggers  = []   # (t_rel, load) of each reactive onset
    proactive_triggers = []   # (t_rel, U) of each proactive onset

    for _, row in df.iterrows():
        metrics    = _build_metrics(row)
        feat_stats = _build_feature_stats(row)
        ts         = float(row.get("ts", 0.0))

        result    = model.compute(metrics, feat_stats, ts=ts)
        u_smooth  = result["risk_index"]
        load_norm = float(row.get("load_score", metrics["gpu_percent"])) / 100.0

        t0 = df["ts"].iloc[0]
        t  = ts - t0

        t_rel_arr.append(t)
        n_track_arr.append(float(row.get("n_track_total", 0.0)))
        stat_frac_arr.append(float(row.get("stationary_fraction_mean", 0.0)))
        reactive_arr.append(load_norm)
        proactive_arr.append(u_smooth)

        # Detect reactive overload onset (load_score > threshold)
        reactive_over = load_norm * 100.0 > overload_thr
        if reactive_over:
            if reactive_overload_start is None:
                reactive_overload_start = t
        else:
            if reactive_overload_start is not None:
                dur = t - reactive_overload_start
                if dur >= overload_dur:
                    reactive_triggers.append((reactive_overload_start + overload_dur,
                                              load_norm))
            reactive_overload_start = None

        # Detect proactive overload onset (U >= risk_threshold)
        proactive_over = u_smooth >= risk_thr
        if proactive_over:
            if proactive_overload_start is None:
                proactive_overload_start = t
        else:
            if proactive_overload_start is not None:
                proactive_triggers.append((proactive_overload_start, u_smooth))
            proactive_overload_start = None

    t_rel     = np.array(t_rel_arr)
    n_track   = np.array(n_track_arr)
    stat_frac = np.array(stat_frac_arr)
    reactive  = np.array(reactive_arr)
    proactive = np.array(proactive_arr)

    # Compute mean lead time: for each proactive trigger find the nearest
    # reactive trigger that comes after it and measure the gap
    lead_times = []
    for pt, _ in proactive_triggers:
        later_rt = [rt for rt, _ in reactive_triggers if rt > pt]
        if later_rt:
            lead_times.append(min(later_rt) - pt)
    mean_lead = float(np.mean(lead_times)) if lead_times else 0.0

    red_peaks = _find_red_peaks(n_track, t_rel)

    # Load migration events if available
    mig_times = []
    if args.mig and args.mig.exists():
        try:
            mig_df = pd.read_csv(args.mig)
            if "timestamp_iso" in mig_df.columns:
                import datetime
                t0_dt = datetime.datetime.fromtimestamp(df["ts"].iloc[0])
                for _, mrow in mig_df.iterrows():
                    try:
                        mts = datetime.datetime.fromisoformat(mrow["timestamp_iso"])
                        mig_times.append((mts - t0_dt).total_seconds())
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[plot_burst] Could not read migration log: {exc}", file=sys.stderr)

    # ── Figure ────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(3, 1, figsize=(13, 10), sharex=True)
    fig.suptitle("Chart 2 — Proactive vs Reactive Load Control Under Traffic Bursts",
                 fontsize=13)

    # Panel A: traffic stimulus
    ax = axes[0]
    ax.plot(t_rel, n_track,   color="#795548", linewidth=1.2, label="N_track (vehicles/frame)")
    ax2a = ax.twinx()
    ax2a.plot(t_rel, stat_frac, color="#FF9800", linewidth=0.9, alpha=0.7,
              linestyle="--", label="Stationary fraction S")
    ax2a.set_ylabel("S (fraction)", color="#FF9800", fontsize=9)
    ax2a.tick_params(axis="y", labelcolor="#FF9800")
    ax2a.set_ylim(0, 1.05)
    for rp in red_peaks:
        ax.axvline(rp, color="red", alpha=0.15, linewidth=0.8)
    ax.set_ylabel("N_track (vehicles)")
    ax.set_title("Traffic Stimulus (signal-cycle peaks shaded in red)")
    ax.legend(loc="upper left", fontsize=8)
    ax2a.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.25)

    # Panel B: load signals
    ax = axes[1]
    ax.plot(t_rel, reactive,  color="#F44336", linewidth=1.1,
            label="Reactive baseline (load_score / 100)")
    ax.plot(t_rel, proactive, color="#2196F3", linewidth=1.3,
            label=f"Proactive U (smoothed, window={window_s:.0f}s)")
    ax.axhline(overload_thr / 100.0, color="#F44336", linestyle=":",
               linewidth=1.0, alpha=0.7,
               label=f"Reactive threshold ({overload_thr:.0f}%)")
    ax.axhline(risk_thr, color="#2196F3", linestyle=":",
               linewidth=1.0, alpha=0.7,
               label=f"Proactive threshold (U={risk_thr:.2f})")
    for rp in red_peaks:
        ax.axvline(rp, color="red", alpha=0.12, linewidth=0.8)
    ax.set_ylabel("Normalised Load / Risk Index")
    ax.set_title("Load Signals: Proactive U vs Reactive load_score")
    ax.legend(fontsize=8)
    ax.set_ylim(0, 1.08)
    ax.grid(True, alpha=0.25)

    # Panel C: trigger timeline
    ax = axes[2]
    # Reactive triggers
    for t_trig, _ in reactive_triggers:
        ax.axvline(t_trig, color="#F44336", linewidth=1.2, alpha=0.8)
    if reactive_triggers:
        ax.axvline(reactive_triggers[0][0], color="#F44336", linewidth=1.2,
                   alpha=0.8, label="Reactive offload trigger")
    # Proactive triggers
    for t_trig, _ in proactive_triggers:
        ax.axvline(t_trig, color="#2196F3", linewidth=1.2, alpha=0.8,
                   linestyle="--")
    if proactive_triggers:
        ax.axvline(proactive_triggers[0][0], color="#2196F3", linewidth=1.2,
                   alpha=0.8, linestyle="--", label="Proactive offload trigger")
    # Migration events
    for mt in mig_times:
        ax.axvline(mt, color="#4CAF50", linewidth=1.0, alpha=0.6,
                   linestyle="-.")
    if mig_times:
        ax.axvline(mig_times[0], color="#4CAF50", linewidth=1.0,
                   alpha=0.6, linestyle="-.", label="Actual migration")

    if mean_lead > 0:
        ax.text(0.02, 0.80,
                f"Mean proactive lead time: {mean_lead:.1f} s",
                transform=ax.transAxes, fontsize=10,
                bbox=dict(boxstyle="round,pad=0.3", facecolor="#E3F2FD"))

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Trigger event")
    ax.set_yticks([])
    ax.set_title("Offload Trigger Timeline")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.25, axis="x")

    plt.tight_layout()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(args.out, dpi=150)
    print(f"[plot_burst] Saved → {args.out}")
    if mean_lead > 0:
        print(f"[plot_burst] Mean proactive lead time: {mean_lead:.1f} s")
    plt.show()


if __name__ == "__main__":
    main()
