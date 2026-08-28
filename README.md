# IoT Graduate — Distributed Edge Traffic Monitoring

Experimental distributed traffic-monitoring system for NVIDIA Jetson edge devices.
Detects vehicles, tracks them, estimates speed via homography, reads license plates,
records violations, and redistributes camera workloads between Jetson nodes under load.

> **Status:** research prototype. Designed for a trusted LAN; not hardened for open networks.

---

## Architecture — Three Parts

```
Camera/              Edge/ (Jetson A / B / C)          Server/
MediaMTX RTSP   →    ONE process per Jetson:        →  MediaMTX relay (:8554, RTSP push)
Docker sim           DeepStream pipeline               Zenoh router :7447 (optional relay)
                     + HealthAgent  (in-process)       aiohttp dashboard :9090
                     + PeerOrchestrator                   WebSocket to browsers
                     + Zenoh command/offload subs         Violation store (JSON)
```

**Camera** — Docker Compose runs MediaMTX (`:8554`) + FFmpeg containers that loop MP4
files as RTSP streams simulating live cameras.

**Edge** — Each Jetson runs a **single Python process** (`run_edge.sh` → `main.py`
→ `run_python_mode()`). Inside that one process:

1. The DeepStream pipeline (below).
2. `HealthAgent` — collects Jetson metrics (jtop), computes `load_score`,
   publishes the **sole** heartbeat on Zenoh, forwards overspeed events to Server.
3. `PeerOrchestrator` — local load decisions, escalation ladder, RFO voting,
   failover rescue, reclaim.
4. `ZenohCommandSubscriber` / `OffloadPublisher` / `OffloadReceiver` — camera
   ADD/REMOVE commands and crop-offload data plane.

All components share **one Zenoh session**. A single heartbeat publisher exists per
node; duplicate publishers were removed deliberately.

**Server** — aiohttp app (`:9090`): WebSocket hub receiving health + overspeed payloads
from all Jetson nodes; serves a browser dashboard; stores violation records; optionally
runs a Zenoh router (`ZENOH_ROUTER=tcp/<server>:7447`) so cross-subnet nodes converge
without relying on multicast alone.

---

## Jetson DeepStream Pipeline

```
N × uridecodebin
      │
  nvstreammux  (1920×1080 mux, up to max_streams=8)
      │
   PGIE  (YOLO11 — vehicle detection)
      │
  NvDCF tracker
      │
  SGIE1 (LPD — license plate detection)
      │
  SGIE2 (LPR — license plate recognition)
      │
 nvdsanalytics  (ROI polygon, line crossing)
      │
  SpeedProbe (GLib buffer probe)
      │
  rtsp_push sink → MediaMTX relay on Server
```

Per-camera homography maps pixel displacement to world coordinates (meters);
median-filtered speed estimate triggers an overspeed event when `speed_kmh > SPEED_LIMIT_KMH`.

The SpeedProbe writes a unified JSON snapshot to `/dev/shm/speedflow_fps.json` every 1 s
(atomic tmp+rename). `HealthAgent` reads it each cycle with sequence + session-id +
staleness guards and rejects stale or non-advancing payloads. All readers sample on the
same 1 s cadence as the writer (`HEALTH_INTERVAL=1.0`) — mismatched cadences silently
drop windows.

Note: per-camera FPS after `nvstreammux` does not exist — GPU saturation throttles all
streams together. Workload counts (`n_track`, `n_plate`) are the per-camera signal.

---

## Load Score — FPS-Primary Composite

`load_score` is a bounded 0–100 FPS-dominant scalar driving all decisions.

**FPS piecewise-linear curve** (input clamped to `[0, TARGET_FPS=27]`):

| avg FPS | score | level anchor |
|---------|-------|--------------|
| ≥ 27    |   0   | healthy      |
| 22      |  57   | near L3      |
| 19      |  65   | L2 threshold |
| 17      |  75   | near L1      |
| 0       | 100   | unavailable  |

Linear interpolation between anchors; source-starved cameras excluded from the average;
burst clamped to configured nominal FPS.

**Additive bonuses** (config-gated in `edge_node.yml → load_score:`):
- `workload`: ramp on total tracked+plate objects across active non-starved cameras,
  up to `max_bonus` (default 10.0) at `capacity`.
- `thermal`: ramp on `gpu_temp_c` from onset (~70 °C) to critical (~85 °C), up to +5.0.
- `composite = min(100, fps_score + workload_bonus + thermal_bonus)`

**Hardware fuse floor** (non-additive): if CPU ≥ 90 OR RAM ≥ 90 **and**
`avg_fps < TARGET_FPS − 2`, then `score = max(composite, hw_fuse_score_floor=80.0)`.
GPU utilisation is excluded from scoring entirely (burst-aliased, DVFS-conflated);
telemetry only.

Every health cycle publishes `load_score_breakdown` with the full decomposition.

### Workload-primary QoS mapping

`HealthAgent` smooths telemetry with EMA(α=0.33) over total objects + valid FPS and maps
to QoS states: low workload → healthy, moderate → 30–55, high workload with degraded
FPS → degraded, severe → overloaded/critical. Orchestration honours recognized states;
blank/unrecognized states fall back to legacy `load_score` behaviour.

---

## Multi-Level Offload Policy

Each Jetson runs an independent `PeerOrchestrator`; no master node. Decisions are
strictly local; Zenoh is only the message layer.

### Escalation ladder (current config values)

| Level     | Threshold | Sustained | Dwell | Action |
|-----------|-----------|-----------|-------|--------|
| L3        | ≥ 60.0    | 5 s       | 10 s before L2 | Offload **plate crops** (JPEG ~1–3 KB) to best peer |
| L2        | ≥ 67.0    | —         | 17 s before L1 | Offload **vehicle crops** (~15–40 KB) for LPD+LPR |
| L1        | ≥ 76.0    | —         | —     | Full-stream **camera migration** via RFO vote |
| Reclaim   | < 45.0 (= 60 − `reclaim_margin` 15) | stable 30 s | — | Take back migrated cameras |

L3/L2 crop offload does **not** reduce source-node pipeline load (PGIE + tracker still
process full frames); only L1 migration actually sheds stream load. Escalation clears
lower levels; recovery below all thresholds resets the level to 0.

### Camera selection & ownership policy (hard rules)

- Per-camera ranking uses **workload** (`n_track + n_plate`), never post-mux FPS:
  L1 picks the *lightest* owned camera, L2/L3 pick the *heaviest* camera.
- **A node never migrates away a camera it does not own** (ownership defined by its own
  `cameras.yml`). Foreign/rescued cameras are skipped for L1; they may only return to
  their owner via the owner's reclaim path. If the escalation ladder locked onto a
  foreign camera during L2/L3, the L1 step swaps to an eligible owned camera instead.
- **A node always keeps ≥ 1 locally-owned camera active.** L1 returns no candidate when
  ≤ 1 owned camera remains.
- Ownership commits only after the receiver reaches PLAYING **and** the sender has
  removed the camera **and** the sender's remaining streams are verified healthy —
  an ADD ACK alone is not ownership.
- Migration/reclaim targets are validated against current cluster state (heartbeats),
  not local snapshots: RFO rejected if an alive peer already owns the camera,
  per-camera single-flight, dynamic holders yield to static owners.

### L1 migration flow (make-before-break)

1. Overloaded node broadcasts RFO on `peers/vote/request`.
2. Capable peers respond on `peers/vote/proposal` within `vote_window_s=5`;
   peers already ≥ L3 threshold are excluded as targets.
3. Winner selected by ε-constraint (FPS/load model, network RTT).
4. Winner receives decision on `peers/vote/decision`, ADDs the camera, waits for
   PLAYING (`add_ack_timeout_s=12`).
5. Winner ACKs on `peers/vote/ack/{cam}`.
6. Sender receives ACK → REMOVEs its local branch. Stream continuity preserved.
7. Rollback: no ACK within `migration_timeout_s=15` → sender aborts; repeated
   failures exclude a peer from selection (`zombie_timeout_count=3`).

Reclaim verifies the current holder via heartbeats, uses persistent attempt counters
(max 5), and gives up cleanly (state cleared, logged once) instead of looping forever.

### Failover rescue

Peers declare a node OFFLINE after `heartbeat_timeout_s=5` silence plus
`failover_grace_s=10` convergence grace (offline threshold ≈ 15 s); a further
convergence-grace window suppresses mutual false-offline cascades during failover.

Surviving nodes rescue orphaned cameras using **HRW/Rendezvous hashing**
(`sha256(camera_id:peer_id)`, highest weight wins) — only the dead node's cameras remap.
Each rescuer publishes a priority claim on `peers/failover/claim` (weight truncated to
15 hex digits for msgpack int64 safety), waits `rescue_claim_lease_s=15`, and yields to
higher-weight peers (split-brain guard). Orphan detection intersects the dead peer's
last-known `camera_configs`; empty `active_cameras` fields never suppress rescue nor
overwrite cached ownership.

### Result filtering

Crop offload results return on `offload/results/{receiver}/{sender}` with
`{stid, camera_id, frame_no, plate_text, confidence, inference_ok}`. The original node
filters by matching `stid + camera_id` against live tracks before inserting plate text
into violation records.

---

## Heartbeat & Transport Resilience

Lessons baked into code after field incidents:

- **Single publisher, shared session.** `HealthAgent`, `PeerOrchestrator`, and the
  command/offload subscribers share one Zenoh session; self-heartbeat loopback works
  without mesh reconvergence after restarts.
- **Publish-failure recovery.** A failed heartbeat/status put closes and undeclares the
  publisher, then reconnects immediately (throttle reset) instead of waiting out a retry
  interval. Send/error/consecutive-error counters and throttled warnings track transport
  health.
- **Self-stale gates.** When this node's own heartbeat is stale beyond the offline
  threshold, offline detection of peers is *skipped* — absence of peer updates over a
  broken transport must not produce false OFFLINE flags. The same gate suppresses
  overload decisions.
- **No lock-across-callbacks.** Camera ADD/REMOVE ACK waits run on a bounded thread
  pool; stream-pad operations release `CameraManager._lock` before blocking calls
  (the GLib main loop needs the same lock).

## Stream Lifecycle Robustness

- **Synchronous removal.** `dynamic_remove_stream()` tears down a branch synchronously
  on the GLib thread: sequential PLAYING→PAUSED→READY→NULL state walk with get_state
  waits (nvv4l2decoder requirement), unlink, mux request-pad release, element removal.
  No pad probes — buffer-dependent BLOCK_DOWNSTREAM probing leaked mux pads when data
  flow had stalled, which permanently broke re-ADDs.
- **Stale-source cleanup on ADD.** Re-adding a camera detects and tears down leftover
  elements/pads from a previous timed-out ADD before building a new source bin.
- **ADD timeout handling.** Timed-out configs are disabled and cleaned up; reclaim
  retries re-enable them.
- **Frozen-slot display fix.** After removal, a black `videotestsrc` fills the vacated
  tiler slot (live tiler grid resize crashes VIC on Jetson), removed again on reclaim.

---

## Zenoh Communication

Peer mode with UDP multicast scouting; if `ZENOH_ROUTER` is set, nodes also connect to
the router endpoint (used for the cross-subnet server link).

| Key expression pattern | Direction | Purpose |
|------------------------|-----------|---------|
| `peers/status/{node_id}` | broadcast | Health heartbeat (msgpack), sole publisher |
| `peers/vote/request` | broadcast | RFO — overloaded node requests offload |
| `peers/vote/proposal` | broadcast | Bid from capable peer |
| `peers/vote/decision` | broadcast | Winner selection |
| `peers/vote/ack/{cam_id}` | broadcast | Receiver confirms stream PLAYING |
| `peers/control/{node_id}` | directed | Camera ADD/REMOVE commands (+ACKs) |
| `peers/failover/claim` | broadcast | Rescue priority claims (HRW weights) |
| `traffic/events/{node_id}/**` | local | Pipeline → HealthAgent → Server (overspeed) |
| `offload/plates/{src}/{dst}` | directed | L3 plate crops |
| `offload/vehicles/{src}/{dst}` | directed | L2 vehicle crops |
| `offload/results/{recv}/{sender}` | directed | Inference results back to origin |

---

## B-side Offload Receiver

`OffloadReceiver` subscribes to:
- `offload/plates/*/{my_node_id}` — L3: run LPR engine on plate crop
- `offload/vehicles/*/{my_node_id}` — L2: run LPD then LPR engine on vehicle crop

TensorRT engines (`models/lpd.engine`, `models/lpr.engine`) are loaded lazily on first
request. Dynamic shapes: profile 0 MIN shape used for batch-1 inference (supports both
TensorRT 8 / JetPack 5 and TRT 10 / JetPack 6 APIs).

**Failed inference is not published.** Only successful runs (including a valid empty
observation) publish a result. Engine unavailable or inference exception → result
suppressed, error counter incremented, rate-limited warning logged.

---

## Logging

Dual handlers (configured in `run_python.py`):

- **Terminal:** WARNING and above only — operator sees errors and state changes.
- **File `/tmp/edge_debug.log`:** DEBUG+ — full diagnostic detail.

Additionally stdout/stderr tee into `Edge/logs/run_<timestamp>.log` per launch.

---

## Running the Edge Node

All Edge commands run from `Edge/` with the `DoAn` conda environment.

```bash
# Default: start pipeline + HealthAgent + PeerOrchestrator (one process), RTSP push mode
./run_edge.sh

# Display to screen
./run_edge.sh --mode display

# Custom RTSP push destination
./run_edge.sh --mode rtsp_push --rtsp-push-url rtsp://116.118.9.125:8554/jetson_A

# With workload/thermal bonus policy
./run_edge.sh --load-policy actual --load-model formula

# Collect calibration data (600 s alongside live pipeline)
./run_edge.sh --collect
./run_edge.sh --collect --collect-output logs/calibration.csv \
              --collect-duration 600 --collect-wbase-ref 12.5

# Full automated calibration (W_base → collect → fit → plot)
./run_edge.sh --calibrate

# Train DL load predictor from pre-collected CSVs (no pipeline needed)
./run_edge.sh --train-dataset csv_collected --load-model dl

# Stop: Ctrl+C (graceful shutdown, then SIGKILL)
```

`HEALTH_INTERVAL=1.0` is the only supported cadence (matches SpeedProbe writer).

**Tests** (from repo root):
```bash
conda run -n DoAn python3 -m pytest Edge/tests/ -q
conda run -n DoAn python3 -m py_compile Edge/speedflow_python/run_python.py
```

Deployment to Jetsons is git-based: push from host, `git pull` on device, restart
`run_edge.sh`. Verify deployed code matches host before analysing field logs.

---

## Camera (Simulation)

```bash
cd Camera
docker compose up -d        # starts MediaMTX :8554 + FFmpeg cam containers
docker compose down
```

Video files go in `Camera/videos/`. Edit `docker-compose.yml` or `.env` to add cameras.

> Note: Jetson kernel images without `veth.ko` cannot use Docker bridge networking;
> use `network_mode: host` there.

---

## Server

```bash
cd Server
python3 app.py              # dashboard at http://0.0.0.0:9090
```

Requires `Server/.env` with `SERVER_PORT`, `MEDIAMTX_API`, etc.
Violations stored as JSON in `Server/violations/`. The same host can run a Zenoh
router (`:7447`) for cross-subnet discovery.

---

## Key Configuration Files

| File | Purpose |
|------|---------|
| `Edge/.env` | Flat settings: `NODE_ID`, `TARGET_FPS=27`, `HEALTH_INTERVAL=1.0`, RTSP URLs, `ZENOH_ROUTER`, model paths |
| `Edge/configs/cameras.yml` | Per-camera RTSP URIs, homography, ROI, speed limit. Hot-reloaded (~100 ms via inotify). Defines **ownership**. |
| `Edge/configs/edge_node.yml` | P2P thresholds (L3=60/L2=67/L1=76), dwell timers, heartbeat/failover/grace windows, load_score bonuses, proactive model |
| `Server/.env` | `SERVER_PORT=9090`, `MEDIAMTX_API` |
| `Camera/.env` | RTSP port, video file paths for Docker sim |

---

## Proactive Model (Research, Disabled)

`edge_node.yml → proactive:` holds a formula-based predictor
(`W_base + Σ[α₁·N_track + α₂·N_track² + β·N_plate + γ·S]`) or a DL ONNX predictor.
Both are **disabled by default**; shadow mode emits risk telemetry without affecting
decisions. Research framing: predict sustained-FPS-threshold crossing (QoS degradation)
within short horizons — evaluated by lead time, F1, false alarms, missed events — not
by regression MAE/RMSE.

Promotion gate (not yet met): a forecaster must beat persistence consistently on fresh
Mode-A data (no offload/migration confounding) AND its lead time must exceed actual
migration/offload latency. Current result: Ridge beats persistence overall but loses
individual scenarios — nothing promoted; DLPredictor stays disabled, formula model
active.

`load_score` and object counts carry policy feedback once offload is active, so
training/evaluation uses Mode-A data only (confounded windows tagged or excluded).

---

## Jetson Lockup & Freeze Investigation

Safe, passive diagnostics and manual setup procedures for triaging Jetson kernel hangs or hardware freezes are documented in:
- `Edge/deploy/JETSON_LOCKUP_INVESTIGATION.md`
- Diagnostic collector: `Edge/tools/jetson_diag_collector.sh`
- Background passive metric ring-buffer: `Edge/tools/jetson_diag_watcher.sh`

---

## Known Constraints (Jetson-specific)

- Sequential decoder teardown only (PLAYING→PAUSED→READY→NULL with get_state waits);
  direct PLAYING→NULL leaves NVDEC registers undefined and can deadlock the kernel.
- Pad add/remove on a live `nvstreammux` can corrupt surface pools — removal walks
  element states to NULL before releasing pads.
- NVDEC hardware decode sessions cap around 16–32 per Orin; rapid churn needs gating.
- Live tiler grid resize crashes VIC — vacated slots are black-filled instead.
- Docker bridge requires `veth.ko`; absent on current flashed kernels — use host
  networking.

## Limitations / Unproven

- L3/L2 crop-offload end-to-end throughput under real thermal load is not benchmarked.
- Simultaneous multi-node failure recovery tested only up to three-node setups.
- Thermal/workload bonuses are config-gated; disable for pure FPS-dominant scoring.
- No TLS/auth on Zenoh, WebSocket, or RTSP endpoints — trusted LAN only.
