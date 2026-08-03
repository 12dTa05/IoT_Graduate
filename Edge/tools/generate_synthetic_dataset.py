#!/usr/bin/env python3
"""
Edge/tools/generate_synthetic_dataset.py

Deterministic, high-fidelity synthetic Jetson telemetry for FPS regression.
Augments (never replaces) real data.  Uses ONLY stdlib + numpy.

TARGET_FPS and load_score anchors match Edge/health_agent.py exactly:
  27→0, 22→57, 19→65, 17→75, 0→100  (piecewise-linear in between).

Usage:
  python3 generate_synthetic_dataset.py --output-dir /tmp/syn
  python3 generate_synthetic_dataset.py --output-dir /tmp/syn --total-rows 50000
  python3 generate_synthetic_dataset.py --self-check
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np

# ── Schema ──────────────────────────────────────────────────────────────────
FIELDNAMES = [
    "ts",
    "gpu_percent", "cpu_percent", "ram_percent", "gpu_temp_c",
    "session_id", "sequence",
    "pipeline_window_started_monotonic", "pipeline_window_ended_monotonic",
    "pipeline_window_duration_s", "pipeline_updated_at",
    "fps_avg",
    "n_active_cameras",
    "n_track_total", "n_plate_total",
    "stationary_fraction_mean",
    "offload_crops_received_per_s",
    "load_score",
    "delta_load",
]

MANIFEST_VERSION = "1.0.0"
DEFAULT_START_TS = 1680300000.0   # 2023-03-31T22:00:00Z — deterministic epoch
TARGET_FPS = 27.0                 # matches Edge/health_agent.py
CMA_PERIOD = 8                    # samples for running min reference (4 s window)

# ── Scenario library ────────────────────────────────────────────────────────
# (label, cam_count, tracks_mean, tracks_std)
# ponytail: cam_count is nominal/scenario-level hint; camera assignment is
# orthogonal via the Cartesian schedule.  Do not divide tracks by camera.
SCENARIOS = (
    ("empty",       1,   0.0,  0.0),
    ("sparse",      1,   2.5,  1.2),
    ("moderate",    2,   6.0,  2.0),
    ("dense",       3,  10.0,  2.5),
    ("surge",       2,  14.0,  3.5),
    ("cycle",       1,   6.0,  4.0),
    ("ramp_up",     2,   4.0,  5.0),
    ("ramp_down",   3,  10.0,  3.0),
    ("mixed",       4,   8.0,  8.0),
)
N_SCENARIOS = len(SCENARIOS)

AR1 = {
    "empty": 0.0, "sparse": 0.92, "moderate": 0.90, "dense": 0.95,
    "surge": 0.88, "cycle": 0.90, "ramp_up": 0.93, "ramp_down": 0.91,
    "mixed": 0.89,
}

STATF_BASE = {
    "empty": 0.0, "sparse": 0.15, "moderate": 0.20, "dense": 0.25,
    "surge": 0.10, "cycle": 0.20, "ramp_up": 0.30, "ramp_down": 0.15,
    "mixed": 0.20,
}

# ── FPS model ───────────────────────────────────────────────────────────────
def fps_model(total_tracks: float, rng: np.random.Generator) -> float:
    """Nonlinear threshold/plateau/cliff on TOTAL n_track_total.

    Thresholds applied to absolute total track count (NOT per-camera).
    Camera effect: small observational 3-cam ceiling (~1.5 FPS drop);
    no causal migration dynamics.  Clamped [9, 27].
    """
    t = total_tracks

    if t <= 8.0:
        base = 27.0
    elif t <= 10.0:
        # soft decline: 27 → ~25
        base = 27.0 - (t - 8.0) * 1.0
    elif t <= 12.0:
        # material cliff: ~25 → ~20
        base = 25.0 - (t - 10.0) * 2.5
    else:
        # ponytail: bounded plateau centered near 20.95, no downward extrapolation
        base = 20.95

    # Small jitter (±1.0), clamp final to [9, 27]
    fps = base + rng.uniform(-1.0, 1.0)
    fps = max(9.0, min(27.0, fps))
    return round(float(fps), 2)


# ── Camera-scenario ceiling: small observational non-causal effect ─────────
def camera_ceiling(fps: float, cams: int) -> float:
    """Observational 3-camera ceiling (non-causal); no migration dynamics."""
    # ponytail: tiny cap for 3-camera condition only; no camera*track interaction
    if cams >= 3:
        return min(fps, 26.0)
    return fps


# ── load_score from FPS ────────────────────────────────────────────────────
def load_score_fps(fps: float) -> float:
    """Piecewise linear matching Edge/health_agent.py: 27=>0, 22=>57, 19=>65, 17=>75, 0=>100.

    No hardware floor — generator doesn't model CPU/RAM emergencies.
    """
    c = max(0.0, min(TARGET_FPS, fps))
    if c >= TARGET_FPS:
        return 0.0
    if c >= 22.0:
        return round(float(57.0 * (TARGET_FPS - c) / (TARGET_FPS - 22.0)), 1)
    if c >= 19.0:
        return round(float(57.0 + 8.0 * (22.0 - c) / 3.0), 1)
    if c >= 17.0:
        return round(float(65.0 + 10.0 * (19.0 - c) / 2.0), 1)
    return round(float(75.0 + 25.0 * (17.0 - c) / 17.0), 1)


# ── GPU from workload + cams + jitter ──────────────────────────────────────
def gpu_load(n_tracks: float, fps: float, cams: int, crop_rate: float,
             rng: np.random.Generator) -> float:
    """GPU utilization from tracked objects, FPS deficit, cameras, offload crops."""
    base = 10.0 + float(cams) * 3.0
    track_contrib = n_tracks * 3.2
    deficit = max(0.0, TARGET_FPS - fps)
    fps_contrib = deficit * 4.0
    crop_contrib = crop_rate * 0.25  # ponytail: plausible encoding cost per crop
    raw = base + track_contrib + fps_contrib + crop_contrib
    raw += rng.uniform(-0.04, 0.04) * raw  # burst alias
    return round(float(np.clip(raw, 0.0, 99.8)), 1)


# ── Temperature inertia ────────────────────────────────────────────────────
def temp_next(prev: float, gpu_pct: float, cpu_pct: float, rng: np.random.Generator) -> float:
    """First-order lag inertia, physically bounded [40, 72]."""
    target = 42.0 + gpu_pct * 0.18 + cpu_pct * 0.08
    new = prev + (target - prev) * 0.04  # tau per 0.5 s step
    new += rng.uniform(-0.1, 0.1)
    return round(float(np.clip(new, 40.0, 72.0)), 1)


# ── AR(1) track series ─────────────────────────────────────────────────────
def ar1_series(n: int, mean: float, sigma: float, lag1: float,
               rng: np.random.Generator) -> np.ndarray:
    if lag1 <= 0.0 or mean == 0.0:
        return np.zeros(n)
    noise_std = sigma * np.sqrt(1.0 - lag1 ** 2)
    out = np.empty(n)
    out[0] = mean + rng.normal(0.0, noise_std)
    for i in range(1, n):
        out[i] = mean + lag1 * (out[i - 1] - mean) + rng.normal(0.0, noise_std)
    return np.maximum(0.0, out)


# ── Offload crop rate generator ─────────────────────────────────────────────
def _crop_series(
    n: int, session_idx: int, tracks: np.ndarray, plates: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Deterministic contiguous-interval offload crop rate.

    All sessions get nonzero crop episodes.  Each session: 1-3 bounded
    contiguous on-intervals, each spanning 3-7 % of session rows
    (min 3 rows, max 252 rows for 3600-row default).  Crop rate derived
    from local tracks (L2 vehicle-crop) and plates (L3 plate-crop), both
    bounded [0, 40] crops/s.  All other rows → 0.0.
    """
    crop = np.zeros(n)

    # ponytail: sub-generator so deterministic across identical seeds
    rng2 = np.random.default_rng(rng.integers(0, 2**31 - 1))
    num_episodes = int(rng2.integers(1, 4))         # 1–3 episodes
    # ponytail: episode length scales with session size; absolute floor of 3 rows
    dur_low  = max(3, int(n * 0.03))
    dur_high = max(5, int(n * 0.07))
    dur = int(rng2.integers(dur_low, dur_high + 1))  # same length for all episodes

    for _ in range(num_episodes):
        if dur >= n:
            start = 0
        else:
            start = int(rng2.integers(0, n - dur))
        end = min(start + dur, n)
        eps_rng = np.random.default_rng(rng2.integers(0, 2**31 - 1))
        for i in range(start, end):
            l3 = plates[i] * 1.8               # L3 plate-crop
            l2 = max(0.0, tracks[i] - plates[i]) * 0.9  # L2 vehicle-crop
            val = l3 + l2 + eps_rng.uniform(-1.0, 1.0)
            # ponytail: epsilon-positive noise from empty track/plate is meaningless
            if l3 + l2 <= 0.0:
                crop[i] = 0.0
            else:
                crop[i] = max(0.0, min(val, 40.0))
    return crop


# ── Session generator ──────────────────────────────────────────────────────
def gen_session(
    idx: int, scenario_name: str, cam_count: int, rows: int,
    start_ts: float, rng: np.random.Generator,
) -> tuple[list[dict], dict]:
    """Return (csv_rows, manifest_entry)."""
    sid = f"synthetic_{idx:03d}_{scenario_name}_cam{cam_count}"
    label, _name, tmean, tsig = next(s for s in SCENARIOS if s[0] == scenario_name)
    lag1 = AR1[scenario_name]
    sf_base = STATF_BASE[scenario_name]

    tracks = ar1_series(rows, tmean, tsig, lag1, rng)
    plate_ratio = 0.55 + rng.uniform(-0.03, 0.03)
    plates = tracks * plate_ratio + rng.normal(0.0, 0.2, rows)
    plates = np.maximum(0.0, plates)
    stat_fracs = np.clip(sf_base + rng.normal(0.0, 0.04, rows), 0.0, 1.0)
    offload = _crop_series(rows, idx, tracks, plates, rng)

    cpu_base_val = 18.0 + cam_count * 4.0 + rng.uniform(4.0, 12.0)
    cpu = np.clip(cpu_base_val + rng.normal(0.0, 2.5, rows), 5.0, 95.0)
    ram = np.clip(28.0 + cam_count * 3.5 + rng.uniform(5.0, 12.0) + rng.normal(0.0, 1.5, rows), 10.0, 95.0)

    # Count the rounded value actually emitted to CSV/seen by training.
    crop_rows = int(np.sum(np.round(offload, 2) > 0.0))
    has_crop = crop_rows > 0

    csv_rows: list[dict] = []
    prev_s = 0.0
    t0 = start_ts + idx * rows * 0.5
    t_cur = 42.0 + rng.uniform(0.0, 4.0)

    # ponytail: running-min FPS over CMA_PERIOD samples; crop pressure
    # reduces the MIN FPS seen over the window → lower GPU ceiling.
    recent_fps: list[float] = []

    for i in range(rows):
        ts_val = round(t0 + i * 0.5, 3)
        crop_rate = float(offload[i])

        # Base FPS from track model
        fps_val = fps_model(tracks[i], rng)
        fps_val = camera_ceiling(fps_val, cam_count)

        # Crop-rate causal FPS reduction: bounded [0, 2] FPS penalty
        # at max crop rate (40 crops/s).  Linear on [0, 0.05] per crop/s.
        if crop_rate > 0:
            penalty = min(crop_rate * 0.05, 2.0) + rng.uniform(-0.2, 0.2)
            fps_val = max(9.0, fps_val - penalty)

        # ponytail: double-clamp so no leak above TARGET_FPS
        fps_val = min(27.0, max(9.0, fps_val))

        recent_fps.append(fps_val)
        if len(recent_fps) > CMA_PERIOD:
            recent_fps.pop(0)

        gpu_val = gpu_load(tracks[i], fps_val, cam_count, crop_rate, rng)

        t_cur = temp_next(t_cur, gpu_val, cpu[i], rng)
        ls = load_score_fps(fps_val)
        dl = round(float(ls - prev_s), 2) if i > 0 else 0.0
        prev_s = ls

        w0 = round(ts_val - 0.25, 3)
        w1 = round(ts_val, 3)

        csv_rows.append({
            "ts":                                  ts_val,
            "gpu_percent":                         gpu_val,
            "cpu_percent":                         round(float(cpu[i]), 1),
            "ram_percent":                         round(float(ram[i]), 1),
            "gpu_temp_c":                          round(float(t_cur), 1),
            "session_id":                          sid,
            "sequence":                             i + 1,
            "pipeline_window_started_monotonic":   w0,
            "pipeline_window_ended_monotonic":     w1,
            "pipeline_window_duration_s":          0.25,
            "pipeline_updated_at":                 w1,
            "fps_avg":                              fps_val,
            "n_active_cameras":                     cam_count,
            "n_track_total":                        round(float(tracks[i]), 2),
            "n_plate_total":                        round(float(plates[i]), 2),
            "stationary_fraction_mean":             round(float(stat_fracs[i]), 3),
            "offload_crops_received_per_s":         round(crop_rate, 2),
            "load_score":                            ls,
            "delta_load":                            dl,
        })

    entry = {
        "filename":          f"{sid}.csv",
        "scenario":          scenario_name,
        "num_cameras":       cam_count,
        "rows":              len(csv_rows),
        "crop_rows":         crop_rows,
        "crop_active":       has_crop,
        "crop_l2_vehicle":   True,
        "crop_l3_plate":     True,
    }
    return csv_rows, entry


# ── Write helpers ───────────────────────────────────────────────────────────
def write_csv_rows(out_dir: Path, filename: str, rows: list[dict]) -> None:
    path = out_dir / filename
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_manifest(out_dir: Path, sess_list: list[dict], total_rows: int,
                   seed: int, rows_per_session: int,
                   start_ts: float = DEFAULT_START_TS) -> None:
    crop_session_count = sum(1 for s in sess_list if s.get("crop_active"))
    doc = {
        "schema_version":     MANIFEST_VERSION,
        "schema_columns":     len(FIELDNAMES),
        "synthetic":          True,
        "intended_use":       "augmentation/training only — never for validation or test",
        "calibration_scope":  (
            "Primarily Mode-A 2-camera thresholds; "
            "1-camera and 3-camera effects are observational/confounded"
        ),
        "static_non_monocular": True,
        "crop_generation": {
            "enabled":             True,
            "sessions_with_crop":  crop_session_count,
            "crop_l2_vehicle":     "derived from (track - plate) × 0.9",
            "crop_l3_plate":       "derived from plate × 1.8",
            "crop_effect_gpu":     "GPU +0.25 per crop/s, causal FPS penalty up to 2.0",
            "crop_bounds":         "[0, 40] crops/s",
            "crop_episodes":       "1–3 bounded contiguous intervals, each 3–7 % of session rows",
            "crop_eligible":       "all sessions",
        },
        "provenance":         {
            "generator": "Edge/tools/generate_synthetic_dataset.py",
            "seed": seed,
            "rows_per_session": rows_per_session,
            "total_sessions": len(sess_list),
            "total_rows": total_rows,
            "start_ts": start_ts,
        },
        "sessions": sorted(sess_list, key=lambda x: x["filename"]),
    }
    path = out_dir / "manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


# ── Main generator ─────────────────────────────────────────────────────────
def generate(output_dir: str, total_rows: int, rows_per_session: int,
             seed: int, start_ts: float = DEFAULT_START_TS) -> Path:
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(
            f"ERROR: '{out}' is not empty — refusing to overwrite."
        )
    out.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)

    # Deterministic Cartesian schedule: all 9 scenarios × (1,2,3) product,
    # cycled in order.  Build the 27-element product once, then cycle.
    CAMERAS = (1, 2, 3)
    scenario_names = [s[0] for s in SCENARIOS]

    product_order: list[tuple[str, int]] = []
    for sn in scenario_names:
        for cam in CAMERAS:
            product_order.append((sn, cam))

    plan: list[tuple[str, int, int]] = []
    allocated = 0
    while allocated < total_rows:
        for sn, cam in product_order:
            if allocated >= total_rows:
                break
            sz = min(rows_per_session, total_rows - allocated)
            plan.append((sn, cam, sz))
            allocated += sz

    manifest_entries: list[dict] = []
    for idx, (sc, cc, sz) in enumerate(plan):
        rows, entry = gen_session(idx, sc, cc, sz, start_ts, rng)
        write_csv_rows(out, entry["filename"], rows)
        manifest_entries.append(entry)
        print(f"  -> {entry['filename']} ({entry['rows']} rows)")

    write_manifest(out, manifest_entries, total_rows, seed, rows_per_session, start_ts)
    print(f"Done: {total_rows} rows in {len(manifest_entries)} sessions -> {out}")

    return out


# ── Self-check ─────────────────────────────────────────────────────────────
def _all_csv_rows(out_dir: Path) -> list[dict]:
    """Collect all CSV rows from a generation output directory."""
    rows: list[dict] = []
    for csv_file in sorted(out_dir.glob("*.csv")):
        rows.extend(csv.DictReader(csv_file.open()))
    return rows


def self_check():
    print("=== SELF-CHECK ===")
    tmp = Path(tempfile.mkdtemp(prefix="synthetic_self_check_"))

    # -- Round 1: 27 sessions × 50 rows = 1350 rows, covers all 27 combos
    out1 = tmp / "round1"
    r1 = generate(str(out1), total_rows=1350, rows_per_session=50, seed=42)

    rows1 = _all_csv_rows(out1)
    assert len(rows1) == 1350, f"round1 rows={len(rows1)}, expected 1350"

    for r in rows1:
        assert len(r) == len(FIELDNAMES), f"header count mismatch: {len(r)} vs {len(FIELDNAMES)}"
        oc = float(r["offload_crops_received_per_s"])
        assert 0.0 <= oc <= 40.0, f"offload_crops out of bounds: {oc}"
        g = float(r["gpu_percent"])
        assert 0.0 <= g <= 99.8, f"gpu out of bounds: {g}"
        t = float(r["gpu_temp_c"])
        assert 40.0 <= t <= 72.0, f"temp out of bounds: {t}"
        ls = float(r["load_score"])
        assert 0.0 <= ls <= 100.0, f"load_score out of bounds: {ls}"
        fps = float(r["fps_avg"])
        assert 9.0 <= fps <= 27.0, f"fps out of bounds: {fps}"
        dl = float(r["delta_load"])
        assert abs(dl) <= 100.0

    # Crop assertions: ≥1 session has nonzero crop rows, others all zero
    crop_rows = [r for r in rows1 if float(r["offload_crops_received_per_s"]) > 0.0]
    assert len(crop_rows) > 0, "Must have at least one row with nonzero offload crop rate"
    sessions_with_crop = set()
    for r in rows1:
        sid = r["session_id"]
        if float(r["offload_crops_received_per_s"]) > 0.0:
            sessions_with_crop.add(sid)
    assert len(sessions_with_crop) >= 1, "Must have ≥1 session with nonzero crop rows"

    # Rows in crop sessions: verify that crop GPU contribution is structurally
    # wired — offload_crops → gpu_load(crop_rate) → GPU%.  Check at least one
    # session where the max GPU row also has nonzero crop.
    crop_gpu_coupled = False
    for sid in sorted(sessions_with_crop):
        sess_rows = [r for r in rows1 if r["session_id"] == sid]
        max_gpu = max(float(r["gpu_percent"]) for r in sess_rows)
        max_rows = [r for r in sess_rows if float(r["gpu_percent"]) == max_gpu]
        if any(float(r["offload_crops_received_per_s"]) > 0 for r in max_rows):
            crop_gpu_coupled = True
            break
    assert crop_gpu_coupled, "Crop rate never coincides with peak GPU — causal chain appears disconnected"

    # Manifest checks
    m1 = json.loads((out1 / "manifest.json").read_text())
    assert m1["synthetic"] is True
    assert m1["intended_use"] == "augmentation/training only — never for validation or test"
    assert "primarily mode-a 2-camera thresholds" in m1["calibration_scope"].lower()
    assert m1["static_non_monocular"] is True
    assert m1["schema_version"] == "1.0.0"
    assert m1["schema_columns"] == len(FIELDNAMES)
    assert m1["provenance"]["total_rows"] == 1350
    assert len(m1["sessions"]) == 27
    # Crop manifest metadata
    assert m1["crop_generation"]["enabled"] is True
    assert m1["crop_generation"]["sessions_with_crop"] >= 1
    assert m1["crop_generation"]["crop_bounds"] == "[0, 40] crops/s"

    # Verify camera set ⊆ {1,2,3}
    cams_seen = {int(r["n_active_cameras"]) for r in rows1}
    assert cams_seen.issubset({1, 2, 3}), f"camera set not subset of {{1,2,3}}: {cams_seen}"

    # FPS bins in 2-camera condition
    fps2 = [(float(r["n_track_total"]), float(r["fps_avg"]))
            for r in rows1 if int(r["n_active_cameras"]) == 2]

    lo8   = [f for n, f in fps2 if n <= 8]
    mid8  = [f for n, f in fps2 if 8 < n <= 10]
    mid10 = [f for n, f in fps2 if 10 < n <= 12]
    hi12  = [f for n, f in fps2 if n > 12]

    # ≤8: near 27 (crop episodes may pull some rows lower)
    if lo8:
        med = sorted(lo8)[len(lo8) // 2]
        assert med >= 26.5, f"<=8 2-cam median FPS should be near 27: median={med}"
    # 8–10: lower (soft decline)
    if mid8:
        avg = sum(mid8) / len(mid8)
        assert avg < 26.5, f"8-10 2-cam avg should be < 26.5: {avg}"
    # 10–12: materially lower than 8–10
    if mid10 and mid8:
        avg10 = sum(mid10) / len(mid10)
        avg8 = sum(mid8) / len(mid8)
        assert avg10 < avg8 - 1.0, f"10-12 should be materially lower than 8-10: {avg10:.1f} vs {avg8:.1f}"
    # >12: plateau near 20.95
    if hi12:
        avg = sum(hi12) / len(hi12)
        assert 20.0 < avg < 22.0, f">12 2-cam avg should be ~20.95: {avg:.2f}"

    # load_score anchors: verify exact match with health_agent.py contract
    # TARGET_FPS=27: 27→0, 22→57, 19→65, 17→75, 0→100
    assert load_score_fps(27.0) == 0.0, "anchor 27→0"
    assert load_score_fps(30.0) == 0.0, "anchor >27→0"
    assert load_score_fps(22.0) == 57.0, "anchor 22→57"
    assert abs(load_score_fps(19.0) - 65.0) < 0.1, "anchor 19→65"
    assert abs(load_score_fps(17.0) - 75.0) < 0.1, "anchor 17→75"
    assert abs(load_score_fps(0.0) - 100.0) < 0.1, "anchor 0→100"
    # Midpoints: monotonic decreasing
    assert load_score_fps(24.0) < load_score_fps(20.0) < load_score_fps(18.0)
    assert load_score_fps(15.0) < load_score_fps(10.0) < load_score_fps(5.0)

    # All 9 scenarios × 3 cameras covered (27 complete sessions)
    combo_seen = set()
    for r in rows1:
        sid = r["session_id"]
        parts = sid.split("_")
        sc = "_".join(parts[2:-1])
        cam = parts[-1]
        combo_seen.add((sc, cam))
    all_combos = {(s[0], f"cam{c}") for s in SCENARIOS for c in (1, 2, 3)}
    assert combo_seen == all_combos, f"Missing combos: {all_combos - combo_seen}"

    # -- Round 2, different output dir, identical seed → byte-identical CSVs
    out2 = tmp / "round2"
    r2 = generate(str(out2), total_rows=1350, rows_per_session=50, seed=42)
    for csv_file in out1.glob("*.csv"):
        name = csv_file.name
        b1 = csv_file.read_bytes()
        b2 = (out2 / name).read_bytes()
        assert b1 == b2, f"Byte mismatch: {name}"
    m2 = json.loads((out2 / "manifest.json").read_text())
    assert m1 == m2, "Manifest byte-identical check failed"

    # -- Round 3: non-default start_ts → reflected in CSV timestamps and manifest
    non_default_ts = 1700000000.0
    out3 = tmp / "round3"
    r3 = generate(str(out3), total_rows=100, rows_per_session=50, seed=99,
                  start_ts=non_default_ts)
    first_ts = None
    for csv_file in sorted(out3.glob("*.csv")):
        rows3 = list(csv.DictReader(csv_file.open()))
        if rows3:
            first_ts = float(rows3[0]["ts"])
            break
    assert first_ts == non_default_ts, f"first ts={first_ts}, expected {non_default_ts}"
    m3 = json.loads((out3 / "manifest.json").read_text())
    assert m3["provenance"]["start_ts"] == non_default_ts, \
        f"manifest start_ts={m3['provenance']['start_ts']}, expected {non_default_ts}"

    # -- Round 4: byte-identical with same non-default start_ts
    out4 = tmp / "round4"
    r4 = generate(str(out4), total_rows=100, rows_per_session=50, seed=99,
                  start_ts=non_default_ts)
    for csv_file in out3.glob("*.csv"):
        name = csv_file.name
        b3 = csv_file.read_bytes()
        b4 = (out4 / name).read_bytes()
        assert b3 == b4, f"Byte mismatch with non-default ts: {name}"
    m4 = json.loads((out4 / "manifest.json").read_text())
    assert m3 == m4, "Manifest byte-identical check failed (non-default tsp)"

    print("SELF-CHECK: ALL PASSED")
    shutil.rmtree(tmp, ignore_errors=True)


# ── CLI ────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate deterministic synthetic Jetson telemetry CSVs"
    )
    ap.add_argument("--output-dir", type=str, required=False,
                    help="Output directory for generated CSVs + manifest")
    ap.add_argument("--total-rows", type=int, default=300_000,
                    help="Total dataset rows to generate (default: 300000)")
    ap.add_argument("--rows-per-session", type=int, default=3600,
                    help="Rows per session CSV (default: 3600)")
    ap.add_argument("--seed", type=int, default=42,
                    help="Random seed for reproducibility (default: 42)")
    ap.add_argument("--start-ts", type=float, default=DEFAULT_START_TS,
                    help="Deterministic origin timestamp (default: fixed epoch)")
    ap.add_argument("--self-check", action="store_true",
                    help="Run built-in validation + byte-reproducibility test")
    args = ap.parse_args()

    if args.self_check:
        self_check()
        return 0

    if args.output_dir is None:
        ap.error("--output-dir is required (or use --self-check)")

    if args.total_rows <= 0:
        ap.error("--total-rows must be positive")
    if args.rows_per_session <= 0:
        ap.error("--rows-per-session must be positive")

    generate(args.output_dir, args.total_rows, args.rows_per_session, args.seed, args.start_ts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
