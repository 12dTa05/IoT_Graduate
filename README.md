# IoT Graduate — Distributed Edge Traffic Monitoring

Experimental distributed traffic-monitoring system for NVIDIA Jetson edge devices.
Detects vehicles, tracks them, estimates speed via homography, reads license plates,
records violations, and redistributes camera workloads between Jetson nodes under load.

> **Status:** research prototype. Designed for a trusted LAN; not hardened for open networks.

---

## Architecture — Three Parts

```
Camera/          Edge/ (Jetson A / B)       Server/
MediaMTX RTSP  →  DeepStream pipeline    →  MediaMTX relay (RTSP push)
Docker sim       + health_agent          →  aiohttp dashboard :9090
                 + PeerOrchestrator         WebSocket to browsers
                   (Zenoh peer-mode)
```

**Camera** — Docker Compose runs MediaMTX (`:8554`) + FFmpeg containers that loop MP4
files as RTSP streams simulating live cameras.

**Edge** — Each Jetson runs:
1. `health_agent.py` — collects Jetson metrics (jtop), computes `load_score`, publishes
   Zenoh heartbeats, forwards overspeed events to Server via WebSocket.
2. `main.py` (DeepStream pipeline) — multi-stream YOLO11 detection → NvDCF tracker →
   LPD secondary model → LPR secondary model → nvdsanalytics → speed calculation →
   violation snapshot. Outputs RTSP push to Server's MediaMTX relay.

**Server** — aiohttp app (`:9090`): WebSocket hub receiving health + overspeed payloads
from all Jetson nodes; serves a browser dashboard; stores violation records.

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

Pipeline writes a unified JSON snapshot to `/dev/shm/speedflow_fps.json` every 1 s.
`health_agent` reads this file atomically (sequence + session_id + `_updated_at`
staleness guard) and rejects stale or non-advancing payloads.

---

## Load Score — FPS-Primary Composite

`load_score` is the single number driving all offload decisions.

**FPS piecewise-linear curve** (input clamped to `[0, TARGET_FPS=27]`):

| avg FPS | score | level anchor |
|---------|-------|--------------|
| ≥ 27    |   0   | healthy      |
| 22      |  57   | L3 threshold |
| 19      |  65   | L2 threshold |
| 17      |  75   | L1 threshold |
| 0       | 100   | unavailable  |

Linear interpolation between anchors; source-starved cameras excluded from the average.

**Optional additive bonuses** (configured in `configs/edge_node.yml → load_score:`):
- `workload`: linear ramp on `n_track + n_plate` across active non-starved cameras,
  up to `max_bonus` (default 10.0) at `capacity` (default 40.0 objects).
- `thermal`: linear ramp on `gpu_temp_c` from `onset_c=70` to `critical_c=85`,
  up to `max_bonus=5.0`.
- `composite = min(100, fps_score + workload_bonus + thermal_bonus)`

**Hardware emergency floor** (post-composite):
If CPU ≥ `hw_fuse_threshold=90` OR RAM ≥ 90 **and** FPS is already degraded
(`avg_fps < TARGET_FPS − 2`), then `score = max(composite, hw_fuse_score_floor=75.0)`.
GPU utilisation is excluded from the fuse (burst-aliased by DeepStream).

Every health cycle publishes `load_score_breakdown` with fields:
`fps_score`, `workload_bonus`, `thermal_bonus`, `composite_score`, `load_score`.

---

## Multi-Level Offload Policy (A-side orchestrator)

Each Jetson runs an independent `PeerOrchestrator`; no master node.

### Load thresholds → escalation ladder

| Threshold | Score | Action |
|-----------|-------|--------|
| L3 = 57   | ≥ 57  | Offload **plate crops** (JPEG ~1–3 KB) to peer B via Zenoh |
| L2 = 65   | ≥ 65  | Offload **vehicle crops** (~15–40 KB) to peer B for LPD+LPR |
| L1 = 75   | ≥ 75  | Full-stream RTSP **camera migration** to peer B |
| Reclaim   | < 50  | Reclaim migrated cameras when load stable for `reclaim_stable_s=15` |

Escalation requires `overload_duration_s=7` of sustained overload before acting.
Dwell timers (`l3_dwell_s=10`, `l2_dwell_s=7`) prevent premature escalation.
`offload_level` in `edge_node.yml` controls which levels are active (default: 3).

### Make-before-break migration (L1)

1. A-side broadcasts RFO (Request For Offload) on `peers/vote/request`.
2. Capable peers respond on `peers/vote/proposal` within `vote_window_s=2`.
3. A-side selects winner by ε-constraint (FPS model, load, network RTT).
4. Winner receives decision on `peers/vote/decision`, ADDs camera, waits for PLAYING.
5. Winner ACKs on `peers/vote/ack/{cam}`.
6. A-side receives ACK → REMOVEs camera. Stream continuity preserved.
7. Rollback: if no ACK within `migration_timeout_s=15`, A-side aborts migration.

**Failover rescue:** Edge peers and the Server registry declare a node OFFLINE
after the shared `heartbeat_timeout_s=5` silence.
Surviving nodes wait a random jitter (`failover_jitter_max_s=3`) then ADD the
orphaned cameras from the deceased peer's last known `camera_configs`.

### A-side result filtering

When A-side offloads crops (L2/L3), `offload/results/{receiver}/{sender}` Zenoh
messages carry `{stid, camera_id, frame_no, plate_text, confidence, inference_ok}`.
A-side filters results by matching `stid + camera_id` against live tracks before
inserting plate text into the speed-violation record.

---

## B-side Offload Receiver

`offload_receiver.py` subscribes to:
- `offload/plates/*/{my_node_id}` — L3: run LPR engine on plate crop
- `offload/vehicles/*/{my_node_id}` — L2: run LPD then LPR engine on vehicle crop

TensorRT engines (`models/lpd.engine`, `models/lpr.engine`) are loaded lazily on the
first request. Dynamic shapes: profile 0 **MIN shape** is used for single-crop
batch-1 inference (supports both TensorRT 8 / JetPack 5 and TRT 10 / JetPack 6 APIs).

**Failed inference is not published.** Only successful runs (including a valid empty
observation — no plate detected) publish a result. Engine unavailable or inference
exception → result suppressed, `offload_inference_errors` counter incremented,
rate-limited warning logged.

---

## Zenoh Communication

All inter-node messaging uses Zenoh **peer mode** with UDP multicast scouting
(no broker required on the LAN).

| Key expression pattern | Direction | Purpose |
|------------------------|-----------|---------|
| `peers/status/{node_id}` | broadcast | Health heartbeat (msgpack) |
| `peers/vote/request` | broadcast | RFO — overloaded node requests offload |
| `peers/vote/proposal` | broadcast | Bid from capable peer |
| `peers/vote/decision` | broadcast | Winner selection |
| `peers/vote/ack/{cam_id}` | broadcast | Winner confirms stream PLAYING |
| `traffic/events/{node_id}/**` | local | Pipeline → health_agent → Server (overspeed) |
| `offload/plates/{src}/{dst}` | directed | L3 plate crop (A→B) |
| `offload/vehicles/{src}/{dst}` | directed | L2 vehicle crop (A→B) |
| `offload/results/{recv}/{sender}` | directed | Inference result (B→A) |

---

## Running the Edge Node

All Edge commands run from `Edge/` with the `DoAn` conda environment.

```bash
# Default: start health_agent + pipeline, RTSP push mode
./run_edge.sh

# Display to screen
./run_edge.sh --mode display

# Custom RTSP push destination
./run_edge.sh --mode rtsp_push --rtsp-push-url rtsp://192.168.212.21:8554/jetson_A

# With workload/thermal bonus policy
./run_edge.sh --load-policy actual --load-model formula

# Collect calibration data for load predictor (600 s alongside live pipeline)
./run_edge.sh --collect
./run_edge.sh --collect --collect-output logs/calibration.csv \
              --collect-duration 600 --collect-wbase-ref 12.5

# Full 6-step automated calibration (W_base → collect → fit → plot)
./run_edge.sh --calibrate

# Train DL load predictor from pre-collected CSVs (no pipeline needed)
./run_edge.sh --train-dataset csv_collected --load-model dl

# Stop: Ctrl+C (graceful 3 s shutdown, then SIGKILL)
```

`TELEMETRY_INTERVAL` is locked at `1.0 s` — the only supported cadence.

**Test commands** (DoAn conda env, from `Edge/`):
```bash
conda run -n DoAn python3 -m pytest tests/ -v
conda run -n DoAn python3 -m py_compile health_agent.py
conda run -n DoAn python3 -m py_compile speedflow_python/peer_orchestrator.py
```

---

## Camera (Simulation)

```bash
cd Camera
docker compose up -d        # starts MediaMTX :8554 + FFmpeg cam1/cam2 containers
docker compose down
```

Video files go in `Camera/videos/`. Edit `docker-compose.yml` or `.env` to add more cameras.

---

## Server

```bash
cd Server
python3 app.py              # dashboard at http://0.0.0.0:9090
```

Requires `Server/.env` with `SERVER_PORT`, `MEDIAMTX_API`, etc.
Violations stored as JSON in `Server/violations/`.

---

## Key Configuration Files

| File | Purpose |
|------|---------|
| `Edge/.env` | All flat settings: `NODE_ID`, `TARGET_FPS=27`, `HEALTH_INTERVAL=1.0`, RTSP URLs, model paths |
| `Edge/configs/cameras.yml` | Per-camera RTSP URIs, homography, ROI, speed limit. Hot-reloaded (~100 ms via inotify). |
| `Edge/configs/edge_node.yml` | P2P thresholds, offload levels, load_score bonuses, proactive model coefficients |
| `Server/.env` | `SERVER_PORT=9090`, `MEDIAMTX_API` |
| `Camera/.env` | RTSP port, video file paths for Docker sim |

---

## Proactive Load Model (Optional)

`edge_node.yml → proactive:` holds a formula-based predictor
(`W_base + Σ[α₁·N_track + α₂·N_track² + β·N_plate + γ·S]`) or a DL ONNX predictor.
Both are **disabled by default** (`enabled: false`). Shadow mode emits `risk_index`
telemetry without affecting decisions. To activate:

1. Run `./run_edge.sh --calibrate` to measure W_base, collect data, fit coefficients.
2. Edit `configs/edge_node.yml`: set `proactive.enabled: true`.
3. Restart with the chosen `--load-policy` and `--load-model`.

---

## Limitations / Unproven

- **L3/L2 offload** (crop Zenoh path): implemented and unit-tested; end-to-end
  field throughput under real Jetson thermal load is not benchmarked.
- **Proactive model** is disabled by default; coefficients are placeholders until
  calibration runs on the target hardware.
- **Full-cycle migration** (L1 make-before-break) is implemented; simultaneous
  multi-node failure recovery is not tested beyond two-node setups.
- **Thermal/workload bonus** sections are config-gated; set `enabled: false` to
  revert to pure FPS-dominant scoring.
- No TLS/auth on Zenoh, WebSocket, or RTSP endpoints — trusted LAN only.
