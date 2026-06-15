# PLAN.md — Predictive, Pair‑Aware Multi‑Edge Coordination for Resilient Traffic Monitoring

> **Status:** Design + execution blueprint (living document).
> **Scope:** Evolve the current reactive P2P traffic‑monitoring cluster into a *predictive*, *saturation‑aware*, *pair‑aware* multi‑edge system that stays stable under compute‑node failure and traffic surges — validated on a synthetic, fully‑interconnected intersection dataset that can also be fed into real Jetson devices.
> **Audience:** Engineers and reviewers who need to understand (1) what exists today, (2) what we are building, (3) the exact steps to build it, and (4) the scientific contribution.

---

## Table of Contents

1. [Why this document exists](#1-why-this-document-exists)
2. [The reality today (current system)](#2-the-reality-today-current-system)
3. [Problems we must solve](#3-problems-we-must-solve)
4. [The future system (target architecture)](#4-the-future-system-target-architecture)
5. [The four switching mechanisms](#5-the-four-switching-mechanisms)
6. [Design decisions (locked)](#6-design-decisions-locked)
7. [Workstreams & build steps](#7-workstreams--build-steps)
8. [Data contracts (schemas)](#8-data-contracts-schemas)
9. [Execution order & milestones](#9-execution-order--milestones)
10. [Validation & metrics](#10-validation--metrics)
11. [Potential contributions](#11-potential-contributions)
12. [Risks & mitigations](#12-risks--mitigations)
13. [Glossary](#13-glossary)

---

## 1. Why this document exists

The current system is a **production‑grade, decentralized** traffic‑monitoring cluster: NVIDIA Jetson edge nodes run DeepStream (YOLO + LPD + LPR) pipelines, coordinate via **Eclipse Zenoh** in brokerless peer mode, and balance load through a **Pareto ε‑constraint voting protocol** with leaderless consistent‑hash failover. It works, but three things hold it back in the *real world* of a signalized intersection:

1. **It reacts instead of predicts.** Offload happens *after* a node is already overloaded.
2. **Its load formula is wrong for Jetson MAXN mode.** GPU% pins near 100% even when the device is *not* saturated, so the weighted `load_score` is misleading.
3. **The metadata edges exchange is incomplete and inaccurate.** Peers cannot see each other's *per‑camera* workload, real capacity, power mode, or camera roles — so coordination decisions are made on partial information.

This plan turns the system into a **predictive, saturation‑aware, pair‑aware** coordinator. It removes the brittle traffic‑light‑cycle assumption entirely and replaces the analytical proactive model with a **lightweight, stable machine‑learning predictor** trained on a **synthetic but fully interconnected** intersection dataset.

---

## 2. The reality today (current system)

### 2.1 Topology

```
Cameras (RTSP) ──▶ Jetson Edge nodes (DeepStream + Zenoh P2P) ──▶ Central Server (aiohttp + MediaMTX)
```

- **Edge/** — AI processing. Each Jetson runs the DeepStream pipeline (`main.py`), a standalone `health_agent.py`, and an in‑process `PeerOrchestrator`.
- **Server/** — aggregation + dashboard (`app.py`, `edge_registry.py`, `violation_store.py`).
- **Camera/** — Docker RTSP simulator (loops MP4s as live streams).

### 2.2 Per‑Jetson camera layout (important)

Each Jetson currently drives **two main cameras pointing in opposite directions on the same road**:

- One camera scans vehicles **approaching** the traffic light.
- The other scans vehicles **leaving** the traffic light.

These two cameras are a **logical pair** observing the same physical road segment from opposite ends. The current code treats them as fully independent streams — it has **no notion of a camera role or pair**.

### 2.3 How load is computed today (the broken part)

`health_agent._compute_load_score()`:

```
base    = w_gpu·GPU% + w_cpu·CPU% + w_ram·RAM%
penalty = max(0, (TARGET_FPS − avg_fps)/TARGET_FPS) · fps_penalty_max
score   = min(100, base + penalty)
```

with adaptive ω presets (`normal`, `bandwidth`) and a thermal ramp `Θ_thermal`. A "proactive" model (`load_model.py::ProactiveModel`) optionally fuses a CV‑plane index `L` and a hardware index `H` via noisy‑OR, **averaged over a fixed 90‑second window** (`cycle_window_s`) assumed to match a traffic‑light cycle.

### 2.4 How coordination works today

- **Heartbeat:** `peers/status/{node_id}` every 2s with hardware metrics + `pipeline{fps_per_camera, avg_fps, active_cameras, camera_configs}`.
- **Overload → RFO voting:** overloaded node requests offload; peers bid if they pass ε‑constraints (capacity, predicted FPS, RTT, cooldown, penalty); lowest `F(x)` wins.
- **Make‑before‑Break migration:** winner starts the stream, acks PLAYING, only then the requester removes it.
- **Leaderless failover:** on heartbeat timeout, all peers independently consistent‑hash the dead node's cameras to deterministically elect a rescuer.
- **Multi‑level offload (built but disabled):** L1 = full stream migration, L2 = vehicle‑crop offload, L3 = plate‑crop offload, escalated by fixed thresholds.

### 2.5 What the heartbeat actually carries vs. what peers read

| Field produced | Consumed by `PeerState`? | Problem |
|---|---|---|
| `load_score` | ✅ | MAXN‑broken (see §3) |
| `gpu/cpu/ram_percent`, `gpu_temp_c` | ✅ | GPU% misleading in MAXN |
| `power_mw`, `omega_preset` | ❌ never read | wasted signal |
| `pipeline.fps_per_camera` | ✅ | aggregate only; no per‑camera *workload* |
| `pipeline.active_cameras` | ✅ | no roles/pairs |
| `pipeline.camera_configs` | ✅ | used for failover only |
| `max_streams` | ❌ never populated | capacity hardcoded to 8 |
| per‑camera `n_track/n_plate/ocr_backlog` | ❌ absent | peers can't predict each other's surges |
| camera `role`/`pair_id` | ❌ absent | pairs get split on offload/failover |

---

## 3. Problems we must solve

| # | Problem | Root cause | Consequence |
|---|---|---|---|
| P1 | **Reactive, not predictive** | Offload triggers after overload | Frame drops & blind‑spots during surges |
| P2 | **Load formula wrong in MAXN** | GPU%‑dominated score; GPU pinned ~100% | False overloads / missed real saturation |
| P3 | **Incomplete inter‑edge metadata** | Heartbeat lacks per‑camera workload, real capacity, power mode, roles | Bad coordination decisions on partial info |
| P4 | **Signal‑cycle assumption is brittle** | Fixed 90s `cycle_window_s` tied to a traffic light | Wrong at non‑signalized/variable intersections |
| P5 | **No camera‑pair awareness** | Code treats the 2 opposite cameras as independent | Pair split across nodes → loses approaching↔leaving correlation |
| P6 | **No realistic, interconnected dataset** | Only looped MP4s | Cannot validate prediction/failover scientifically |

---

## 4. The future system (target architecture)

```
                 ┌─────────────────────── Zenoh peer mode (brokerless) ───────────────────────┐
                 │  peers/status/*  (v2 heartbeat: per‑camera workload, capacity, roles, pred) │
                 │  peers/vote/*    (predictive + selective RFO)                               │
                 │  peers/control/* (ADD/REMOVE — pair‑aware)                                  │
                 └────────────────────────────────────────────────────────────────────────────┘
   ┌───────────────────────── Jetson Edge Node ─────────────────────────┐
   │ DeepStream pipeline (YOLO → NvDCF → LPD → LPR)                      │
   │   └─ SpeedProbe ── emits per‑camera workload (n_track, n_plate,     │
   │                    OCR backlog, FPS deficit) every frame            │
   │                                                                    │
   │ Saturation Load Model  ← replaces GPU%‑weighted score (P2)         │
   │ FlowPredictor (lightweight ML) ← forecasts N_proc(t+H) (P1,P4)     │
   │ PeerOrchestrator                                                    │
   │   ├─ Predictive trigger (act before saturation)                    │
   │   ├─ Selective level (L1/L2/L3 by predicted bottleneck)            │
   │   ├─ Pair‑aware offload & failover (P5)                            │
   │   └─ v2 metadata contract (P3)                                     │
   └────────────────────────────────────────────────────────────────────┘
                                  ▲
                                  │ feature time‑series (no video needed)
   ┌──────────────────────────────┴───────────────────────────────────┐
   │ Synthetic Interconnected Intersection Dataset (P6)                 │
   │  ground truth (per camera/frame/vehicle/plate, cross‑camera linked)│
   │  + feed adapter → /dev/shm/speedflow_fps.json on a REAL Jetson     │
   │  + simulation harness (reactive vs predictive, failure injection)  │
   └────────────────────────────────────────────────────────────────────┘
```

### 4.1 Component changes at a glance

| Component | Today | Future |
|---|---|---|
| Load score | GPU%‑weighted + FPS penalty | **Saturation/contention model** (FPS deficit, OCR/track backlog, latency, thermal headroom) |
| Proactive model | Analytical noisy‑OR over 90s cycle | **`FlowPredictor`** — lightweight ML, horizon `H`, **cycle‑free** |
| Offload trigger | Threshold after overload | **Predictive** — act before saturation crosses in horizon |
| Level selection | Fixed 3→2→1 by load | **Selective** — pick L1/L2/L3 by *predicted dominant bottleneck* |
| Camera model | Independent streams | **Role (approaching/leaving) + pair** |
| Heartbeat | Aggregate metrics | **v2 contract**: per‑camera workload, real capacity, power mode, predictions, roles |
| Failover | Consistent‑hash (kept) | **Same, made pair‑aware** |
| Validation | Looped MP4s | **Synthetic interconnected dataset + live feed adapter + harness** |

---

## 5. The four switching mechanisms

The system shifts work between nodes using four granularities. The **predictor chooses which** to use based on the *predicted dominant bottleneck*, then the existing transport primitives execute it.

| Mechanism | When predicted bottleneck is… | Reuses existing primitive | Cost |
|---|---|---|---|
| **(1) RTSP camera stream switch (L1)** | Whole‑camera saturation | RFO voting + Make‑before‑Break migration | High (full stream) |
| **(2) Vehicle‑crop segment switch (L2)** | Detection/tracking heavy (many vehicles) | `OffloadPublisher.put_vehicle` → `OffloadReceiver` (remote LPD+LPR) | Medium |
| **(3) LPD‑box segment switch (L3)** | OCR/plate heavy | `OffloadPublisher.put_plate` → `OffloadReceiver` (remote LPR) | Low |
| **(—) Hold** | Surge predicted to subside within horizon | no action (anti‑thrash) | Zero |

**Prediction driver:** *"number of vehicles to process (tracking + OCR)."* The predictor forecasts both the **tracking workload** (`n_track` trend) and the **OCR workload** (`n_plate` + OCR backlog trend), and maps the dominant one to the cheapest sufficient mechanism.

---

## 6. Design decisions (locked)

These were decided with stakeholders and are binding for this plan:

1. **Predict + select** — the predictor both forecasts overload early *and* selects the offload granularity.
2. **Remove the 90s signal‑cycle logic entirely** — no traffic‑light period anywhere; replace with a horizon‑based, cycle‑free predictor.
3. **Keep the consistent‑hash leaderless failover** — add prediction only on the offload/load‑balancing path; make failover *pair‑aware* but do not change its election mechanism.
4. **Lightweight, stable ML model** — default **GBDT (e.g., LightGBM/XGBoost) exported to ONNX or a pure‑Python tree dump**, <1 ms inference on Jetson, dependency‑light. Optional compact GRU/TCN as a comparison baseline only.
5. **Completely remove `ProactiveModel`/`CycleSmoother`** but **keep `theta_thermal`, `compute_h_reactive`, `fuse`** (cycle‑free, reused) so system logic never breaks.
6. **Synthetic dataset first** — build the interconnected ground truth + feed adapter + harness before the predictor, so we have labels to train/validate against.
7. **No MP4 rendering required**, but provide a **direction to feed data into real Jetson devices** for live processing/collection (feature time‑series → `/dev/shm/speedflow_fps.json`).
8. **Camera role + pair** are part of config and metadata; the two opposite‑facing cameras per Jetson are a pair (approaching ↔ leaving on the same road).

---

## 7. Workstreams & build steps

> Each workstream lists **files**, **what changes**, and **acceptance criteria**. Items marked *(new)* are new files.

### WS‑0 — Synthetic Interconnected Intersection Dataset *(build first)*

**New dir:** `Edge/tools/dataset/`

- `generate_intersection_dataset.py` *(new)* — world+fleet simulator:
  - Persistent vehicles: `vehicle_uid`, `plate_text`, `type` (car/bus/truck/moto), `color`, `true_speed`, continuous world trajectory `(x, y, heading, t)`.
  - 4‑leg intersection; per leg an **opposite‑facing camera pair** (approaching + leaving) with homography in the same format as `cameras.yml`.
  - **Cycle‑free demand:** Poisson/burst arrivals per leg + stop‑line queue formation (parameterized; no hard‑coded light period).
  - **Cross‑camera interconnection:** project each vehicle's world position into every camera; same `vehicle_uid` and `plate_text` appear in the approaching cam then the leaving cam with consistent world coords.
- `synthesize_features.py` *(new)* — aggregate ground truth into the per‑camera feature time‑series (`n_track`, `n_plate`, `ocr_backlog`, `stationary_fraction`, `fps_deficit`) in the exact shape the live `_features` file uses.
- `feed_to_jetson.py` *(new)* — replay the feature time‑series into `/dev/shm/speedflow_fps.json` at real cadence so a **real Jetson's** health_agent/predictor/orchestrator process it **without video** (the "direction to feed real devices").
- `sim_replay.py` *(new)* — offline harness: run reactive vs predictive on the same dataset; inject node death; emit metrics + charts.

**Outputs:** `ground_truth.csv`, `flow_timeseries.csv` (CSV‑only by default; Parquet optional behind `pyarrow`).

**Acceptance:** for any vehicle crossing both cameras of a pair, `vehicle_uid` and `plate_text` match and world coords are continuous; feature time‑series replays into a Jetson and shows up in `health_agent` logs.

---

### WS‑1 — Saturation Load Model (fix P2: MAXN)

**Files:** `Edge/speedflow_python/load_model.py` (rewrite), `Edge/health_agent.py`, `Edge/configs/edge_node.yml`.

- Replace GPU%‑weighted score with a **saturation/contention** score (0–100 = headroom→saturated):
  - **Primary:** per‑camera **FPS deficit** `max(0, (TARGET_FPS − fps_cam)/TARGET_FPS)`.
  - **Backlog:** tracking queue depth + **pending‑OCR backlog** (from `SpeedProbe`, WS‑3).
  - **Latency:** per‑frame probe processing time trend.
  - **Thermal headroom:** reuse `theta_thermal`.
  - GPU% kept only as a weak tie‑breaker.
- Keep `theta_thermal`, `compute_h_reactive`, `fuse` as reusable primitives.

**Acceptance:** in a MAXN profile where GPU% ≈ 100% but FPS is on target, the new score stays low; when FPS drops/backlog grows, the score rises.

---

### WS‑2 — Remove cycle logic cleanly (P4, decision #5)

**Files:** `load_model.py`, `edge_node.yml`, `tools/fit_coefficients.py`, `tools/plot_rmse.py`, `tools/plot_burst.py`, `tests/test_load_model.py`, `README.md`.

- Delete `CycleSmoother` and `ProactiveModel`; purge `cycle_window_s` and all red/green‑phase wording.
- Replace cycle tests with predictor/saturation tests.
- Ensure the system runs with the predictor **disabled** (graceful fallback to the new saturation load score).

**Acceptance:** `rg -n "cycle_window_s|CycleSmoother|ProactiveModel|signal cycle"` returns no functional references; full pipeline starts with predictor off.

---

### WS‑3 — Per‑camera workload instrumentation (enables P1/P3)

**Files:** `Edge/speedflow_python/probes.py` (`SpeedProbe`).

- Add a lightweight **OCR backlog** counter: number of active tracks not yet plate‑locked vs. completed, per camera.
- Extend `_flush_features` / `_feature_cache` to emit per‑camera `{n_track, n_plate, ocr_backlog, fps_deficit, stationary_fraction}`.

**Acceptance:** `/dev/shm/speedflow_fps.json` `_features` block contains the new per‑camera fields; values move sensibly under synthetic load.

---

### WS‑4 — Inter‑edge metadata contract v2 (fix P3, P5)

**Files:** `peer_orchestrator.py` (`PeerState`, `_on_peer_status`), `run_python.py::_health_push_loop`, `health_agent.py`, `edge_node.yml`, `cameras.yml`.

- **Heartbeat v2** (see §8.1): add `schema_version`, populated `max_streams`, `power_mode`, `saturation_headroom`, per‑camera workload block, predicted fields, and camera `role`/`pair_id`. Consume previously‑ignored `power_mw`.
- Extend `PeerState` to store all of it; update every consumer (ε‑constraints, `_pick_best_peer`, failover, reclaim) to read **real** values instead of hardcoded defaults.
- Add `role` + `pair_id` to each camera in `cameras.yml`.

**Acceptance:** a peer can read another peer's real `max_streams`, per‑camera workload, and pair grouping from a single heartbeat; no consumer uses the hardcoded `max_streams=8` default anymore.

---

### WS‑5 — `FlowPredictor` (lightweight ML, cycle‑free) (P1, decision #4)

**New file:** `Edge/speedflow_python/flow_predictor.py`; **new tool:** `Edge/tools/train_flow_predictor.py`.

- **Target:** `N_proc(t+H)` (tracking + OCR work) and resulting predicted saturation, per camera, over horizon `H` (config; e.g. 5–15s).
- **Features:** recent `n_track`, `n_plate`, arrival rate (Δn_track), `ocr_backlog`, `fps_deficit`, `stationary_fraction`, plus lagged values — **no periodicity**.
- **Model:** GBDT exported to ONNX/tree‑dump; pure‑Python fallback if runtime missing. Trained offline on WS‑0 dataset by `train_flow_predictor.py`.
- **Online:** runs each health tick; outputs `risk_pred`, `track_pred`, `ocr_pred`, `n_proc_pred` → merged into heartbeat (WS‑4) and consumed by orchestrator (WS‑6).

**Acceptance:** on held‑out synthetic data, predictor beats a naive "last value" baseline on MAE and gives non‑zero **lead time** before saturation; inference < 1 ms/camera on Jetson.

---

### WS‑6 — Predictive + selective + pair‑aware offload (P1, P5, decision #1)

**Files:** `peer_orchestrator.py`.

- **Predictive trigger:** overload onset fires when `risk_pred` crosses threshold within horizon `H` (hard fuse on instantaneous saturation retained as safety net).
- **Selective level:** `_select_offload_level()` maps predicted dominant bottleneck → L1/L2/L3/hold (the four mechanisms, §5).
- **Pair‑aware:** offload and failover keep a camera **pair** together unless the target can host both or splitting is explicitly allowed.
- **Target selection:** use peers' `risk_pred` (predicted free capacity), not just current load.
- **Failover election unchanged** (decision #3) — only made pair‑aware.

**Acceptance:** under a predicted OCR surge the system chooses L3 (not full migration); under whole‑camera saturation it chooses L1; pairs never split unintentionally in harness runs.

---

### WS‑7 — Tests, docs, end‑to‑end validation

**Files:** `Edge/tests/`, `README.md`, `PLAN.md` (this file).

- New tests: `test_flow_predictor.py`, `test_load_model_saturation.py`, `test_metadata_contract.py`, `test_offload_selection.py`, `test_dataset_integrity.py`, `test_pair_awareness.py`.
- Update `README.md` to reflect predictor, saturation load model, v2 metadata, pairs; remove cycle claims.

**Acceptance:** all tests pass; `sim_replay.py` produces predictive‑vs‑reactive charts; README has no stale signal‑cycle references.

---

## 8. Data contracts (schemas)

### 8.1 Heartbeat v2 (`peers/status/{node_id}`)

```jsonc
{
  "schema_version": 2,
  "type": "health",
  "node_id": "jetson_A",
  "timestamp": 1700000000.0,

  // Saturation-based load (WS-1) — NOT GPU%-weighted
  "load_score": 0.0,            // 0..100 headroom→saturated
  "saturation_headroom": 0.0,   // 0..1 (1 = idle, 0 = saturated)
  "power_mode": "MAXN",         // device power profile
  "gpu_percent": 0.0, "cpu_percent": 0.0, "ram_percent": 0.0,
  "gpu_temp_c": 0.0, "power_mw": 0.0,  // now consumed by peers

  // Real capacity (WS-4)
  "max_streams": 8,

  // Prediction (WS-5)
  "risk_pred": 0.0,             // predicted saturation at t+H
  "predict_horizon_s": 10.0,

  "pipeline": {
    "avg_fps": 0.0,
    "fps_per_camera": { "cam_01": 0.0 },
    "active_cameras": ["cam_01", "cam_02"],

    // Per-camera workload (WS-3/4) — lets peers predict each other
    "camera_workload": {
      "cam_01": {
        "n_track": 0.0, "n_plate": 0.0, "ocr_backlog": 0.0,
        "fps_deficit": 0.0, "stationary_fraction": 0.0,
        "n_proc_pred": 0.0,
        "role": "approaching",   // approaching | leaving
        "pair_id": "leg_north"
      }
    },
    "camera_configs": { /* for failover, unchanged */ }
  }
}
```

### 8.2 `cameras.yml` per‑camera additions (WS‑4)

```yaml
cam_01:
  # ... existing fields ...
  role: approaching      # approaching | leaving
  pair_id: leg_north     # the two opposite cameras on the same road share this
```

### 8.3 Dataset ground truth (WS‑0) — one row per detection

```
camera_id, frame_no, ts, vehicle_uid, track_id,
bbox_x, bbox_y, bbox_w, bbox_h,
world_x, world_y, true_speed_kmh,
plate_text, vtype, color,
role, pair_id, in_roi, occluded
```

### 8.4 Dataset feature time‑series (WS‑0) — feeds predictor & Jetson

```
ts, camera_id, n_track, n_plate, ocr_backlog, stationary_fraction, fps_deficit
```

---

## 9. Execution order & milestones

| Milestone | Workstreams | Deliverable |
|---|---|---|
| **M1 — Foundation** | WS‑0 | Synthetic dataset + feed adapter + harness; ground truth verified interconnected |
| **M2 — Honest load** | WS‑1, WS‑2, WS‑3 | Saturation load model; cycle logic removed; per‑camera workload emitted |
| **M3 — Complete metadata** | WS‑4 | Heartbeat v2; pairs/roles; peers read real capacity & workload |
| **M4 — Prediction** | WS‑5 | Trained `FlowPredictor`; predicted fields in heartbeat |
| **M5 — Predict + select + pair** | WS‑6 | Predictive, selective, pair‑aware offload |
| **M6 — Prove it** | WS‑7 | Tests green; predictive‑vs‑reactive charts; docs updated |

Each milestone is independently reviewable. The system remains runnable after every milestone (predictor disabled until M4).

---

## 10. Validation & metrics

Run via `Edge/tools/dataset/sim_replay.py` on the synthetic dataset (and optionally a live Jetson via `feed_to_jetson.py`).

| Metric | Meaning | Goal |
|---|---|---|
| **Prediction MAE / lead time** | accuracy & how early surges are seen | beat naive last‑value; lead time > 0 |
| **Migration count** | offload churn | fewer / no thrashing vs reactive |
| **Predicted vs actual overload** | precision/recall of trigger | high precision, few false triggers |
| **Failover blind‑spot (ms)** | gap when a node dies | ≤ current, pair preserved |
| **FPS deficit during surge** | true saturation proxy | lower than reactive baseline |
| **Pair integrity** | pairs kept together | 100% unless splitting allowed |

Failure injection: kill a node mid‑run; measure rescue time and that paired cameras land together.

---

## 11. Potential contributions

1. **Cycle‑free predictive coordination** — removes the traffic‑light‑period assumption that limits prior signal‑cycle‑aware methods; generalizes to any intersection or free‑flow road.
2. **Saturation‑based load model for MAXN accelerators** — a load metric that is meaningful when GPU utilization is pinned high; addresses a real, under‑reported gap in Jetson‑class edge deployments.
3. **Predict‑and‑select fine‑grained offload** — choosing L1/L2/L3 (stream / vehicle‑crop / LPD‑crop) by *predicted dominant bottleneck* rather than fixed thresholds; more granular than published stream‑level migration work.
4. **Pair‑aware coordination** — first‑class modeling of opposite‑facing approaching/leaving camera pairs, preserving cross‑camera vehicle correlation under offload and failover.
5. **Complete, versioned inter‑edge metadata contract** — per‑camera workload + real capacity + power mode + predictions exchanged P2P, enabling peers to anticipate each other's surges.
6. **Synthetic interconnected intersection dataset + Jetson feed path** — reproducible ground truth (cross‑camera linked by `vehicle_uid`/`plate_text`) that drives both offline simulation and live on‑device experiments without video.

---

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Predictor overfits synthetic data | Hold‑out split; naive baseline floor; keep predictor optional & fall back to saturation score |
| ML runtime unavailable on Jetson | Export to pure‑Python tree dump fallback; GBDT chosen for portability |
| Removing cycle logic breaks callers | Keep `theta_thermal`/`fuse`/`compute_h_reactive`; run full pipeline with predictor disabled after WS‑2 |
| Heartbeat v2 breaks old nodes | `schema_version` gate; consumers tolerate missing v2 fields |
| Pair constraint reduces offload flexibility | Allow explicit pair‑split when no peer can host both; configurable |
| OCR backlog instrumentation adds probe cost | Lightweight counters only; measured on the hot path |

---

## 13. Glossary

| Term | Definition |
|---|---|
| **MAXN** | Jetson max‑performance power mode; GPU% often pinned high regardless of true saturation |
| **Saturation headroom** | How far the pipeline is from failing to meet target FPS / backlog limits |
| **FlowPredictor** | Lightweight ML model forecasting tracking+OCR workload over horizon `H` |
| **N_proc(t+H)** | Predicted number of vehicles to process (tracking + OCR) at time `t+H` |
| **Pair / role** | Two opposite‑facing cameras on one road; roles = approaching / leaving |
| **L1 / L2 / L3** | Offload granularity: RTSP stream / vehicle‑crop / LPD‑plate‑crop switch |
| **RFO** | Request For Offload (Zenoh vote protocol) |
| **Make‑before‑Break** | Start the new stream and confirm PLAYING before removing the old one |
| **Heartbeat v2** | Redesigned, complete inter‑edge metadata contract |
| **Interconnected dataset** | Ground truth where the same vehicle is linked across cameras by `vehicle_uid`/`plate_text` |

---

*End of PLAN.md*
