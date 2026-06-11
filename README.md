# IoT_Graduate — Decentralized Real-Time Traffic Monitoring with P2P Load Balancing

A production-grade distributed real-time traffic monitoring system that runs **AI inference (NVIDIA DeepStream with YOLOv8 and custom LPR)** on multiple autonomous Jetson Edge nodes to detect vehicles, measure speed using perspective transform homography, and read license plates in real time.

**Architecture**: Jetson Edge nodes are **fully decentralized** and communicate via **Eclipse Zenoh** (peer mode with UDP multicast scouting) for P2P load balancing and failover. A **Central Monitoring Server** aggregates health metrics and violations for visualization and long-term storage. Each Edge pushes a composite RTSP stream to a central **MediaMTX** relay, which converts it to **WebRTC (WHEP)** for low-latency browser playback.

---

## System Overview: How It Works

### 1. Vehicle Detection & Speed Measurement

Each Jetson Edge node runs a **real-time DeepStream GStreamer pipeline** that processes multiple RTSP camera sources simultaneously:

- **Vehicle Detection**: YOLO detector (primary inference engine) identifies vehicles in each frame and assigns a unique track ID via NvDCF tracker.
- **Speed Measurement**: Using **homography matrix perspective transform** (calibrated per camera), the system converts pixel-space vehicle positions to real-world coordinates (meters). Velocity is computed as `Δworld_Y / Δtime × 3.6` to convert m/s to km/h.
- **Validation**: Speed measurements are validated against multiple filters: minimum world displacement (0.5m), bounding box area stability (max 3× change), detection confidence (≥0.5), and minimum track age (0.5s).
- **Smoothing**: Raw speed samples are smoothed using a median filter (3–5 frame window) to reject noise and outliers.

### 2. License Plate Detection & Recognition (LPR)

**License Plate Detection (LPD)**: A secondary classifier (SGIE) marks potential license plate regions in the vehicle bounding box.

**License Plate Recognition (LPR)**: A tertiary OCR classifier reads the text from detected plates. The system employs a **frame accumulation strategy**: it collects plate candidates over 5 frames per vehicle track, selects the most frequent/highest-quality text via voting, and "locks" it to that vehicle. Once locked, the plate is displayed alongside the speed overlay.

**Retry Logic**: If no plate is detected after 5 frames, up to 3 retry attempts are made (15 frames total). After 3 failures, the vehicle is marked with `plate_locked = None`.

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

Every 2 seconds, the **health_agent** collects hardware metrics and computes a **unified load score**:

```
Load Score = w_gpu × GPU% + w_cpu × CPU% + w_ram × RAM% + FPS_penalty
```

**Adaptive Weights** (from `edge_node.yml`):
- **thermal** preset (GPU temp ≥ 75°C): ω = [0.3, 0.2, 0.5] (prioritize cooling)
- **bandwidth** preset (≥3 cameras): ω = [0.2, 0.5, 0.3] (balance CPU)
- **normal** preset (default): ω = [0.5, 0.3, 0.2] (GPU-heavy)

The FPS penalty is applied when actual average FPS drops below target — each frame/second lost incurs up to 30 points. This metric is published to `peers/status/{node_id}` over Zenoh. All peers listen and update their in-memory view of peer state.

**Heartbeat Timeout**: If a peer has not published a status update for `heartbeat_timeout_s` (typically 15 seconds), it is marked **OFFLINE**.

### Request for Offload (RFO) Voting Protocol

When a node detects it is overloaded (`load_score > 75%` for >10 seconds continuously):

1. **Select Camera**: Prioritized by FPS (highest FPS camera is most effective to offload).
2. **Publish RFO**: Declares camera ID, its load score, and tiered ε-constraint requirements:
   - **ε₁ Capacity**: Responder must have < `eps_streams_max` (e.g., 4) active cameras.
   - **ε₂ FPS Prediction**: Responder's predicted FPS after adding this stream must be ≥ `eps_fps` (with tier-based relaxation).
   - **ε₃ Network RTT**: RTSP source round-trip latency must be ≤ `eps_network_ms`.
   - **ε₄ Per-Camera Cooldown**: Camera must not have been migrated in the last 45 seconds.
   - **ε₅ Penalty Check**: Responder must not have an active penalty from a previous failed migration.

3. **Collect Bids**: All peers passing all ε-constraints respond with a **bid** including composite score `F(x)` (estimated load after adding stream).

4. **Elect Winner**: Requester collects bids for `vote_window_s` (3 seconds), selects the peer with the lowest F(x) score.

5. **Escalate on Failure**: If no peer bids, the requester escalates to a more relaxed ε-constraint tier and retries. If all tiers fail, log **CLUSTER_SATURATED** and continue at overload.

### Make-Before-Break Migration

Once the winner is elected:

1. A **decision** is published to `peers/vote/decision` with winner's node ID and camera config.
2. The **winner receives an ADD control command** on `peers/control/{winner_node_id}` with full camera config (URI, homography, ROI, FPS, speed limit, etc.).
3. The winner **immediately begins streaming** from the camera.
4. When the stream reaches **PLAYING state** (buffered), the winner publishes an ack (`peers/vote/ack/{camera_id}`).
5. The **requester waits for this ack** (15-second timeout). Upon receipt, it publishes a **REMOVE command** to stop its own stream.
6. A **per-camera cooldown** (45 seconds) prevents the same camera from being offloaded again immediately.

This **Make-Before-Break** strategy ensures **zero frame loss** during migration.

### Leaderless Failover

When any peer detects another peer is **OFFLINE** (heartbeat timeout):

1. All living peers **independently compute the same assignment** using **consistent hash** of the offline peer's camera list. This deterministic approach ensures all peers elect the same **rescuer** for each orphaned camera—no coordination needed.

2. The elected peer **waits for random jitter** (0–2 seconds uniform) to avoid a "thundering herd".

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
   - GPU %, CPU %, RAM %, temperature, load score.
   - FPS per camera.
   - Active camera list.
3. Subscribes to `traffic/events/{NODE_ID}/**` on Zenoh (published by SpeedProbe).
   - When an overspeed violation is published, forwards it to Server over WebSocket.
   - This avoids the pipeline needing its own WebSocket connection.
4. Implements exponential backoff reconnection (5s, 10s, 20s, 30s, repeat).

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
  - **Secondary Inferences**: Vehicle attribute classifier + License Plate Reader (LPR) classifier.
  - **Analytics**: `nvdsanalytics` probe attachment point.
  - **Tiling**: `nvmultistreamtiler` arranges cameras into grid (2×2 for 4 cameras, etc.).
  - **Rendering**: `nvdsosd` overlays speed, plate text, bounding boxes.
  - **Output Sinks**: Three modes:
    - `display`: X11 EGL sink (HDMI/monitor).
    - `file`: `nvstreamdemux` → per-camera H.264 encoders → MP4 files.
    - `rtsp_push`: H.264 encoder → `rtspclientsink` pushes composite to MediaMTX.

- **`speedflow_python/probes.py`**: **GStreamer pad probes** — functions attached to pipeline pads:
  - **`ROIFilterProbe`**: Filters objects outside per-camera ROI polygon using OpenCV point-in-polygon.
  - **`SpeedProbe`**: Main analytics probe (called once per frame):
    - Iterates over objects; separates vehicles from license plates.
    - **Perspective Transform**: Converts pixel-space vehicle centroids to world coordinates using homography matrix.
    - **Position History**: Maintains deque of world positions per (source_id, track_id).
    - **Speed Calculation**: When history is long enough, computes velocity and converts to km/h.
    - **Validation**: Checks minimum displacement, area stability, confidence, track age.
    - **Median Smoothing**: 3–5 frame window to smooth noisy speeds.
    - **License Plate Tracking**: Accumulates detections over 5 frames; selects most frequent/highest-quality text.
    - **Overspeed Publishing**: If speed ≥ limit, encodes JPEG snapshot and calls `publisher.put(payload)` (non-blocking).
  - **FPS Statistics**: Separate thread reads FPS counters every 2 seconds, writes to JSON file that health_agent consumes.

- **`speedflow_python/camera_config.py`**: **Camera configuration manager**. Reads `cameras.yml`, parses per-camera settings (RTSP URI, homography matrix, ROI polygon, speed limit, FPS). Implements **file-system watcher** (via `watchdog` library) with 100ms debounce to detect `cameras.yml` changes and hot-reload without restarting. Uses `GLib.idle_add()` to queue dynamic stream add/remove operations on GLib Main Loop thread (thread-safe).

#### P2P Orchestration & Load Balancing

- **`speedflow_python/peer_orchestrator.py`**: **Decentralized load balancer**. Implements full P2P orchestration protocol:
  - Maintains in-memory registry of peer state (load score, FPS, active cameras, timestamps).
  - Implements decision loop: monitors own load, triggers RFO when overloaded, collects bids, elects winners, sends migration commands.
  - Detects offline peers via heartbeat timeout; triggers leaderless failover using consistent hashing.
  - Tracks rescued cameras; automatically returns them to recovered owners.
  - Manages per-camera cooldowns to prevent thrashing.
  - All P2P parameters loaded from `Edge/configs/edge_node.yml`.

- **`speedflow_python/zenoh_publisher.py`**: **Non-blocking event publisher**. Maintains bounded queue and daemon thread:
  - When SpeedProbe detects overspeed, calls `publisher.put(payload)` (non-blocking, < 0.1ms).
  - If queue is full (network down), oldest event dropped — pipeline never stalls.
  - Daemon thread consumes queue; publishes via Zenoh to `traffic/events/{node_id}/{camera_id}` (msgpack format).

- **`speedflow_python/zenoh_subscriber.py`**: **Control command receiver**. Subscribes to `peers/control/{node_id}`:
  - **ADD**: Adds new camera; computes homography; enqueues delta for dynamic stream addition.
  - **REMOVE**: Marks camera disabled; enqueues delta for dynamic stream removal.
  - **STATUS**: Returns current active camera list.
  - After successful ADD, waits 3 seconds (GLib processing), then publishes ack to `peers/vote/ack/{camera_id}`.

- **`speedflow_python/zenoh_session.py`**: **Zenoh session factory**. Creates peer-mode Zenoh session (no broker required) with UDP multicast scouting for local-network peer discovery.

#### Monitoring & Communication

- **`health_agent.py`**: **Standalone daemon process** that runs independently from pipeline:
  - Collects hardware metrics every `HEALTH_INTERVAL` seconds: GPU %, CPU %, RAM %, GPU temp (via `jtop` on Jetson, or `psutil` fallback).
  - Reads FPS stats from file written by `SpeedProbe._fps_writer_loop()`.
  - Computes unified **load score** using weighted formula with adaptive ω presets.
  - Publishes health payload to `peers/status/{node_id}` via Zenoh.
  - Opens persistent **WebSocket** to Central Server (`ws://SERVER:PORT/ws/edge?node_id=<NODE_ID>`), implicitly registering node.
  - Subscribes to `traffic/events/{node_id}/**` on Zenoh; forwards violations to Server over WebSocket.
  - Implements exponential backoff reconnection on WebSocket drop.

- **`speedflow_python/monitor_client.py`**: **WebSocket client** used by health_agent:
  - Persistent outbound connection with exponential backoff (5s, 10s, 20s, 30s).
  - Thread-safe queue for outbound messages.
  - Daemon thread consumes queue; sends JSON over WebSocket.
  - URL parameter encoding for special characters in node_id/advertise_ip.

- **`speedflow_python/run_python.py`**: **Pipeline orchestration glue**. Initializes and manages lifecycle of:
  - GStreamer pipeline (via core_pipeline.py).
  - Zenoh publisher/subscriber.
  - Peer orchestrator instance.
  - Probe instances (ROI, Speed).
  - Loop and signal handlers (SIGINT, SIGTERM) for graceful shutdown.
  - **PID file lock** (`run_python.pid`) prevents two instances (would cause MediaMTX publisher conflicts).

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
    - **`GET /api/violations`**: Query violations with filters (node_id, date, limit, offset).
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
  - **Query method**: Synchronous method reads JSONL; returns paginated results (supports filtering by node_id/date).
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

```bash
cd Edge
cp .env.example .env
```

Edit `Edge/.env`:

```ini
NODE_ID=jetson_A
MAX_STREAMS=4

# RTSP push destination (MediaMTX on VPS)
RTSP_PUSH_URL=rtsp://SERVER_IP:8554/jetson_A
RTSP_PUSH_BITRATE=4000000

# Central Monitor (set to your Server IP)
MONITOR_URL=http://SERVER_IP:9090

# Your LAN IP (for Server to display the correct address)
ADVERTISE_IP=192.168.1.200
```

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
| `/api/clusters` | GET | Edges grouped by cluster (IP subnet) |
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
│   ├── generate-compose.sh
│   └── videos/
├── Edge/                           # AI processing node
│   ├── .env                        # Single source of truth
│   ├── main.py
│   ├── health_agent.py             # Hardware metrics → Zenoh + MonitorClient
│   ├── speed_gui.py                # PyQt5 calibration GUI
│   ├── requirements.txt
│   ├── configs/
│   │   ├── cameras.yml                 # Multi-camera sources + homography + ROI
│   │   ├── config_infer_primary_yolo11.txt   # DeepStream GIE — YOLO detector
│   │   ├── config_infer_secondary_lpd.txt    # DeepStream GIE — plate detector
│   │   ├── config_infer_secondary_lpr.txt    # DeepStream GIE — plate reader
│   │   ├── config_nvdsanalytics.txt          # DeepStream analytics
│   │   ├── config_tracker_NvDCF_perf.yml     # NvDCF tracker
│   │   ├── config_tracker_lpd.yml            # Tracker for LPD
│   │   ├── edge_node.yml               # P2P tuning parameters (p2p: section)
│   │   ├── labels_lpd.txt              # Plate detector class labels
│   │   ├── labels_lpr.txt              # Plate reader class labels
│   │   └── labels_YOLO.txt             # YOLO detector class labels
│   └── speedflow_python/
│       ├── __init__.py
│       ├── settings.py               # Config loader (strict validation)
│       ├── common.py                 # GStreamer helpers
│       ├── core_pipeline.py          # DeepStream pipeline builder
│       ├── run_python.py             # Pipeline runner + orchestration
│       ├── peer_orchestrator.py      # P2P load balancing
│       ├── probes.py                 # GStreamer pad probes (SpeedProbe, ROIFilterProbe)
│       ├── plate_preprocessor.py     # Plate image preparation
│       ├── camera_config.py          # Camera config manager + file watcher
│       ├── analytics.py              # Future analytics (stub)
│       ├── draw.py                   # OSD helpers
│       ├── io_utils.py               # Utilities
│       ├── zenoh_publisher.py        # Non-blocking event publisher
│       ├── zenoh_subscriber.py       # Control command receiver
│       ├── zenoh_session.py          # Zenoh session factory
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
| **Health Heartbeat** | Every 2–5 seconds |
| **Peer Decision Loop** | Every 30 seconds |
| **FPS per Camera** | 25–30 fps (configurable) |
| **Speed Accuracy** | ±5% (with proper calibration) |
| **Memory Footprint** | ~800MB–1.2GB per edge (GStreamer + CUDA) |

---

## Known Issues & Fixes

| Issue | File | Status |
|-------|------|--------|
| **BUG-05** | Server/app.py:45 | Watchdog task GC fix — store strong ref |
| **BUG-03/12** | Server/app.py:92 | Path traversal prevention in static serving |
| **BUG-08** | Server/app.py:133 | Event loop blocking fix — use `query_async()` |
| **BUG-09** | Edge/speedflow_python/monitor_client.py:92 | Thread-safe counter reads |
| **BUG-18** | Edge/speedflow_python/monitor_client.py:54 | URL-encode node_id/advertise_ip |
| **BUG-B** | Edge/speedflow_python/monitor_client.py:68 | Lock-ordering inversion fix |
| **BUG-F** | Server/app.py:50 | RuntimeError guard for broadcast outside event loop |

---

## License & Contributing

This is a production-grade research project. For questions or contributions, please open an issue or PR.
