#!/usr/bin/env python3
"""
Edge/tools/profile_collect.py

System profiling data collector — records hardware metrics and pipeline
telemetry snapshots to a time-stamped CSV for load-model coefficient regression
and DL model training.

Reads the unified pipeline payload (written atomically by SpeedProbe) and
hardware metrics from jtop in lock-step.  Only writes a new CSV row when the
pipeline payload advances to a fresh sequence number, normalises the
interleaved FPS + features to a single writer window, and skips warmup data.

Usage (run on the target Jetson while the DeepStream pipeline is active):

    python3 tools/profile_collect.py --output logs/calibration.csv --duration 600

    --output    Path to output CSV (default: logs/calibration.csv)
    --duration  Collection window in seconds (default: 600 = 10 min)
    --interval  Polling interval in seconds (default: 2.0)

The script also measures W_base: if --wbase is passed, launch the pipeline with
zero video sources, run for --wbase-duration seconds, and record the idle GPU/CPU/
RAM mean as the base load.  Example:
    python3 tools/profile_collect.py --wbase --wbase-duration 60 --output logs/wbase.txt

Collection -> Training contract:
  • load_score is retained as a runtime/control diagnostic (fps-dominant anchored
    curve + HW emergency floor, scale 0-100).  Future ML labels derive separately
    from raw QoS metrics after calibrated collection.
  • FPS serves as the QoS validation signal (compare trained model predictions
    against TARGET_FPS at runtime).
  • Raw gpu_percent remains diagnostic only; use --target gpu_percent in
    train_dl_model.py if you want a raw-load model instead.

Output CSV columns:
    ts, gpu_percent, cpu_percent, ram_percent, gpu_temp_c,
    session_id, sequence,
    pipeline_window_started_monotonic, pipeline_window_ended_monotonic,
    pipeline_window_duration_s, pipeline_updated_at,
    fps_avg,
    n_active_cameras,
    n_track_total, n_plate_total,
    stationary_fraction_mean,
    offload_crops_received_per_s,
    load_score,
    delta_load
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
from health_agent import _read_payload, _compute_load_score

# ---------------------------------------------------------------------------
# Snapshot validity gate (pure helpers — no jtop / probe imports)
# ---------------------------------------------------------------------------
# The unified payload written atomically by SpeedProbe has the shape:
#   {
#     "_updated_at":                       <unix wall time>,
#     "_telemetry": {
#         "session_id":                    <hex str>,
#         "sequence":                      <monotone int>,
#         "pipeline_window_started_monotonic": <float>,
#         "pipeline_window_ended_monotonic":   <float>,
#         "pipeline_window_duration_s":        <float>,
#     },
#     "_features":  {cam_id: {...}},       # per-camera feature dict
#     <cam_id>: <fps float>,               # backward-compat direct key
#     "_offload_crops": {...},             # offload-receiver counters
#   }
#
# The collector accepts at most one CSV row per strictly-advancing
# (session_id, sequence) pair, and never writes fabricated values for a
# missing/malformed/stale snapshot — such samples are skipped.

# Operational polling cadence (seconds).  Snapshots older than this relative
# to read time are treated as stale and dropped.
CADENCE_S = 1.0


def _extract_telemetry(payload) -> tuple | None:
    """
    Return (session_id, sequence) for a well-formed snapshot, else None.
    Rejects missing/non-dict _telemetry, missing/empty session_id, and a
    missing or non-non-negative-int sequence.  Returns None exactly when the
    snapshot identity cannot be trusted — never fabricates a fallback.
    """
    if not isinstance(payload, dict):
        return None
    telemetry = payload.get("_telemetry")
    if not isinstance(telemetry, dict):
        return None
    sess_id = telemetry.get("session_id")
    seq     = telemetry.get("sequence")
    if not isinstance(sess_id, str) or not sess_id:
        return None
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return None
    return sess_id, seq


def _is_fresh(payload, now: float, max_age_s: float = CADENCE_S) -> bool:
    """
    True when _updated_at is present, numeric, and within max_age_s of `now`.
    A missing/malformed _updated_at is treated as stale (not fresh) so we
    never collect a snapshot whose age is unknown.
    """
    updated_at = payload.get("_updated_at") if isinstance(payload, dict) else None
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
        return False
    return (now - float(updated_at)) <= max_age_s


def _skip(interval: float, t0: float) -> None:
    """Sleep the remainder of this poll cycle; helper to keep loops terse."""
    time.sleep(max(0.0, interval - (time.monotonic() - t0)))


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
    "session_id", "sequence",
    "pipeline_window_started_monotonic", "pipeline_window_ended_monotonic",
    "pipeline_window_duration_s", "pipeline_updated_at",
    "fps_avg",
    "input_fps_avg",
    "n_active_cameras",
    "n_track_total", "n_plate_total",
    "stationary_fraction_mean",
    "offload_crops_received_per_s",
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

    # ── Session tracking for integrity ─────────────────────────────────────
    # Two-phase commit: candidate session must produce advancing sequences
    # through the warmup period before it becomes the committed session.
    # After commitment, session changes are rejected.
    #
    # Candidate state (not yet committed — can reset on session change):
    _cand_session_id: str = ""
    _cand_first_ts: float = 0.0
    _cand_last_seq: int = -1
    #
    # Committed state (locked after first row written):
    _committed_session_id: str = ""
    _committed_last_seq: int = -1

    print(f"[profile_collect] Collecting {duration:.0f}s -> {output}  (Ctrl+C to stop early)")

    with open(output, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()

        while time.monotonic() < deadline:
            t0 = time.monotonic()

            # 1) Read the unified pipeline payload exactly once per loop
            payload = _read_payload()
            if payload is None:
                # No payload has been written yet — wait and retry, write nothing
                _skip(interval, t0)
                continue

            # 2) Validity gate 1 — identity metadata (missing/malformed).
            #    Skip the sample entirely; never write a fabricated row.
            identity = _extract_telemetry(payload)
            if identity is None:
                _skip(interval, t0)
                continue
            sess_id, seq = identity

            # 2b) Validity gate 2 — freshness at the 1s cadence.  A payload
            #     older than CADENCE_S (or with an unknown age) is stale by
            #     definition; a zero-fps snapshot is a real measurement, but a
            #     stale one is not collectible state, so it is skipped.
            if not _is_fresh(payload, time.time()):
                _skip(interval, t0)
                continue

            # ── Phase: not yet committed ? manage candidate ───────────────
            if _committed_session_id == "":
                # 3a) Candidate session changed → reset candidate tracking
                if sess_id != _cand_session_id:
                    _cand_session_id = sess_id
                    _cand_first_ts   = time.time()
                    _cand_last_seq   = -1

                # 3b) Requirement: candidate must have an advancing sequence
                #     (fresh pipeline, not a stale payload). Non-advancing
                #     seqs are silently ignored during candidate phase.
                if seq <= _cand_last_seq:
                    elapsed = time.monotonic() - t0
                    time.sleep(max(0.0, interval - elapsed))
                    continue
                _cand_last_seq = seq

                # 3c) Warmup gate: require at least 6 s of elapsed wall time
                #     since the candidate session's first payload was observed.
                cand_age_s = time.time() - _cand_first_ts
                if cand_age_s < 6.0:
                    elapsed = time.monotonic() - t0
                    time.sleep(max(0.0, interval - elapsed))
                    continue

                # 3d) Candidate has survived warmup with advancing seqs →
                #     COMMIT this session.  We now write the first row.
                _committed_session_id = _cand_session_id
                _committed_last_seq   = seq
            # ── Phase: committed ──────────────────────────────────────────
            else:
                # 4a) Session changed → reject (pipeline restarted mid-collection)
                if sess_id != _committed_session_id:
                    elapsed = time.monotonic() - t0
                    time.sleep(max(0.0, interval - elapsed))
                    continue

                # 4b) Sequence dedup — only strictly advancing seqs accepted
                if seq <= _committed_last_seq:
                    elapsed = time.monotonic() - t0
                    time.sleep(max(0.0, interval - elapsed))
                    continue

                # 4c) Advance committed sequence
                _committed_last_seq = seq
            ts = time.time()

            # 6) Sample hardware ONCE only after accepting the snapshot
            hw = _read_hw(jtop)

            # 7) Compute FPS average from payload
            fps_vals = [v for k, v in payload.items()
                        if not k.startswith("_") and isinstance(v, (int, float)) and v > 0.0]
            fps_avg = sum(fps_vals) / len(fps_vals) if fps_vals else 0.0
            n_active_cameras = len(fps_vals)

            # 8) Compute input FPS average from the same snapshot
            input_fps_dict = payload.get("_input_fps", {})
            if isinstance(input_fps_dict, dict) and input_fps_dict:
                input_fps_vals = [v for v in input_fps_dict.values()
                                  if isinstance(v, (int, float))]
                input_fps_avg = (sum(input_fps_vals) / len(input_fps_vals)
                                 if input_fps_vals else 0.0)
            else:
                input_fps_avg = 0.0

            # 9) Aggregate features across active cameras
            feat_dict = payload.get("_features", {})
            _active_ids = {k for k, v in payload.items()
                           if not k.startswith("_") and isinstance(v, (int, float)) and v > 0.0}
            n_track_total  = 0.0
            n_plate_total  = 0.0
            stat_frac_vals = []
            for cam_id, cam_feats in feat_dict.items():
                if cam_id not in _active_ids:
                    continue
                n_track_total  += cam_feats.get("n_track", 0.0)
                n_plate_total  += cam_feats.get("n_plate", 0.0)
                stat_frac_vals.append(cam_feats.get("stationary_fraction", 0.0))
            stat_mean = (sum(stat_frac_vals) / len(stat_frac_vals)
                         if stat_frac_vals else 0.0)

            # 10) Offload rate from the same payload
            offload_crops = payload.get("_offload_crops", {})
            offload_rate = round(float(offload_crops.get("received_per_s", 0.0)), 3)

            delta = round(hw["gpu_percent"] - wbase_ref, 2)

            # 11) Compute load_score from the payload fps
            # Build fps_dict as health_agent expects: {cam_id: float}
            fps_dict = {k: v for k, v in payload.items()
                        if not k.startswith("_") and isinstance(v, (int, float))}
            load_score, _preset = _compute_load_score(hw, fps_dict)

            # 12) Snapshot metadata directly from the gate-verified payload
            tmeta = payload["_telemetry"]  # safe — _extract_telemetry confirmed a dict
            win_started = tmeta.get("pipeline_window_started_monotonic", 0.0)
            win_ended   = tmeta.get("pipeline_window_ended_monotonic", 0.0)
            win_dur     = tmeta.get("pipeline_window_duration_s", 0.0)
            updated_at  = payload.get("_updated_at", 0.0)

            writer.writerow({
                "ts":                                 round(ts, 3),
                "gpu_percent":                        hw["gpu_percent"],
                "cpu_percent":                        hw["cpu_percent"],
                "ram_percent":                        hw["ram_percent"],
                "gpu_temp_c":                         hw["gpu_temp_c"],
                "session_id":                         sess_id,
                "sequence":                           seq,
                "pipeline_window_started_monotonic":  round(win_started, 3),
                "pipeline_window_ended_monotonic":    round(win_ended, 3),
                "pipeline_window_duration_s":         round(win_dur, 3),
                "pipeline_updated_at":                round(updated_at, 3),
                "fps_avg":                            round(fps_avg, 2),
                "input_fps_avg":                       round(input_fps_avg, 2),
                "n_active_cameras":                   n_active_cameras,
                "n_track_total":                      round(n_track_total, 2),
                "n_plate_total":                      round(n_plate_total, 2),
                "stationary_fraction_mean":           round(stat_mean, 3),
                "offload_crops_received_per_s":       offload_rate,
                "load_score":                         load_score,
                "delta_load":                         delta,
            })
            rows_written += 1

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))

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
    ap = argparse.ArgumentParser(description="System profiling data collector for load-model regression")
    ap.add_argument("--output",         type=Path, default=Path("logs/calibration.csv"))
    ap.add_argument("--duration",       type=float, default=600.0,
                    help="Collection duration in seconds (default 600)")
    ap.add_argument("--interval",       type=float, default=1.0,
                    help="Sampling interval in seconds (default 1.0)")
    ap.add_argument("--wbase",          action="store_true",
                    help="Measure W_base instead of collecting calibration data")
    ap.add_argument("--wbase-duration", type=float, default=60.0,
                    help="W_base measurement window (default 60s)")
    ap.add_argument("--wbase-output",   type=Path, default=Path("logs/wbase.txt"),
                    help="File to write W_base result")
    ap.add_argument("--wbase-ref",      type=float, default=0.0,
                    help="Known W_base GPU%% to subtract as delta_load")
    args = ap.parse_args()

    # profile_collect cadence is 1.0 s, enforced at the CLI
    if abs(args.interval - 1.0) > 1e-9:
        print(
            "[profile_collect] ERROR: --interval must be 1.0 second. "
            "The entire telemetry stack (SpeedProbe, health agent, "
            "proactive model) operates on a 1 s cadence — mismatched "
            "collector interval would produce non-stationary rows.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.wbase:
        measure_wbase(args.wbase_output, args.wbase_duration, args.interval)
    else:
        collect(args.output, args.duration, args.interval, args.wbase_ref)


if __name__ == "__main__":
    main()
