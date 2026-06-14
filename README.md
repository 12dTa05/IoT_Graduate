# IoT_Graduate — Decentralized Real-Time Traffic Monitoring with P2P Load Balancing

A production-grade distributed real-time traffic monitoring system that runs **AI inference (NVIDIA DeepStream with YOLO and custom LPR)** on multiple autonomous Jetson Edge nodes to detect vehicles, measure speed using perspective transform homography, and read license plates in real time.

**Architecture**: Jetson Edge nodes are **fully decentralized** and communicate via **Eclipse Zenoh** (peer mode with UDP multicast scouting) for P2P load balancing and failover. A **Central Monitoring Server** aggregates health metrics and violations for visualization and long-term storage. Each Edge pushes a composite RTSP stream to a central **MediaMTX** relay, which converts it to **WebRTC (WHEP)** for low-latency browser playback.

**Deployment target**: 4 Jetson Edge nodes at a signalized intersection, each handling 2 cameras (8 cameras total). Traffic load follows a deterministic 60–90 second signal cycle (red-phase queue buildup → green-phase discharge), which the proactive load model explicitly accounts for.

---

## System Overview: How It Works

### 1. Vehicle Detection & Speed Measurement

Each Jetson Edge node runs a **real-time DeepStream GStreamer pipeline** that processes multiple RTSP camera sources simultaneously:

- **Vehicle Detection**: YOLO detector (primary inference engine) identifies vehicles in each frame and assigns a unique track ID via NvDCF tracker.
- **Speed Measurement**: Using **homography matrix perspective transform** (calibrated per camera), the system converts pixel-space vehicle positions to real-world coordinates (meters). Velocity is computed as `Δworld_Y / Δtime × 3.6` to convert m/s to km/h.
- **Validation**: Speed measurements are validated against multiple filters, configured in `Edge/.env`: minimum world displacement (`MIN_WORLD_DISPL_M=0.5`m), bounding box area stability (`BBOX_AREA_JUMP=2.5`× max change), detection confidence (`MIN_DET_CONF=0.45`), maximum plausible speed (`MAX_ABS_KMH=160`), and minimum track age (0.5s, derived as `VIDEO_FPS × 0.5` frames).
- **Smoothing**: Raw speed samples are smoothed with a median filter over a `MEDIAN_WINDOW=5` sample window (smoothing kicks in once ≥3 samples are collected) to reject noise and outliers.

### 2. License Plate Detection & Recognition (LPR)

**License Plate Detection (LPD)**: A secondary classifier (SGIE) marks potential license plate regions in the vehicle bounding box.

**License Plate Recognition (LPR)**: A tertiary OCR classifier reads the text from detected plates. The system employs a **frame accumulation strategy**: it collects plate candidates over a 20-frame window per vehicle track (`PLATE_DETECTION_FRAMES = 20`), selects the most frequent/highest-quality text via voting, and "locks" it to that vehicle. Once locked, the plate is displayed alongside the speed overlay.

**Retry Logic**: If no plate text is selected after a 20-frame window, up to 3 attempts are made (60 frames total). After 3 failures, the vehicle is marked with `plate_locked = None`.

### 3. Region of Interest (ROI) Filtering

Each camera has a configurable **polygon ROI** (region of interest) defined in pixel coordinates. A **ROI filter probe** runs OpenCV's point-in-polygon test on the vehicle centroid (bottom center of bounding box) — only vehicles inside the ROI are tracked and analyzed, reducing false positives and processing load.

### 4. Multi-Camera Streaming & Tiling

The pipeline accepts multiple RTSP sources (up to `MAX_STREAMS`, typically 4–8 per Jetson). All cameras are merged into a single video grid via `nvmultistreamtiler`, creating a composite output (e.g., 2×2 for 4 cameras). This composite is encoded once as H.264 and pushed to the central MediaMTX relay.

### 5. Overspeed Detection & Alerting

When a vehicle's smoothed speed exceeds the per-camera `speed_limit_kmh` threshold:

1. A JPEG snapshot of the vehicle is cropped and encoded to base64.
2. A **payload** is constructed: vehicle track ID, speed, detected license plate, timestamp, camera ID, and image.
3. The payload is **enqueued** to a non-blocking `ZenohPublisher` queue (configurable max size).
4. If the queue is full (network down), the **oldest enqueued event is dropped** — the pipeline never blocks.
5. A daemon thread consumes the queue and publishes via Zenoh to `traffic/events/{node_id}/{camera_id}` (msgpack format).
6. The `health_agent.py` **subscribes** to this topic, receives violations, and forwards them to the **Central Monitoring Server** over WebSocket.

---

## Key Innovation: P2P Load Balancing & Failover

Each Jetson runs an independent **PeerOrchestrator** instance that implements a **Pareto ε-constraint voting protocol** over Zenoh—eliminating the single point of failure of a master orchestrator.

### Peer Status & Monitoring

Every 2 seconds, the **health_agent** collects hardware metrics, CV-plane features, and computes two complementary load signals:

#### Reactive Baseline (legacy, always active)

```
Load Score = w_gpu × GPU% + w_cpu × CPU% + w_ram × RAM% + FPS_penalty
```

**Adaptive Weights** ω = `[w_gpu, w_cpu, w_ram]` (from `edge_node.yml load_score`):
- **bandwidth** preset (active cameras ≥ `stream_bandwidth_threshold=3`): ω = [0.2, 0.3, 0.5]
- **normal** preset (default): ω = [0.3, 0.3, 0.4]

The FPS penalty is applied when average FPS drops below `TARGET_FPS` — capped at `fps_penalty_max=25` points. The `omega_preset` name is included in the heartbeat.

> **Note**: The `thermal` omega preset has been removed. Thermal stress is now handled exclusively by the smooth `Θ_thermal` ramp inside `H_reactive` (see below), eliminating double-counting of the same signal.

#### Proactive Load Model (Vehicle-Driven, optional)

When `proactive.enabled: true` in `edge_node.yml`, the system also computes a **Unified Risk Index U** that bridges the Computer Vision plane to the Hardware plane:

```
L_proactive = (W_base + Σ_cam[α₁·N_track + α₂·N_track² + β·N_plate + γ·S]) / 100
H_reactive  = max(R_GPU, R_CPU, R_RAM) × Θ_thermal
U           = 1 − (1 − L̂_avg)(1 − Ĥ_avg)        [noisy-OR; both cycle-averaged]
```

**Parameters**:
- `W_base`: idle GPU% with zero video sources (measured offline via `tools/profile_collect.py --wbase`).
- `N_track`, `N_plate`, `S`: per-camera vehicle count, plate count, and stationary fraction — extracted live from `SpeedProbe` every frame.
- `α₁, α₂, β, γ`: regression coefficients fitted offline by `tools/fit_coefficients.py` on intersection data. `α₂=0` if the linear model wins on held-out RMSE.
- `Θ_thermal`: smooth linear ramp 1.0 → `max_mult=1.25` over `t_low=75`°C → `t_high=90`°C (matches Jetson's hardware throttle curve).
- **Cycle-aware smoothing**: both `L̂` and `Ĥ` are averaged over a `cycle_window_s=90`s sliding window (≈ one signal cycle) before fusion. This prevents transient red-phase peaks from triggering unnecessary migrations — only sustained, cycle-averaged overload triggers offload.

The heartbeat payload gains: `l_proactive`, `h_reactive`, `risk_index` (U, cycle-smoothed), `l_proactive_instant`, `h_reactive_instant`, `risk_index_instant`, `n_track_mean`, `n_plate_mean`, `stationary_fraction`, `theta_thermal`.

**Heartbeat Timeout**: If a peer has not published a status update for `heartbeat_timeout_s` (default 5 seconds), it is marked **OFFLINE**.

### Request for Offload (RFO) Voting Protocol

When a node detects it is overloaded (`load_score > overload_threshold` for longer than `overload_duration_s`; defaults 65 and 5s in `edge_node.yml`):

1. **Select Camera**: Prioritized by FPS (highest FPS camera is most effective to offload).
2. **Publish RFO**: Declares camera ID, its load score, and tiered ε-constraint requirements:
   - **ε₁ Capacity**: Responder must have < `eps_streams_max` (default 4) active cameras.
   - **ε₂ FPS Prediction**: Responder's predicted FPS after adding this stream (from the `fps_model` table) must be ≥ `eps_fps` (`eps_fps_strict=15` → `eps_fps_tier1=12` → `eps_fps_tier2=10` with tier-based relaxation).
   - **ε₃ Network RTT**: RTSP source round-trip latency must be ≤ `eps_network_ms` (`eps_network_ms_strict=30` → `eps_network_ms_tier1=60`).
   - **ε₄ Per-Camera Cooldown**: Camera must not have been migrated within the last `cooldown_s` (default 15) seconds.
   - **ε₅ Penalty Check**: Responder must not have an active penalty from a previous failed migration.

3. **Collect Bids**: All peers passing all ε-constraints respond with a **bid** including composite score `F(x)` (estimated load after adding stream).

4. **Elect Winner**: Requester collects bids for `vote_window_s` (default 3 seconds), selects the peer with the lowest F(x) score.

5. **Escalate on Failure**: If no peer bids, the requester escalates to a more relaxed ε-constraint tier and retries. If all tiers fail, log **CLUSTER_SATURATED** and continue at overload.

### Make-Before-Break Migration

Once the winner is elected:

1. A **decision** is published to `peers/vote/decision` with winner's node ID and camera config.
2. The **winner receives an ADD control command** on `peers/control/{winner_node_id}` with full camera config (URI, homography, ROI, FPS, speed limit, etc.).
3. The winner **immediately begins streaming** from the camera.
4. When the stream reaches **PLAYING state** (buffered), the winner publishes an ack (`peers/vote/ack/{camera_id}`).
5. The **requester waits for this ack** (`migration_timeout_s`, default 2 seconds; on timeout it rolls back and penalizes the winner). Upon receipt, it publishes a **REMOVE command** to stop its own stream.
6. A **per-camera cooldown** (`cooldown_s`, default 15 seconds) prevents the same camera from being offloaded again immediately.

This **Make-Before-Break** strategy ensures **zero frame loss** during migration.

### Leaderless Failover

When any peer detects another peer is **OFFLINE** (heartbeat timeout):

1. All living peers **independently compute the same assignment** using **consistent hash** of the offline peer's camera list. This deterministic approach ensures all peers elect the same **rescuer** for each orphaned camera—no coordination needed.

2. The elected peer **waits for random jitter** (0–`failover_jitter_max_s` seconds uniform, default 0–3s) to avoid a "thundering herd".

3. **Verify RTSP reachability** — if the camera was hosted on the dead node's subnet, it will be unreachable, and rescue is skipped.

4. **Rescued cameras are tracked** separately and automatically **returned** to their original owner if that owner comes back online and re-registers.

---

## System Architecture & Data Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│         Zenoh Peer Mode (UDP multicast local discovery)             │
│  No broker, no master — fully symmetric peer-to-peer network        │
└─────────────────────────────────────────────────────────────────────┘
     ▲               ▲               ▲               ▲
     │ peers/        │ peers/        │ peers/        │ traffic/
     │ status/       │ vote/*        │ control/      │ events/
     │ (heartbeat)   │ (RFO/bid)     │ (ADD/REMOVE)  │ (speed)
     ▼               ▼               ▼               ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Jetson Node A│  │ Jetson Node B│  │ Jetson Node C│
│              │  │              │  │              │
│ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │
│ │GStreamer │ │  │ │GStreamer │ │  │ │GStreamer │ │
│ │Pipeline  │ │  │ │Pipeline  │ │  │ │Pipeline  │ │
│ │(YOLO+    │ │  │ │(YOLO+    │ │  │ │(YOLO+    │ │
│ │LPR+      │ │  │ │LPR+      │ │  │ │LPR+      │ │
│ │Tracking) │ │  │ │Tracking) │ │  │ │Tracking) │ │
│ └────┬─────┘ │  │ └────┬─────┘ │  │ └────┬─────┘ │
│      │       │  │      │       │  │      │       │
│ ┌────▼──────────────────────────────────────────┐
│ │SpeedProbe + OffloadPublisher (level 2/3 crops)
│ └────┬─────┬─────┬─────────────────────────────┘
│      │     │     │
│ ┌────▼─────▼─────▼─────┐
│ │ ZenohPublisher       │
│ │ traffic/events/...   │
│ └────┬─────────────────┘
│      │
│ ┌────▼─────┐
│ │PeerOrch  │
│ │(Voting,  │
│ │Failover) │
│ └──────────┘
│
│ ┌──────────┐
│ │HealthAg │
│ │(metrics,│
│ │WebSocket)│
│ └────┬─────┘
└──────┼───────┘
       │
       │ ① RTSP push
       │ (composite video grid)
       ▼
  ┌───────────────────────────┐
  │  MediaMTX RTSP ↔ WebRTC   │
  │  :8554 (RTSP), :8889 (WHEP)
  └─────────────┬─────────────┘
                │
                │ ③ WebRTC (WHEP)
                ▼
     ┌──────────────────────┐
     │Central Monitoring    │
     │Server (aiohttp)      │
     │- EdgeRegistry        │
     │- ViolationStore      │
     │- WebSocket broadcast │
     └──────────┬───────────┘
                │ ④ WebSocket push
                ▼
     ┌──────────────────────┐
     │Browser Dashboard     │
     │- Live video grid     │
     │- Cluster status      │
     │- Violation feed      │
     └──────────────────────┘
```

### Data Flow Descriptions

#### ① Edge Node → MediaMTX (RTSP Video Push)

Each Jetson runs the DeepStream pipeline (`main.py --mode rtsp_push`), tiling all N camera streams into a single composite grid (2×2 for 4 cameras, 4×2 for 8 cameras). This composite is:
1. Encoded in real time as H.264 using NVIDIA's hardware encoder (`nvv4l2h264enc`).
2. Pushed continuously to MediaMTX via `rtspclientsink` (RTSP protocol, TCP).
3. MediaMTX broadcasts it to multiple formats (RTSP, WebRTC, HLS).

**Purpose**: Centralized aggregation; browsers don't need direct access to Jetson devices (which may be restricted or behind NAT).

#### ② Edge Node → Central Server (WebSocket Health & Violations)

The `health_agent.py` daemon runs as a separate process on each Edge:
1. Opens persistent WebSocket to `ws://SERVER:PORT/ws/edge?node_id=<NODE_ID>&advertise_ip=<LAN_IP>`.
   - Query parameters are URL-encoded to handle special characters.
   - Connection implicitly registers the node (no separate registration endpoint).
2. Every `HEALTH_INTERVAL` seconds (e.g., 2 seconds), publishes health containing:
   - GPU %, CPU %, RAM %, temperature, power, load score, `omega_preset`.
   - `source` (`jtop` or `jtop_unavailable`) — the dashboard shows GPU%/Temp as "N/A" when jtop is down rather than a misleading 0.0.
   - FPS per camera, average FPS.
   - Active camera list.
3. Subscribes to `traffic/events/{NODE_ID}/**` on Zenoh (published by SpeedProbe).
   - When an overspeed violation is published, forwards it to Server over WebSocket.
   - This avoids the pipeline needing its own WebSocket connection.
4. Implements exponential backoff reconnection (2s, 5s, 10s, 30s, repeat).

**Note**: The pipeline (`main.py`) does NOT connect to Server directly. It publishes events via Zenoh; health_agent bridges them.

#### ③ MediaMTX → Browser (WebRTC Playback)

The browser dashboard uses **WHEP (WebRTC-HTTP Egress Protocol)** for low-latency playback:
1. For each active camera, the dashboard POSTs an SDP offer to `http://SERVER:8889/{node_id}/whep`.
2. MediaMTX responds with SDP answer; WebRTC connection is established.
3. ICE (Interactive Connectivity Establishment) negotiates the best path through NAT/firewalls.
4. WebRTC streams inbound video with minimal latency (~100–500ms vs. RTSP's 1–5s).

Server does NOT relay video itself—it only proxies health and violation data.

#### ④ Central Server → Browser (WebSocket Push)

The browser connects to `ws://SERVER:PORT/ws/server` to receive live updates:
- **Health updates**: Whenever an Edge sends new health, broadcast to all browsers.
- **Violation events**: Whenever a violation is received, append to ViolationStore and push to browsers.
- **Edge offline**: When an Edge misses heartbeats for 15 seconds, watchdog marks offline and broadcasts event.

Dashboard updates UI in real-time using DOM diffing to avoid flicker.

#### ⑤ Edge ↔ Edge (Zenoh P2P Control)

All inter-node communication (peer discovery, voting, control commands, failover) occurs via Zenoh key expressions:
- `peers/status/{node_id}` — health status broadcasts
- `peers/vote/*` — RFO voting protocol
- `peers/control/{node_id}` — ADD/REMOVE/STATUS commands
- `traffic/events/{node_id}/{camera_id}` — overspeed events

---

## Zenoh Key Expressions & Protocol Reference

Zenoh is a pub-sub framework with hierarchical key expressions. All peer communication is **brokerless** in peer mode, using UDP multicast scouting for local discovery.

| Key Expression | Publisher | Subscribers | Payload | Semantics |
|---|---|---|---|---|
| `peers/status/{node_id}` | health_agent | peer_orchestrators | msgpack: GPU%, CPU%, RAM%, temp, load_score, FPS per camera, active cameras | **Heartbeat & metrics**. Published every 2 seconds. If not heard for 15s, peer marked offline. |
| `peers/vote/request` | peer_orch (RFO sender) | all peer_orchs | msgpack: requester, camera_id, load_score, avg_fps, eps_fps, eps_network_ms, tier | **Request for Offload (RFO)**. Sent when node is overloaded. Initiates voting window. |
| `peers/vote/proposal` | peer_orch (responder) | requester | msgpack: bidder, camera_id, score (F(x)), fps_predicted, rtt_ms | **Bid from responder**. Sent if all ε-constraints satisfied. Collected for 3 seconds. |
| `peers/vote/decision` | peer_orch (requester) | winner + requester | msgpack: winner, camera_id, from_node, cam_config, ts | **Election result**. Winner receives ADD command; requester prepares REMOVE. |
| `peers/vote/ack/{camera_id}` | zenoh_subscriber (winner) | requester | msgpack: node_id, camera_id, event: PLAYING | **Acknowledgment**. Stream is PLAYING. Requester waits before REMOVE. |
| `peers/control/{node_id}` | peer_orch | zenoh_subscriber | msgpack: cmd (ADD/REMOVE/STATUS), camera_id, source_id, uri, homography, roi_polygon, fps, speed_limit | **Control command**. ADD (full config) or REMOVE (camera_id only) or STATUS (query). |
| `traffic/events/{node_id}/{camera_id}` | zenoh_publisher (probe) | health_agent | msgpack: type (overspeed), node_id, camera_id, ts, track_id, speed_kmh, license_plate, image_b64, dedup_key | **Overspeed event**. Published by SpeedProbe when vehicle exceeds speed limit. Includes snapshot. |
| `offload/plates/{src}/{dst}` | offload_publisher | offload_receiver (dst) | msgpack: type (plate), src, dst, camera_id, stid, frame_no, jpeg, confidence | **Level 3 offload**. Plate crops sent to a peer for remote LPR. Only used when `offload_level ≥ 3`. |
| `offload/vehicles/{src}/{dst}` | offload_publisher | offload_receiver (dst) | msgpack: type (vehicle), src, dst, camera_id, stid, frame_no, jpeg, bbox_world_y | **Level 2 offload**. Vehicle crops sent to a peer for remote LPD+LPR. Only used when `offload_level ≥ 2`. |
| `offload/results/{node_id}/{sender}` | offload_receiver | offload_publisher (sender) | msgpack: src, dst, camera_id, stid, frame_no, plate_text, confidence | **Offload result**. Decoded plate text returned to the sender for OSD overlay. |

---

## Components & Modules

### Three-Tier Deployment

| Component | Role | Technology |
|---|---|---|
| **Camera Simulator** (`Camera/`) | Docker-based RTSP camera farm. Loops video files as real-time RTSP streams to simulate live traffic cameras. | Docker Compose + ffmpeg |
| **Edge Node** (`Edge/`) | AI processing unit (Jetson Orin/NX/Nano). Runs DeepStream pipeline, health monitoring, P2P orchestration. Multiple nodes form decentralized cluster (no master). | NVIDIA DeepStream, GStreamer, Python, Zenoh, aiohttp |
| **Central Server** (`Server/`) | Aggregation & visualization. Receives violations and health from all Edge nodes, stores persistently, serves live dashboard. Separate from video relay. | Python aiohttp, JSONL storage, Zenoh optional |

### Edge Node Architecture — Detailed Modules

#### Core Pipeline & Detection

- **`main.py`**: CLI entry point. Parses command-line arguments (`--mode display/file/rtsp_push`, `--rtsp-push-url`, `--width`, `--height`) and delegates to `run_python_mode()`.

- **`speedflow_python/settings.py`**: **Single source of truth** for runtime configuration. Loads from `Edge/.env` using `python-dotenv`. Strict validation: missing required variables raise errors at import time (no silent defaults). Ensures configuration mismatches are caught immediately.

- **`speedflow_python/core_pipeline.py`**: **DeepStream pipeline builder**. Constructs a complete GStreamer pipeline:
  - **Demultiplexing**: N RTSP sources (`uridecodebin`) feeding into single `nvstreammux` (batch muxer).
  - **Primary Inference (PGIE)**: YOLO detector (vehicle detection, classification).
  - **Tracking**: NvDCF tracker — assigns consistent track IDs via motion prediction.
  - **Secondary Inferences**: License Plate Detector (LPD, `SGIE_CONFIG`) followed by License Plate Reader (LPR, `LPR_CONFIG`) classifier. Both use custom DeepStream parsers under `configs/nvdsinfer_custom_impl_Yolo/` and `configs/nvinfer_custom_lpr_parser/`.
  - **Analytics**: `nvdsanalytics` probe attachment point.
  - **Tiling**: `nvmultistreamtiler` arranges cameras into grid (2×2 for 4 cameras, etc.).
  - **Rendering**: `nvdsosd` overlays speed, plate text, bounding boxes.
  - **Output Sinks**: Three modes:
    - `display`: X11 EGL sink (HDMI/monitor).
    - `file`: `nvstreamdemux` → per-camera H.264 encoders → MP4 files.
    - `rtsp_push`: H.264 encoder → `rtspclientsink` pushes composite to MediaMTX.

- **`speedflow_python/probes.py`**: **GStreamer pad probes** — functions attached to pipeline pads:
  - **`ROIFilterProbe`**: Filters objects outside per-camera ROI polygon using C extension for performance. Skips filter entirely if ROI is not configured.
  - **`SpeedProbe`**: Main analytics probe (called once per frame):
    - Iterates over objects; separates vehicles from license plates.
    - **Perspective Transform**: Converts pixel-space vehicle centroids to world coordinates using homography matrix in one batched C call.
    - **Position History**: Maintains deque of world positions per (source_id, track_id).
    - **Speed Calculation**: When history is long enough, computes velocity and converts to km/h.
    - **Validation**: Checks minimum displacement, area stability, confidence, track age.
    - **Median Smoothing**: 3–5 frame window to smooth noisy speeds.
    - **License Plate Tracking**: Accumulates detections over a 20-frame window; selects most frequent/highest-quality text.
    - **Overspeed Publishing**: If speed ≥ limit, encodes JPEG snapshot and calls `publisher.put(payload)` (non-blocking).
  - **Proactive Feature Counting**: On every frame, counts per-camera `n_track` (active vehicle tracks), `n_plate` (plate detections), and `n_stationary` (vehicles with smoothed speed < 3 km/h, i.e. stopped at red). Averages are flushed every 2 seconds alongside FPS into the shared stats file.
  - **FPS Statistics**: Separate thread writes FPS counters + proactive feature averages every 2 seconds to `FPS_STATS_FILE` (`/dev/shm/speedflow_fps.json`). Both FPS and features are consumed by `health_agent.py`. Properly stopped on pipeline exit.
  - **Cleanup**: Every 30 seconds, removes stale tracks that have not produced speed readings or left the scene.

- **`speedflow_python/camera_config.py`**: **Camera configuration manager**. Reads `cameras.yml`, parses per-camera settings (RTSP URI, homography matrix, ROI polygon, speed limit, FPS). Implements **file-system watcher** (via `watchdog` library) with 100ms debounce to detect `cameras.yml` changes and hot-reload without restarting. Uses `GLib.idle_add()` to queue dynamic stream add/remove operations on GLib Main Loop thread (thread-safe).

#### P2P Orchestration & Load Balancing

- **`speedflow_python/peer_orchestrator.py`**: **Decentralized load balancer**. Implements full P2P orchestration protocol:
  - Maintains in-memory registry of peer state (load score, FPS, active cameras, timestamps).
  - Implements decision loop: monitors own load, triggers RFO when overloaded, collects bids, elects winners, sends migration commands.
  - Detects offline peers via heartbeat timeout; triggers leaderless failover using consistent hashing.
  - Tracks rescued cameras; automatically returns them to recovered owners.
  - Manages per-camera cooldowns to prevent thrashing.
  - Implements three-level offload partitioning: Level 3 (plate crops), Level 2 (vehicle crops), Level 1 (full stream migration).
  - All P2P parameters loaded from `Edge/configs/edge_node.yml`.
  - **Thread safety**: Uses `_self_lock` for private state snapshots, `_lock` for peer state, `_offload_lock` for camera offload table.

- **`speedflow_python/zenoh_publisher.py`**: **Non-blocking event publisher**. Maintains bounded queue and daemon thread:
  - When SpeedProbe detects overspeed, calls `publisher.put(payload)` (non-blocking, < 0.1ms).
  - If queue is full (network down), oldest event dropped — pipeline never stalls.
  - Daemon thread consumes queue; publishes via Zenoh to `traffic/events/{node_id}/{camera_id}` (msgpack format).

- **`speedflow_python/zenoh_subscriber.py`**: **Control command receiver**. Subscribes to `peers/control/{node_id}`:
  - **ADD**: Adds new camera; delegates config build + delta enqueue to `CameraManager.handle_add_command()`.
  - **REMOVE**: Marks camera disabled; enqueues delta for dynamic stream removal.
  - **STATUS**: Returns current active camera list.
  - After an ADD, a background thread polls the live `source_id` lookup until the stream reaches PLAYING (15-second timeout), then publishes ack to `peers/vote/ack/{camera_id}` — it never acks a failed ADD.

- **`speedflow_python/zenoh_session.py`**: **Zenoh session factory**. Creates peer-mode Zenoh session (no broker required) with UDP multicast scouting for local-network peer discovery.

#### Multi-Level Offload (Fine-Grained Load Shedding)

- **`speedflow_python/offload_publisher.py`**: **Non-blocking crop sender**. When `PeerOrchestrator` sets a camera's offload level to 2 or 3, `SpeedProbe` sends crops to the target peer instead of (or in addition to) processing locally:
  - **Level 3**: plate crops → `offload/plates/{src}/{dst}` (~1–3 KB).
  - **Level 2**: vehicle crops → `offload/vehicles/{src}/{dst}` (~15–40 KB).
  - Shares one daemon thread + bounded queue (drop-oldest), same non-blocking contract as `ZenohPublisher`.

- **`speedflow_python/offload_receiver.py`**: **Standalone TensorRT inference for offloaded crops**. Subscribes to `offload/plates/*/{node_id}` and `offload/vehicles/*/{node_id}`:
  - **Level 3**: runs LPR directly on the plate crop.
  - **Level 2**: runs LPD then LPR on the detected plate sub-crop.
  - Loads `.engine` files lazily in a worker thread (no DeepStream pipeline needed). Publishes results back to the sender on `offload/results/{node_id}/{sender}`; the sender's `SpeedProbe.inject_offload_result()` overlays the text on the next frame.

#### Native Extension (Hot-Path Acceleration)

- **`speedflow_cpp/` + `speedflow_python/speedflow_c.py`**: **C/C++ extension** loaded via ctypes. Replaces the per-frame hot path (speed computation, batched perspective transform, point-in-polygon, plate quality, center distance, plate enhancement) with native code. Every binding has a pure-Python/OpenCV fallback in `speedflow_c.py`, so the pipeline still runs if `speedflow_cpp.so` is not built. Override the `.so` path with `SPEEDFLOW_CPP_SO`.

#### Monitoring & Communication

- **`health_agent.py`**: **Standalone daemon process** that runs independently from pipeline:
  - Collects hardware metrics every `HEALTH_INTERVAL` seconds: GPU %, CPU %, RAM %, GPU temp (via `jtop` on Jetson with 10-second timeout guard). If jtop is unavailable, metrics are reported as zero and a warning is logged — no psutil fallback (Jetson-only deployment).
  - Reads FPS stats from file written by `SpeedProbe._fps_writer_loop()`.
  - Computes unified **load score** using weighted formula with adaptive ω presets.
  - Publishes health payload to `peers/status/{node_id}` via Zenoh.
  - Opens persistent **WebSocket** to Central Server (`ws://SERVER:PORT/ws/edge?node_id=<NODE_ID>`), implicitly registering node.
  - Subscribes to `traffic/events/{node_id}/**` on Zenoh; forwards violations to Server over WebSocket.
  - Implements exponential backoff reconnection on WebSocket drop.

- **`speedflow_python/monitor_client.py`**: **WebSocket client** used by health_agent:
  - Persistent outbound connection with exponential backoff (`_RECONNECT_DELAYS = [2, 5, 10, 30]` seconds).
  - Thread-safe queue for outbound messages.
  - Daemon thread consumes queue; sends JSON over WebSocket.
  - URL parameter encoding for special characters in node_id/advertise_ip.

- **`speedflow_python/run_python.py`**: **Pipeline orchestration glue**. Initializes and manages lifecycle of:
  - GStreamer pipeline (via core_pipeline.py).
  - `PeerOrchestrator` (owns the single shared Zenoh session for all P2P traffic).
  - `ZenohCommandSubscriber` (control commands) — shares the orchestrator's session.
  - `ZenohPublisher` (overspeed events) — wired into `SpeedProbe.set_publisher()` so violations actually publish.
  - `OffloadPublisher` / `OffloadReceiver` — started only when `offload_level > 0` in `edge_node.yml`.
  - Probe instances (ROI filter, plate preprocessor, Speed/LPR).
  - A health-push thread (metrics → Dashboard via MonitorClient + Zenoh).
  - Loop and signal handlers for graceful shutdown.
  - **PID file lock** (`run_python.pid`, rtsp_push mode) prevents two instances (would cause MediaMTX publisher conflicts).
  - Returns probe from all execution modes; cleanly stops the FPS writer and event publisher on exit.
  - Does **not** open its own MonitorClient WebSocket unless `PIPELINE_OWN_WS=1` (avoids competing with `health_agent.py` for the same `node_id`).

### Server (Central Monitoring & Dashboard)

#### REST API & Data Aggregation

- **`app.py`**: Async Python web server (aiohttp) with lifecycle management:
  - **Startup**: Opens HTTP session; starts edge registry watchdog.
  - **Shutdown**: Cancels pending tasks; closes all WebSocket connections; flushes data.
  - Uses **`SO_REUSEADDR`** on TCP socket to allow rapid restarts without "Address already in use".
  - Routes:
    - **`GET /`**: Dashboard HTML (single-page app).
    - **`GET /health`**: Health check.
    - **`GET /api/edges`**: All registered edges with live health state.
    - **`GET /api/clusters`**: Edges grouped by cluster (IP subnet or cluster_id from health).
    - **`GET /api/violations`**: Query violations with filters (node_id, date, limit, offset). Uses early-exit optimization to bound memory.
    - **`GET /api/streams`**: Proxy to MediaMTX API (list active RTSP sessions).
    - **`GET /api/snapshots/{node_id}/{filename}`**: Serve snapshot images (path traversal-safe).
    - **`GET /ws/edge`**: WebSocket for Edge nodes. Implicit registration + receives health/violations + broadcasts to browsers.
    - **`GET /ws/server`**: WebSocket for browsers. Receives live push updates (health + violations).

- **`edge_registry.py`**: In-memory registry of all connected Edge nodes:
  - Stores `EdgeInfo` (node_id, IP, online status, last heartbeat, health metrics, cluster_id).
  - **Watchdog task** runs every 5 seconds; checks heartbeat timeout (15 seconds). Nodes not heard from in 15s marked **OFFLINE**; callback fires (broadcasts "edge_offline" to browsers).
  - Methods to query edges: all, online only, or grouped by cluster.

- **`violation_store.py`**: Persistent violation storage:
  - Organizes files by date and node_id: `violations/{date}/{node_id}/violations.jsonl` and `violations/{date}/{node_id}/{camera_id}_{timestamp_ms}.jpg`.
  - **Async write**: All violations appended via `aiofiles` (avoids blocking event loop).
  - **Snapshot handling**: JPEG snapshots (base64-encoded in payload) decoded and saved separately.
  - **Query method**: Synchronous method reads JSONL with early-exit when sufficient records collected; bounded memory for large violation stores.
  - Single `asyncio.Lock` guards all async writes to prevent race conditions.

#### Dashboard (Frontend)

- **`static/index.html`**: Single-page application (SPA) with three main panels:
  - **Live Video Panel**: Auto-discovers WebRTC streams by querying `/api/streams` (proxies to MediaMTX) and `/api/edges` (lists active cameras from edge health). Uses WHEP to fetch low-latency video.
  - **Cluster Status Panel**: Displays live edge cards (node_id, IP, online status, GPU %, CPU %, RAM %, temp, load score, FPS per camera). Updates via `/ws/server` WebSocket with DOM diffing to prevent flicker.
  - **Violation Feed Panel**: Shows violations sorted newest-first. Each card displays: camera ID, timestamp, detected license plate, speed, snapshot thumbnail. Supports filtering by node_id and date range.

- **`mediamtx.yml` / `docker-compose.media.yml`**: MediaMTX (Bluenviron's high-performance RTSP relay) in Docker:
  - Port 8554 (RTSP input/output).
  - Port 8889 (WebRTC WHEP output).
  - Port 9997 (REST API for stream introspection).
  - **ICE configuration**: `webrtcICEHostNAT1To1IPs` for NAT traversal (clients behind NAT can use server's public IP).
  - **HLS output**: Optional HTTP Live Streaming for compatibility.

---

## Speed Measurement Methodology

The system's core innovation is **perspective transform-based speed measurement**, converting pixel-space motion to real-world velocity:

1. **Calibration (Offline)**: For each camera, define:
   - A **source polygon** (4 points) in the video frame representing a known real-world distance (e.g., 3.75m lane width).
   - A **target rectangle** in world plane (e.g., 0–3.75m in X, 0–Ly in Y).
   - System computes homography matrix `H` that maps source → target coordinates.

2. **Real-Time Tracking**:
   - YOLO provides bounding box: (left, top, width, height).
   - Extract center X and bottom Y: `(cx, cy_bottom) = (left + width/2, top + height)`.
   - Homography transforms: `(cx, cy_bottom) → (world_x, world_y)`.
   - Typically, only `world_y` (position along road direction) used for speed calculation.

3. **Velocity Computation**:
   - Maintain history of world Y positions per track.
   - Speed = `Δworld_Y / Δtime × 3.6` (to convert m/s to km/h).
   - Raw speeds are noisy; **median filter** (3–5 samples) smooths them.

4. **Validity Checks**:
   - Minimum world displacement (e.g., 0.5m across frames) rejects stationary objects.
   - Bounding box area stability (max 3× change) rejects detection artifacts.
   - Minimum track age (0.5 seconds) rejects newly detected objects.
   - Detection confidence threshold (≥0.5) rejects low-quality detections.

5. **Overspeed Detection**:
   - If smoothed speed ≥ camera's `speed_limit_kmh`, violation is published.
   - **Cooldown timer** per track (2.5 seconds) prevents duplicate alerts.

---

## Prerequisites

- **Camera node**: Docker & Docker Compose
- **Edge nodes**: NVIDIA Jetson (Orin/NX/Nano) with JetPack 6.x and DeepStream SDK 7.x
- **Server**: Any machine with Python 3.8+ (VPS recommended for public access)
- **Python**: 3.8+ on all nodes

---

## Quick Start

### 1. VPS — Start MediaMTX + Central Server

```bash
cd Server

# Start MediaMTX (RTSP → WebRTC relay)
docker compose -f docker-compose.media.yml up -d

# Start Central Monitoring Server
pip install -r requirements.txt
python3 app.py --port 9090

# Dashboard: http://<SERVER_IP>:9090
```

The Server uses `SO_REUSEADDR` to survive rapid restarts without "address already in use". A systemd service file is provided at `/etc/systemd/system/monitor-server.service` with `Restart=always` and `RestartSec=10` for production.

### 2. Camera Node — RTSP Simulator

```bash
cd Camera

# Place .mp4 files in ./videos/
# Edit Camera/.env to set video file paths and RTSP URLs
chmod +x generate-compose.sh start.sh
./generate-compose.sh 4            # generate docker-compose.yml for 4 cameras
docker compose up -d

# Streams available at:
#   rtsp://<CAMERA_IP>:8554/cam_01
#   rtsp://<CAMERA_IP>:8554/cam_02
#   ...
```

### 3. Edge Node — Configure `.env`

`Edge/.env` is the single source of truth and is strictly validated at import time
(missing required keys raise an error — no silent defaults). Edit it to match your
deployment:

```ini
NODE_ID=jetson_A
MAX_STREAMS=8

# RTSP push destination (MediaMTX on VPS)
RTSP_PUSH_URL=rtsp://SERVER_IP:8554/jetson_A
RTSP_PUSH_BITRATE=2500000

# Central Monitor (set to your Server IP)
MONITOR_URL=http://SERVER_IP:9090

# Your LAN IP (for Server to display the correct address)
ADVERTISE_IP=192.168.1.200

# Pipeline / detection
TARGET_FPS=25.0
VIDEO_FPS=25.0
MUX_WIDTH=1280
MUX_HEIGHT=720
FPS_STATS_FILE=/dev/shm/speedflow_fps.json
```

P2P tuning (overload thresholds, ε-constraints, offload levels, ω weights) lives
separately in `Edge/configs/edge_node.yml`.

### 4. Edge Node — Launch (Two Processes)

The Edge requires **two separate processes** — one for data connection and one for video:

```bash
cd Edge
./setup_system.sh             # system deps (first time only)
pip3 install -r requirements.txt

# 1. Start health agent (WebSocket + Zenoh + metrics)
python3 health_agent.py &

# 2. Start DeepStream pipeline (RTSP push to MediaMTX)
python3 main.py --mode rtsp_push
```

**Note**: `main.py` does NOT connect to Server directly. It publishes overspeed events via Zenoh; `health_agent.py` subscribes and forwards them to avoid duplicate WebSocket connections.

### 5. Dashboard — Browser

Open [http://<SERVER_IP>:9090](http://<SERVER_IP>:9090). The dashboard shows:

- **Live Video panel**: Auto-discovered WebRTC streams from each Edge (WHEP player). Streams appear from both MediaMTX API and edge health `active_cameras`.
- **Cluster Status panel**: Live edge cards with GPU%, CPU%, RAM%, temp, load score, FPS per camera (DOM-diff updated to avoid flicker).
- **Violation Feed panel**: Live violations sorted newest-first with plate, speed, snapshot thumbnails, and filters by node/date.

---

## REST API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Dashboard HTML |
| `/health` | GET | Server health check |
| `/api/edges` | GET | All registered edges with live health |
| `/api/clusters` | GET | Edges grouped by cluster (`cluster_id` from health, else IP /24 subnet) |
| `/api/violations` | GET | Query violations `?node_id=&date=&limit=&page=` |
| `/api/streams` | GET | Proxy to MediaMTX — list active RTSP streams |
| `/api/snapshots/{node}/{file}` | GET | Serve snapshot image (path traversal safe) |
| `/ws/edge` | WebSocket | Edge inbound data channel (implicit registration + health + violations) |
| `/ws/server` | WebSocket | Browser push channel (health updates + violations) |

---

## Project Structure

```
IoT_Graduate/
├── Camera/                         # RTSP camera simulator
│   ├── .env
│   ├── docker-compose.yml
│   ├── Dockerfile
│   ├── mediamtx.yml                # MediaMTX config for the simulator
│   ├── generate-compose.sh         # Generate docker-compose.yml for N cameras
│   ├── start.sh                    # Convenience launcher
│   └── videos/
├── Edge/                           # AI processing node
│   ├── .env                        # Single source of truth (strictly validated)
│   ├── main.py
│   ├── health_agent.py             # Hardware metrics → Zenoh + MonitorClient
│   ├── speed_gui.py                # PyQt5 calibration GUI
│   ├── setup_system.sh             # System dependency installer (first-time)
│   ├── requirements.txt
│   ├── configs/
│   │   ├── cameras.yml                 # Multi-camera sources + homography + ROI
│   │   ├── config_infer_primary_yolo11.txt   # DeepStream GIE — YOLO detector
│   │   ├── config_infer_secondary_lpd.txt    # DeepStream GIE — plate detector
│   │   ├── config_infer_secondary_lpr.txt    # DeepStream GIE — plate reader
│   │   ├── config_nvdsanalytics.txt          # DeepStream analytics
│   │   ├── config_tracker_NvDCF_perf.yml     # NvDCF tracker
│   │   ├── config_tracker_lpd.yml            # Tracker for LPD
│   │   ├── edge_node.yml               # P2P tuning + load-score weights
│   │   ├── labels_lpd.txt              # Plate detector class labels
│   │   ├── labels_lpr.txt              # Plate reader class labels
│   │   ├── labels_YOLO.txt             # YOLO detector class labels
│   │   ├── nvdsinfer_custom_impl_Yolo/ # Custom DeepStream YOLO parser (C/C++)
│   │   └── nvinfer_custom_lpr_parser/  # Custom DeepStream LPR output parser (C/C++)
│   ├── speedflow_cpp/                  # Native C/C++ hot-path extension
│   │   ├── speedflow.cpp               # Speed/perspective/polygon/quality math
│   │   ├── speedflow.h
│   │   └── plate_enhance.cpp           # CLAHE + sharpen plate enhancement
│   └── speedflow_python/
│       ├── __init__.py
│       ├── settings.py               # Config loader (strict validation)
│       ├── common.py                 # GStreamer helpers
│       ├── core_pipeline.py          # DeepStream pipeline builder
│       ├── run_python.py             # Pipeline runner + orchestration
│       ├── peer_orchestrator.py      # P2P load balancing
│       ├── peer_discovery.py         # No-op shim (Zenoh scouting handles discovery)
│       ├── probes.py                 # GStreamer pad probes (SpeedProbe, ROIFilterProbe)
│       ├── plate_preprocessor.py     # Plate image preparation
│       ├── camera_config.py          # Camera config manager + file watcher
│       ├── analytics.py              # Future analytics (stub)
│       ├── draw.py                   # OSD helpers
│       ├── io_utils.py               # Utilities
│       ├── speedflow_c.py            # ctypes bindings to speedflow_cpp.so (+ Python fallbacks)
│       ├── zenoh_publisher.py        # Non-blocking overspeed-event publisher
│       ├── zenoh_subscriber.py       # Control command receiver (ADD/REMOVE/STATUS)
│       ├── zenoh_session.py          # Zenoh session factory
│       ├── offload_publisher.py      # Level 2/3 crop offload sender
│       ├── offload_receiver.py       # Level 2/3 crop receiver (standalone TensorRT LPR/LPD)
│       └── monitor_client.py         # WebSocket client → Server
├── Server/                         # Central Monitoring Server + MediaMTX
│   ├── .env                        # SERVER_HOST, SERVER_PORT, DATA_DIR, MEDIAMTX_API
│   ├── requirements.txt
│   ├── app.py                      # aiohttp server + REST API + WS push (SO_REUSEADDR)
│   ├── edge_registry.py            # Edge state tracker + heartbeat watchdog
│   ├── violation_store.py          # JSONL + image file persist
│   ├── mediamtx.yml                # MediaMTX config (ICE NAT fix, HLS)
│   ├── docker-compose.media.yml    # Docker for MediaMTX
│   ├── static/
│   │   └── index.html              # Dashboard SPA (WHEP video grid, clusters, violations)
│   └── violations/                 # Runtime data (gitignored)
└── README.md
```

---

## Key Technologies

| Technology | Purpose |
|---|---|
| **GStreamer** | Real-time video pipeline (decoding, mux, sync) |
| **NVIDIA DeepStream** | PGIE (YOLO) + SGIE (plate) inference, NvDCF tracker |
| **TensorRT** | Model optimization & INT8 quantization |
| **OpenCV** | Homography transformation (bird's-eye speed estimation) |
| **Zenoh** | Pub/sub distributed messaging (peer discovery, state sync) |
| **aiohttp** | Async web framework for central server |
| **Qt5** | GUI dashboard (optional local monitoring) |
| **jtop** | Jetson telemetry (GPU, CPU, RAM, temp, power) |
| **msgpack** | Efficient binary serialization for Zenoh payloads |

---

## Performance Characteristics

| Metric | Target |
|--------|--------|
| **Probe Latency** | < 0.1 ms (non-blocking queue) |
| **Health Heartbeat** | Every `HEALTH_INTERVAL` seconds (default 2.0) |
| **Peer Decision Loop** | Every 1 second |
| **Stale-Track Cleanup** | Every 30 seconds |
| **FPS per Camera** | `TARGET_FPS` (default 25 fps, configurable) |
| **Speed Accuracy** | ±5% (with proper calibration) |
| **Memory Footprint** | ~800MB–1.2GB per edge (GStreamer + CUDA) |

