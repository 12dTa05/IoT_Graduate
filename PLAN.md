# PLAN — Stability-Aware Decentralized Multi-Edge Coordination for Resilient Real-Time Traffic Video Analytics

> **Goal:** Evolve the current reactive/ε-constraint load-sharing into a *stability-first, control-theoretic, decentralized* coordination system that (a) avoids node overload-collapse, (b) guarantees full camera coverage under node failure, and (c) is validated on a real 3-Jetson testbed + a discrete-event simulator — producing a publishable full paper for top venues.
>
> **Paper working title:** *Stability-Aware Decentralized Coordination for Resilient Real-Time Video Analytics at the Traffic-Edge*

---

## 0. Decisions Locked In

| # | Decision | Choice |
|---|----------|--------|
| Novelty focus | system stability + no overload + full coverage under failure | **Control-theoretic** (hysteresis + feedforward + Lyapunov) |
| Evaluation | repeatable + credible | **Hybrid: discrete-event simulator + 3-Jetson testbed** |
| Theory depth | top-venue grade | **Full Lyapunov stability argument** + empirical bound validation |
| Simulator deps | portability | **Plain Python + NumPy/pandas** (no SimPy) |
| Data source | per constraint | **All workload data derived from camera videos** |
| Oracle baseline | needs future knowledge | **Simulator-only** (not on testbed) |

### Open questions to confirm before/early in execution
- **Q1 (hero metric):** recommended = `M_cyc` (migrations/cycle) **+** `T_ovl` (overload-time fraction). Coverage `Δτ`/`D` as secondary headline.
- **Q2 (traffic ground truth):** extract per-camera vehicle-density time series offline from the actual `.mp4` clips (preferred) vs. synthesize cycles matched to observed density statistics. Plan supports both; offline extraction is Stage 7 deliverable feeding Stage 3.
- **Q5 (standby pre-warming):** include as a *studied variable* in Stage 6 (on/off ablation), default off.

---

## 1. System As-Built (verified by full code read)

### 1.1 Topology
- **Edge (`Edge/`):** Jetson Orin nodes. DeepStream pipeline (YOLO PGIE → NvDCF tracker → LPD SGIE → LPR SGIE → nvdsanalytics → tiler → OSD → sink). Brokerless **Zenoh peer mode** for all P2P.
- **Server (`Server/`):** aiohttp aggregator (`app.py`), `EdgeRegistry`, `ViolationStore`; MediaMTX relay (RTSP→WHEP).
- **Camera (`Camera/`):** Docker + ffmpeg RTSP simulator (runs on the Jetsons per deployment notes).

### 1.2 Where the load-sharing logic actually lives (exact anchors)
All decision logic is isolated in **`Edge/speedflow_python/peer_orchestrator.py`** (1589 lines):

| Concern | Method | Line |
|---|---|---|
| Per-node state vector | `PeerState` (dataclass) | 60 |
| Migration audit log | `MigrationLogger` (CSV) | 86 |
| Overload classification | `_is_overloaded(load_score, risk_index)` | 438 |
| Effective load (logging/RFO) | `_effective_load` | 470 |
| Decision loop (1 s tick) | `_decision_loop` | 485 |
| Offline-peer detection → failover | `_check_offline_peers` | 498 |
| Return rescued cams | `_check_rebalance` | 543 |
| Reclaim migrated-out cams | `_check_reclaim` | 588 |
| **Self-overload + escalation 3→2→1** | `_check_self_overload` | 698 |
| Legacy L1 trigger | `_trigger_level1_if_due` | 819 |
| **Target peer selection (greedy least-load)** | `_pick_best_peer` | 854 |
| RFO send + vote window | `_trigger_rfo` | 885 |
| Vote close + winner election | `_close_vote_window` | 941 |
| ε-constraint check + **bid `F(x)`** | `_evaluate_and_bid` | 1027 |
| Make-before-break migration | `_wait_and_remove` | 1187 |
| **Leaderless failover (consistent hash)** | `_leaderless_failover` | 1338 |
| Consistent hash | `_consistent_hash` | 1330 |
| RTT reachability | `_measure_rtt` | 1451 |
| **Camera-to-offload pick (highest FPS)** | `_pick_camera_to_offload` | 1568 |

**Bid score today (crude):** `F(x) = self_load + (100 - self_load) * 0.25` (`_evaluate_and_bid`, L1112).

### 1.3 Load math (centralized, reusable)
**`Edge/speedflow_python/load_model.py`** — pure functions, hardware-free, already unit-tested:
- `theta_thermal(gpu_temp_c, cfg)` — smooth thermal ramp 75→90 °C.
- `compute_h_reactive(gpu,cpu,ram,temp,cfg)` — `max(R_GPU,R_CPU,R_RAM)·Θ`.
- `compute_l_proactive(feature_stats,cfg)` — `(W_base + Σ α₁N+α₂N²+βP+γS)/100`.
- `fuse(L,H)` — noisy-OR `U = 1−(1−L)(1−H)`.
- `CycleSmoother` — sliding-window mean (≈ one signal cycle).
- `ProactiveModel` — wraps three smoothers; `compute()` returns heartbeat dict.

**`Edge/health_agent.py`** — `_compute_load_score(metrics, fps_stats)` (L168): adaptive ω weights (`weights_normal`/`weights_bandwidth`) + FPS penalty (cap `fps_penalty_max`). `_select_omega` (L142). Heartbeat published every `HEALTH_INTERVAL` (2 s) on `peers/status/{node_id}`.

### 1.4 Capacity model (data-driven)
`edge_node.yml`:
- `p2p.fps_model`: `{streams_after: predicted_fps}` → `{1:30, 2:25, 3:20.5, 4:12.5}`.
- `p2p.eps_streams_max: 4` (hard capacity).
- `load_score.weights_*`, `fps_penalty_max`.
- `proactive.*` — **currently `enabled: false`, all coefficients 0.0** (must calibrate in Stage 7).

### 1.5 Workload features (the disturbance signal)
`SpeedProbe` (`probes.py`) computes per-frame, per-camera and flushes every 2 s to `FPS_STATS_FILE` (`/dev/shm/speedflow_fps.json`):
- `n_track` (active vehicle tracks), `n_plate`, `stationary_fraction` (speed < 3 km/h).
- `_flush_features` (L355), `_fps_writer_loop` (L334).
- Read by `health_agent._read_feature_stats` (L76) and `tools/profile_collect.py`.

### 1.6 Anti-thrashing today (the weakness to replace)
- `overload_duration_s: 35` (must be overloaded this long), `cooldown_s: 120` (per-camera), `_vote_in_progress` set, `offload_level_cooldown_s: 12`.
- **No hysteresis dead-band, no prediction, no cluster-headroom check.** Blunt long cooldowns are the only damping.

### 1.7 Instrumentation status
- **Present:** `MigrationLogger` CSV (`logs/p2p_migrations.csv`) with `migration_time_ms`, `blind_spot_ms`, `trigger_reason`, `result`; heartbeats with `risk_index`/`load_score`/`fps_per_camera`; `tools/profile_collect.py`, `tools/fit_coefficients.py`, `tools/plot_burst.py`, `tools/plot_rmse.py`.
- **Half-wired:** `Δτ` blind-spot — `_first_valid_speed_ts` recorded (probes.py L189) but `blind_spot_ms=None` never filled (orchestrator L1254).
- **Missing entirely:** **per-camera processed-frame / dropped-frame counter** (`D`). No node knows how many camera frames went unprocessed.
- **Tests:** only `tests/test_load_model.py` (pure functions). Orchestrator/failover/policy: zero coverage.

---

## 2. Scientific Contributions

- **C1 — Predictive, signal-cycle-aware overload avoidance (feedforward).** Forecast the red/green-driven load surge from the live `n_track`/`stationary_fraction` series and pre-shed load *before* breach, with lead time ≥ migration latency. Builds on `ProactiveModel`/`CycleSmoother`; adds an explicit phase predictor.
- **C2 — Stability guarantee (anti-thrashing).** Formalize offload as a hysteresis-damped feedback controller; define Lyapunov energy `V(t)=Σ max(0,Uᵢ−U_target)²`; prove `V` is non-increasing between disturbances and that migrations-per-cycle `M_cyc` is bounded by the dead-band width. Replaces the blunt cooldowns.
- **C3 — Coverage-preserving leaderless failover.** Quantify and minimize the camera blind-spot `Δτ` and unprocessed-frame count `D`; deterministic consistent-hash rescue + optional standby pre-warming; maintain a cluster coverage SLA ≈ 100% under single-node failure.

**Gap vs. literature** (arXiv survey): ENTS (SEC'22) centralized/throughput; Utility-Aware Load Shedding (2023) drops frames; RL LB (2024) cloud/centralized; SDN failover (2023) needs central controller; MHP2P (2021) sim-only no CV. **None combine brokerless P2P + real-time DNN video + signal-cycle prediction + failover continuity + stability proof.**

---

## 3. Control-Theoretic Formalization (Stage 0)

- **Plant:** cluster of N nodes; per-node state = `PeerState` fields (`peer_orchestrator.py:60`).
- **Measured state `Uᵢ(t)`:** cycle-smoothed `risk_index` (already `PeerState.risk_index`, L79; produced by `ProactiveModel`).
- **Disturbance `d(t)`:** per-camera `n_track(t)`, `stationary_fraction(t)` — periodic (signal cycle) with drift; from `SpeedProbe._flush_features`.
- **Actuator `a(t)`:** `set_offload_level(camera, level, target)` (L306) + RFO/migration; offload levels 1 (stream), 2 (vehicle crop), 3 (plate crop).
- **Control objective:** keep all `Uᵢ(t) < U_max` AND coverage = 100%, minimizing actuation count.
- **Controller laws:**
  1. **Hysteresis (Schmitt trigger):** offload when `U > U_high`; reclaim only when `U < U_low`; `U_low < U_high` dead-band prevents ping-pong.
  2. **Feedforward:** predict `Û(t+Δ)` from cycle phase; trigger when predicted breach is within migration-latency horizon.
  3. **Cluster-headroom constraint:** never offload to a peer whose post-acceptance `U` would breach `U_max` (prevents cascade → coverage guarantee).
- **Lyapunov function:** `V(t) = Σ_i max(0, Uᵢ(t) − U_target)²`. Claim: between disturbance steps the controller drives `ΔV ≤ 0`; dead-band ⇒ finite switching ⇒ `M_cyc` bounded.
- **Deliverable:** `docs/CONTROL_DESIGN.md` (block diagram, assumptions, proof sketch).

---

## 4. Evaluation Standard (Stage 1)

### 4.1 Metrics (`docs/EVALUATION_PROTOCOL.md`)
| Metric | Symbol | Definition | Source |
|---|---|---|---|
| Overload time fraction | `T_ovl` | % wall-time any node has `U > U_max` | heartbeat `risk_index` log |
| Peak cluster load | `U_peak` | max `Uᵢ` over run | heartbeat |
| **Migrations per cycle** | `M_cyc` | migrations ÷ #signal-cycles | `p2p_migrations.csv` |
| **Unprocessed frames** | `D` | camera frames no node processed within deadline | **new counter (Stage 6)** |
| **Coverage blind-spot** | `Δτ` | time a camera unprocessed during migration/failover | complete wiring (Stage 6) |
| Coverage under failure | `Cov_f` | % cameras processed T s after a node dies | heartbeat-derived |
| Proactive lead time | `L_lead` | s by which proactive trigger precedes reactive breach | extend `plot_burst.py` |
| End-to-end FPS | `FPS` | processed framerate per camera | FPS stats file |
| Detection quality | — | vehicle count / plate accuracy vs labeled clips | manual labels |

### 4.2 Scenario suite (`sim/scenarios/` + testbed scripts)
- **S1 Steady** — constant moderate density.
- **S2 Cyclic** — density oscillates on fixed 90 s red/green period.
- **S3 Drifting-cycle** — period varies 60–120 s.
- **S4 Burst** — sudden incident surge.
- **S5 Failure-injection** — any of S1–S4 + node killed at a known time.

Each run emits `run_meta.json` (policy, scenario, seed, config snapshot) for reproducibility.

---

## 5. Stage-by-Stage Execution

### Stage 2 — Pluggable Coordination Policy (enabler; lowest-risk high-value refactor)

**New package `Edge/speedflow_python/policies/`:**

```
policies/
  __init__.py
  base.py            # CoordinationPolicy ABC (pure, snapshot->decision)
  epsilon.py         # EpsilonConstraintPolicy  (== current behavior)
  static_nop.py      # StaticPolicy             (no migration)
  reactive_greedy.py # ReactiveGreedyPolicy     (threshold + least-loaded)
  oracle.py          # OraclePolicy             (sim-only, future knowledge)
  stability.py       # StabilityAwarePolicy     (C1+C2+C3 — filled Stages 4-5)
```

**`base.py` interface (pure functions; orchestrator keeps all threading/locks):**
```python
@dataclass(frozen=True)
class SelfSnapshot:        # built under _self_lock by orchestrator
    node_id: str
    load_score: float
    risk_index: float
    avg_fps: Optional[float]
    fps_per_camera: Dict[str, float]
    active_cameras: List[str]
    overload_since: Optional[float]

@dataclass(frozen=True)
class PeerSnapshot:        # one per known peer, built under _lock
    node_id: str
    load_score: float
    risk_index: float
    active_cameras: List[str]
    max_streams: int
    last_seen: float
    penalty_until: float

class CoordinationPolicy(ABC):
    name: str
    def is_overloaded(self, s: SelfSnapshot, now: float, cfg: dict) -> bool: ...
    def overload_sustained(self, s: SelfSnapshot, now: float, cfg: dict) -> bool: ...
    def pick_camera_to_offload(self, s: SelfSnapshot) -> Optional[str]: ...
    def pick_target_peer(self, peers: List[PeerSnapshot], camera: str,
                         now: float, cfg: dict) -> Optional[str]: ...
    def bid_score(self, s: SelfSnapshot, predicted_fps: float, cfg: dict) -> float: ...
    def should_reclaim(self, s: SelfSnapshot, migrated_out: Dict[str, str],
                       now: float, cfg: dict) -> Optional[str]: ...
```

**Refactor of `PeerOrchestrator`:**
- `__init__`: instantiate `self._policy = make_policy(cfg.get("coordination", {}).get("policy", "epsilon"), cfg)`.
- `_is_overloaded` → delegate to `self._policy.is_overloaded(snapshot, now, cfg)`.
- `_check_self_overload` (L698): keep the snapshot capture under `_self_lock`; replace inline threshold/escalation with policy calls.
- `_pick_camera_to_offload` (L1568) → `self._policy.pick_camera_to_offload(snapshot)`.
- `_pick_best_peer` (L854) → `self._policy.pick_target_peer(peer_snaps, camera, now, cfg)`.
- `_evaluate_and_bid` (L1112) bid → `self._policy.bid_score(snapshot, predicted_fps, cfg)`.
- `_check_reclaim` (L588) trigger condition → `self._policy.should_reclaim(...)`.
- **Concurrency rule:** policies receive immutable snapshots and return decisions only. All Zenoh I/O, timers, locks, make-before-break stay in the orchestrator. `EpsilonConstraintPolicy` must reproduce current numeric behavior exactly.

**Config (`edge_node.yml`):**
```yaml
coordination:
  policy: epsilon            # epsilon | static | reactive_greedy | oracle | stability
```

**Tests:**
- `tests/test_policies.py` — deterministic per-policy decisions on crafted snapshots.
- `tests/test_orchestrator_regression.py` — assert `EpsilonConstraintPolicy` outputs match the pre-refactor inline logic (golden cases for `is_overloaded`, `bid_score`, `pick_*`).

**Exit:** all 5 policies selectable; epsilon regression green; existing system behavior unchanged.

---

### Stage 3 — Discrete-Event Cluster Simulator (`sim/`)

```
sim/
  __init__.py
  load_model_bridge.py  # imports Edge/speedflow_python/load_model.py verbatim
  capacity.py           # (cameras, traffic) -> fps -> load_score -> risk_index
  traffic_model.py      # S1-S5 generators of n_track/n_plate/stationary_fraction
  policy_adapter.py     # runs the SAME CoordinationPolicy classes
  cluster_sim.py        # discrete-event loop (1s tick mirrors _decision_loop)
  metrics.py            # emits Stage-1 metrics to CSV
  run_sweep.py          # scenarios x policies x seeds -> logs/sim/*.csv
  scenarios/            # YAML scenario definitions (S1..S5)
```

- **`capacity.py`** reuses `fps_model` + `_compute_load_score` logic so sim load == real load formula.
- **`load_model_bridge.py`** imports `compute_l_proactive`, `compute_h_reactive`, `fuse`, `CycleSmoother`, `ProactiveModel` directly (no duplication; sim and real share math).
- **`cluster_sim.py`** models: heartbeat exchange, 1 s decision tick, migration latency + Δτ (distributions measured in Stage 7), node-kill injection.
- **`policy_adapter.py`** builds `SelfSnapshot`/`PeerSnapshot` from sim state → calls the real policy → applies the decision in sim. This means **the simulator validates the actual decision code**, not a reimplementation.

**Exit:** sim reproduces a hand-computed overload scenario; baselines behave as expected (static overloads; oracle best; greedy thrashes more than stability).

---

### Stage 4 — C1: Phase-Aware Predictor

**New `Edge/speedflow_python/phase_predictor.py`:**
```python
class CyclePhasePredictor:
    """Estimate signal-cycle period & phase from a scalar series (n_track or U)
    and forecast U_hat(t + horizon_s). Lightweight: autocorrelation/FFT-peak,
    no heavy ML (keeps the stability story clean)."""
    def update(self, value: float, ts: float) -> None: ...
    def period_s(self) -> Optional[float]: ...
    def forecast(self, horizon_s: float) -> float: ...
```

- Integrate into `StabilityAwarePolicy.is_overloaded`: trigger when `forecast(horizon = migration_latency) > U_high`.
- Upgrade `bid_score`: predicted post-migration risk via `fps_model` lookup + forecast, instead of `load + (100-load)*0.25`.

**Exit:** in S2/S3 sim, proactive trigger precedes reactive breach by ≥ migration latency; forecast RMSE reported.

---

### Stage 5 — C2: Hysteresis Controller + Stability Proof

**`StabilityAwarePolicy` (`policies/stability.py`):**
- Dual-band: offload if `U > u_high`; reclaim if `U < u_low`.
- Cluster-headroom: in `pick_target_peer`, reject peers whose predicted post-acceptance `U ≥ u_max`.
- Optional damping: minimum inter-migration interval derived from band width (replaces fixed `cooldown_s`).

**Config (`edge_node.yml coordination:`):**
```yaml
coordination:
  policy: stability
  u_high: 0.80          # offload band
  u_low:  0.55          # reclaim band (dead-band = u_high - u_low)
  u_max:  0.90          # hard ceiling / headroom constraint
  forecast_horizon_s: 6 # >= measured migration latency
  headroom_margin: 0.05
```

- **Proof (paper + `docs/CONTROL_DESIGN.md`):** show `ΔV ≤ 0` between disturbances; bound `M_cyc ≤ f(dead-band, disturbance amplitude)`.
- **Empirical validation:** sim sweep confirms measured `M_cyc` ≤ theoretical bound; compare `T_ovl`, `U_peak`, `M_cyc` across all 5 policies.

**Exit:** stability policy shows fewer migrations AND lower overload than epsilon baseline in sim; bound holds empirically.

---

### Stage 6 — C3: Coverage & Frame-Drop Accounting

**6a. Frame-drop counter `D` (new instrumentation):**
- Extend `SpeedProbe`: per-camera processed-frame counter (increment in `osd_sink_pad_buffer_probe`, probes.py:590).
- Write counts into `FPS_STATS_FILE` via `_fps_writer_loop` (probes.py:334) alongside existing FPS/features.
- A coverage tracker (in sim + an analysis script) compares processed frames vs expected `fps × Δt` per camera over each interval → `D` and `Cov_f`.

**6b. Complete Δτ wiring:**
- In `_wait_and_remove` (orchestrator L1187) `blind_spot_ms` is currently `None` (L1254). Fill it using `_migration_complete_ts` (L1257) and the winner's `_first_valid_speed_ts` (probes.py L189), surfaced via heartbeat. Add `_update_blind_spot()` referenced in the existing comment but not implemented.

**6c. Failover experiments:**
- Sim: node-kill injection (S5); measure `Δτ`, `D`, `Cov_f`.
- Testbed: `scripts/kill_node.sh` to SIGKILL a Jetson's pipeline at a known time.
- **Standby pre-warming ablation (Q5):** optional pre-declared standby stream for hashed rescuer to shrink `Δτ`; measure GPU cost vs `Δτ` reduction.

**Exit:** `Cov_f ≈ 100%` with bounded measured `Δτ`; `D` quantified vs static baseline (which loses dead node's cameras entirely).

---

### Stage 7 — Testbed Validation (3 Jetson, 6 cameras)

**7a. Calibration (REQUIRED — coefficients are 0.0 / disabled today):**
1. `python3 tools/profile_collect.py --wbase --wbase-output logs/wbase.txt`
2. `python3 tools/profile_collect.py --output logs/calibration.csv --duration 600` (run with varying traffic density).
3. `python3 tools/fit_coefficients.py --csv logs/calibration.csv --wbase <W_base> --output configs/edge_node.yml`
4. Set `proactive.enabled: true`, restart.

**7b. Latency calibration:** measure real migration latency + Δτ distributions on the LAN → feed `sim/cluster_sim.py` network model (closes sim↔real loop).

**7c. Traffic extraction (Q2):** offline-process the `.mp4` clips to produce per-camera vehicle-density time series → drive `sim/traffic_model.py` scenarios realistically and define testbed playback order.

**7d. Runs:** S1–S5 × {static, reactive_greedy, epsilon, stability} on hardware; collect `p2p_migrations.csv`, heartbeat logs, FPS+drop stats. (Oracle = sim only.)

**7e. Reconciliation:** report sim-vs-real gap with stated tolerance.

**Exit:** real-hardware trends match sim within tolerance; calibrated `edge_node.yml` committed.

---

### Stage 8 — Paper Artifacts

- Extend `tools/plot_burst.py` / `plot_rmse.py` →
  - Ablation table (5 policies × 5 scenarios × metrics).
  - `V(t)` stability trajectory plots.
  - `L_lead` CDF (proactive vs reactive).
  - Coverage-under-failure timeline (`Cov_f`, `Δτ`).
  - Sim-vs-real validation scatter.
- Related-work comparison table (from arXiv survey).
- Reproducibility appendix: run manifests, seeds, config snapshots.

---

## 6. Cross-Cutting: Testing (currently near-zero on orchestrator)

| Test file | Purpose |
|---|---|
| `tests/test_load_model.py` | exists — pure load math |
| `tests/test_policies.py` | each policy deterministic on snapshots |
| `tests/test_orchestrator_regression.py` | epsilon policy == pre-refactor behavior |
| `tests/test_speedflow_c_parity.py` | C extension vs Python fallback agree (protects sim math) |
| `tests/test_sim_smoke.py` | simulator runs S1–S5 without error; metrics emitted |
| `tests/test_phase_predictor.py` | period/phase recovery on synthetic cyclic signal |

Run: `cd Edge && python3 -m pytest tests/ -v`.

---

## 7. Dependency Graph & Order

```
Stage 0 (control theory note)
   |
Stage 1 (metrics + scenarios)
   |
Stage 2 (pluggable policy refactor) ---> Stage 3 (simulator)
                                            |
Stage 4 (phase predictor) --> Stage 5 (hysteresis + proof)
                                            |
                              Stage 6 (coverage + frame-drop)
                                            |
                              Stage 7 (testbed validation)
                                            |
                              Stage 8 (paper artifacts)
```

Testing is incremental: add the relevant test file in the same stage as each component.

---

## 8. File Change Inventory (new vs modified)

**New files:**
- `docs/CONTROL_DESIGN.md`, `docs/EVALUATION_PROTOCOL.md`
- `Edge/speedflow_python/policies/{__init__,base,epsilon,static_nop,reactive_greedy,oracle,stability}.py`
- `Edge/speedflow_python/phase_predictor.py`
- `sim/{__init__,load_model_bridge,capacity,traffic_model,policy_adapter,cluster_sim,metrics,run_sweep}.py`, `sim/scenarios/*.yml`
- `scripts/kill_node.sh`
- `Edge/tests/{test_policies,test_orchestrator_regression,test_speedflow_c_parity,test_sim_smoke,test_phase_predictor}.py`

**Modified files:**
- `Edge/speedflow_python/peer_orchestrator.py` — delegate decisions to `self._policy`; complete `blind_spot_ms` wiring.
- `Edge/speedflow_python/probes.py` — processed-frame counter into FPS stats.
- `Edge/configs/edge_node.yml` — add `coordination:` block; calibrate `proactive:`.
- `Edge/tools/plot_burst.py`, `plot_rmse.py` — paper figures.

**Invariant:** default config (`coordination.policy: epsilon`, `proactive.enabled: false`) reproduces today's behavior byte-for-byte — every change is additive/opt-in until experiments switch policies.

---

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Refactor breaks delicate orchestrator threading/locks | Policies are pure snapshot→decision; all I/O/locks/timers stay in orchestrator; regression test guards epsilon. |
| Sim diverges from hardware | Sim imports real `load_model.py`, real `fps_model`, real policy classes; latency distributions measured on testbed. |
| Proactive coefficients unfit (0.0 today) | Stage 7 calibration is a hard prerequisite for C1; epsilon/reactive baselines work without it. |
| `Δτ`/`D` instrumentation gaps | Stage 6 explicitly adds the counter and completes `blind_spot_ms`. |
| Lyapunov assumptions too strong | State assumptions explicitly (bounded disturbance amplitude/period drift); back proof with empirical bound check. |

---

## 10. Next Action (on build start)

1. Create `docs/CONTROL_DESIGN.md` + `docs/EVALUATION_PROTOCOL.md` skeletons (Stages 0–1).
2. Scaffold `Edge/speedflow_python/policies/` with `base.py` + `epsilon.py` and the regression test, **without** yet rewiring the orchestrator (safe, additive).
3. Rewire `PeerOrchestrator` to delegate, behind `coordination.policy` (default epsilon).
4. Stand up `sim/` skeleton importing `load_model.py`; smoke test S1.

Confirm Q1/Q2/Q5 to finalize metric headline and traffic-data approach; everything else is specified above.
