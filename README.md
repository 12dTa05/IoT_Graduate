# IoT_Graduate — Decentralized Real-Time Traffic Monitoring with P2P Load Balancing

A production-grade distributed real-time traffic monitoring system that runs **AI inference (NVIDIA DeepStream with YOLOv8 and custom LPR)** on multiple autonomous Jetson Edge nodes to detect vehicles, measure speed using perspective transform homography, and read license plates in real time.

**Video Architecture**: Each Edge node encodes and pushes a single **RTSP stream** to a central **MediaMTX** relay (all cameras tiled into a single composite stream per node). The MediaMTX server converts incoming RTSP to **WebRTC (WHEP)** for low-latency browser playback — no direct peer connections required.

**Control Plane**: Edge nodes are **fully decentralized** and communicate via **Eclipse Zenoh** (peer mode with UDP multicast scouting) for P2P load balancing and failover. A **Central Monitoring Server** (separate from MediaMTX) aggregates health metrics and violations for visualization and long-term storage.

## System Overview: How It Works

### 1. Vehicle Detection & Speed Measurement

Each Jetson Edge node runs a **real-time DeepStream GStreamer pipeline** that processes multiple RTSP camera sources simultaneously:

- **Vehicle Detection**: YOLO detector (primary inference engine) identifies vehicles in each frame and assigns a unique track ID.
- **Speed Measurement**: Using **homography matrix perspective transform** (calibrated per camera), the system converts pixel-space vehicle positions to real-world coordinates (meters). Velocity is computed as the rate of change in world coordinates over time, then multiplied by 3.6 to convert from m/s to km/h.
- **Validation**: Speed measurements are validated against multiple filters: minimum world displacement, bounding box area stability, detection confidence threshold, and minimum track age (0.5s).
- **Smoothing**: Raw speed samples are smoothed using a median filter (configurable window, typically 3–5 samples) to reject noise and outliers.

### 2. License Plate Detection & Recognition

**License Plate Detection (LPD)**: A secondary classifier marks potential license plate regions in the vehicle bounding box.

**License Plate Recognition (LPR)**: A tertiary OCR-based classifier reads the text from detected plates. The system employs a **frame collection strategy**: it accumulates plate candidates over 5 consecutive frames for a given vehicle track, selects the most frequent / highest-quality plate text, and "locks" it to that vehicle. Once locked, the plate is displayed alongside the speed overlay.

### 3. Region of Interest (ROI) Filtering

To reduce false positives and processing load, each camera has a configurable **polygon ROI** (region of interest) defined in pixel coordinates. A **ROI filter probe** uses OpenCV's point-in-polygon test on the vehicle centroid (bottom center of bounding box) — only vehicles inside the ROI are tracked and analyzed.

### 4. Multi-Camera Streaming & Tiling

The pipeline accepts multiple RTSP sources (up to `MAX_STREAMS`, typically 4 or 8 per Jetson). All cameras are merged into a single video grid via `nvmultistreamtiler`, creating a composite output that shows all camera feeds in a 2×2 or 4×2 layout. This composite is encoded once as H.264 and pushed to the central MediaMTX relay.

### 5. Overspeed Detection & Alerting

When a vehicle's smoothed speed exceeds the per-camera `speed_limit_kmh` threshold:

1. A JPEG snapshot of the vehicle is cropped from the frame and encoded to base64.
2. A **payload** is constructed containing: vehicle track ID, speed, detected license plate (if available), timestamp, camera ID, and the image.
3. The payload is **enqueued** to a non-blocking `ZenohPublisher` queue (maximum size from `.env`).
4. If the queue is full (network down or slow publish), the **oldest** enqueued event is dropped to make room for newer violations — the pipeline never blocks.
5. A separate **daemon thread** consumes the queue and publishes via Zenoh to the key expression `traffic/events/{node_id}/{camera_id}`.
6. The `health_agent.py` process **subscribes** to this topic, receives the violation, and forwards it to the **Central Monitoring Server** over a persistent WebSocket connection.

---

## Key Innovation: P2P Load Balancing & Failover

Each Jetson runs an independent **PeerOrchestrator** instance that implements a decentralized voting protocol over Zenoh. This eliminates the single point of failure of a master orchestrator:

### Peer Status & Monitoring

Every 2 seconds, the **health_agent** on each node collects hardware metrics (GPU %, CPU %, RAM %, temperature) and computes a **unified load score** using a weighted formula:

```
Load Score = 0.5 × GPU% + 0.3 × CPU% + 0.2 × RAM% + FPS Penalty
```

The FPS penalty is applied when the actual average FPS drops below the target — each frame/second lost incurs up to 30 points of penalty. This metric is published to `peers/status/{node_id}` over Zenoh. All peers listen and update their in-memory view of peer state.

Additionally, all peers observe a **heartbeat timeout**: if a peer has not published a status update for `heartbeat_timeout_s` (typically 15 seconds), it is considered **OFFLINE**.

### Request for Offload (RFO) Voting Protocol

When a node detects that it is overloaded (load score > 75%) for more than 10 seconds **continuously**:

1. It **selects a camera to offload** (prioritized by FPS: the camera with the highest FPS is chosen, as reducing high-consuming streams is most effective).
2. It publishes an **RFO (Request for Offload)** to `peers/vote/request`, declaring the camera ID, its own load score, and tiered epsilon-constraint requirements:
   - **ε₁ Capacity**: Responder must have fewer than `eps_streams_max` (e.g., 4) active cameras.
   - **ε₂ FPS Prediction**: Responder must be able to predict that adding this stream will still achieve at least `eps_fps` FPS (with tier-based relaxation: strict, tier 1, tier 2).
   - **ε₃ Network RTT**: The RTSP source must have round-trip latency ≤ `eps_network_ms` (strict or tier 1 / tier 2).
   - **ε₄ Per-Camera Cooldown**: The camera must not have been migrated in the last `cooldown_s` seconds (45 seconds default).
   - **ε₅ Penalty Check**: The responder must not have an active penalty from a previous failed migration.

3. All other peers that **pass all ε-constraints** respond with a **bid** (`peers/vote/proposal`). The bid includes a composite score `F(x)` representing the estimated load this peer would have after accepting the stream.

4. The **requester collects bids for `vote_window_s` seconds** (3 seconds), then selects the **winner** — the peer with the lowest F(x) score that passed the ε-constraints.

5. If no peer bids, the requester **escalates the ε-constraints** to a more relaxed tier and retries. If all tiers fail, the node logs **CLUSTER_SATURATED** and continues operating at overload.

### Make-Before-Break Migration

Once the winner is elected:

1. A **decision is published** to `peers/vote/decision` with the winner's node ID and the camera configuration.
2. The **winner receives a control command** on `peers/control/{winner_node_id}` (ADD command with full camera config: URI, homography, ROI, FPS, speed limit, etc.).
3. The winner **immediately begins streaming** from the camera source.
4. When the stream reaches **PLAYING state** (buffered and flowing), the winner publishes an acknowledgment (`peers/vote/ack/{camera_id}`).
5. The **requester waits for this ack** (timeout: 15 seconds). Upon receipt, it publishes a **REMOVE command** to `peers/control/{self_node_id}` to stop its own stream.
6. A **per-camera cooldown** (45 seconds) prevents the same camera from being offloaded again immediately.

This **Make-Before-Break** strategy ensures **zero frame loss** during migration — the new stream is fully operational before the old one stops.

### Leaderless Failover

When any peer detects that another peer is **OFFLINE** (heartbeat timeout):

1. All living peers **independently compute the same assignment** using a **consistent hash** of the offline peer's camera list, seeded with the sorted list of living peers. This deterministic hash ensures all peers elect the same **rescuer** for each orphaned camera, without coordination.

2. The elected peer **waits for a random jitter** (0–2 seconds, uniform distribution) to avoid a "thundering herd" of simultaneous ADDs.

3. Before rescuing, the peer **verifies the camera RTSP source is reachable** — if the camera was hosted on the dead node's subnet, it will be unreachable, and the rescue is skipped.

4. **Rescued cameras are tracked** separately and automatically **returned** to their original owner if that owner comes back online and re-registers with the same camera list.

---

## Components & Modules

### Architecture Diagram

## Components & System Modules

### Three-Tier Deployment

| Component | Role | Technology |
|---|---|---|
| **Camera Simulator** (`Camera/`) | Docker-based RTSP camera farm. Loops video files as real-time RTSP streams to simulate live traffic cameras. | Docker Compose + ffmpeg |
| **Edge Node** (`Edge/`) | AI processing unit (Jetson Orin/NX/Nano). Runs the DeepStream pipeline, health monitoring, and P2P orchestration. Multiple nodes form a decentralized cluster with no master. | NVIDIA DeepStream, GStreamer, Python, Zenoh, aiohttp |
| **Central Server** (`Server/`) | Aggregation and visualization layer. Receives violations and health data from all Edge nodes, stores them persistently, and serves the live dashboard. Separate from video relay. | Python aiohttp, PostgreSQL-ready (currently JSONL), Zenoh optional |

### Edge Node Architecture — Detailed Modules

#### Core Pipeline & Detection

- **`main.py`**: CLI entry point. Parses command-line arguments (`--mode display/file/rtsp_push`, `--rtsp-push-url`, `--width`, `--height`) and delegates to `run_python_mode()`.

- **`speedflow_python/settings.py`**: **Single source of truth** for all runtime configuration. Loads from `Edge/.env` using `python-dotenv`. Implements strict validation: any missing required variable raises an error at import time (no silent defaults). This ensures configuration mismatches are caught immediately.

- **`speedflow_python/core_pipeline.py`**: **DeepStream pipeline builder**. Constructs a complete GStreamer pipeline with these stages:
  - **Demultiplexing**: N RTSP source bins (`uridecodebin`) feeding into a single `nvstreammux` (batch muxer) that merges all cameras.
  - **Primary Inference (PGIE)**: YOLO detector (vehicle detection and classification).
  - **Tracking**: NvDCF tracker — assigns consistent track IDs across frames using motion prediction.
  - **Secondary Inferences**: Vehicle attribute classifier (type: car, bus, etc.) and License Plate Reader (LPR) classifier.
  - **Analytics**: `nvdsanalytics` probe attachment point for custom logic.
  - **Tiling**: `nvmultistreamtiler` arranges all camera feeds into a grid (e.g., 2×2 for 4 cameras).
  - **Rendering**: `nvdsosd` (on-screen display) overlays speed, plate text, and bounding boxes.
  - **Output Sinks**: Supports three output modes:
    - `display`: X11 EGL sink for direct HDMI/monitor output.
    - `file`: `nvstreamdemux` → per-camera H.264 encoders → MP4 files.
    - `rtsp_push`: H.264 encoder → `rtspclientsink` pushes composite stream to MediaMTX.

- **`speedflow_python/probes.py`**: **GStreamer pad probes** — functions attached to pipeline pads for real-time processing:
  - **`ROIFilterProbe`**: Runs on the analytics pad. Filters out objects outside the per-camera ROI polygon using OpenCV point-in-polygon testing.
  - **`SpeedProbe`**: Main analytics probe. Runs on the OSD pad (called once per frame):
    - Iterates over all objects in the frame and separates vehicles from license plates.
    - **Perspective Transform**: Converts pixel-space vehicle centroid coordinates to world coordinates using the calibrated homography matrix.
    - **Position History**: Maintains a deque of world positions for each (source_id, track_id) pair.
    - **Speed Calculation**: When the history is long enough, computes velocity (distance/time) and converts to km/h.
    - **Validation**: Checks minimum displacement, area stability, detection confidence, track age.
    - **Median Smoothing**: Applies a sliding-window median filter to smooth noisy speed estimates.
    - **License Plate Tracking**: Accumulates plate detections over 5 frames, selects the most frequent/highest-quality text.
    - **Overspeed Publishing**: If speed ≥ limit, encodes a JPEG snapshot and calls `publisher.put(payload)` (non-blocking).
  - **FPS Statistics**: A separate thread reads FPS counters every 2 seconds and writes them to a JSON file that `health_agent.py` consumes.

- **`speedflow_python/camera_config.py`**: **Camera configuration manager**. Reads `cameras.yml`, parses per-camera settings (RTSP URI, homography matrix, ROI polygon, speed limit, FPS). Implements **file-system watcher** (via `watchdog` library) with 100ms debounce to detect changes to `cameras.yml` and hot-reload camera configurations without restarting the pipeline. Uses `GLib.idle_add()` to queue dynamic stream add/remove operations on the GLib Main Loop thread, ensuring thread safety.

#### P2P Orchestration & Load Balancing

- **`speedflow_python/peer_orchestrator.py`**: **Decentralized load balancer**. Implements the full P2P orchestration protocol:
  - Maintains an in-memory registry of peer state (load score, FPS, active cameras, timestamps).
  - Implements the decision loop: monitors own load, triggers RFO when overloaded, collects bids, elects winners, sends migration commands.
  - Detects offline peers via heartbeat timeout and triggers leaderless failover using consistent hashing.
  - Tracks rescued cameras and returns them to recovered owners.
  - Manages per-camera cooldowns to prevent thrashing.
  - All P2P parameters (thresholds, timeouts, ε-constraints, FPS prediction model) are loaded from `Edge/configs/edge_node.yml`.

- **`speedflow_python/zenoh_publisher.py`**: **Non-blocking event publisher**. Maintains a bounded queue (size from `.env`) and a daemon thread:
  - When `SpeedProbe` detects an overspeed violation, it calls `publisher.put(payload)` (non-blocking, < 0.1ms).
  - If the queue is full (network down), the oldest event is dropped to make room — the pipeline never stalls.
  - The daemon thread consumes the queue and publishes via Zenoh to `traffic/events/{node_id}/{camera_id}` (msgpack format).

- **`speedflow_python/zenoh_subscriber.py`**: **Control command receiver**. Subscribes to `peers/control/{node_id}` and listens for ADD/REMOVE/STATUS commands from `peer_orchestrator`:
  - **ADD**: Adds a new camera to the configuration, computes its homography matrix, and enqueues a delta for dynamic stream addition.
  - **REMOVE**: Marks a camera as disabled and enqueues a delta for dynamic stream removal.
  - **STATUS**: Returns the current active camera list.
  - After a successful ADD, waits 3 seconds (for GLib processing) then publishes an acknowledgment to `peers/vote/ack/{camera_id}`.

- **`speedflow_python/zenoh_session.py`**: **Zenoh session factory**. Creates a peer-mode Zenoh session (no broker required) with UDP multicast scouting for local-network peer discovery.

#### Monitoring & Communication

- **`health_agent.py`**: **Standalone daemon process** that runs independently from the pipeline:
  - Collects hardware metrics every `HEALTH_INTERVAL` seconds: GPU %, CPU %, RAM %, GPU temperature (via `jtop` on Jetson, or `psutil` as fallback).
  - Reads FPS statistics from the file written by `SpeedProbe._fps_writer_loop()`.
  - Computes the unified **load score** using the weighted formula.
  - Publishes the health payload (with active cameras list and FPS per camera) to `peers/status/{node_id}` via Zenoh.
  - Opens a persistent **WebSocket connection** to the Central Monitoring Server (`ws://SERVER:PORT/ws/edge?node_id=<NODE_ID>`), which implicitly registers the node.
  - Subscribes to `traffic/events/{node_id}/**` on Zenoh and forwards all overspeed violations to the Server over the same WebSocket.
  - Implements exponential backoff reconnection if the WebSocket drops.

- **`speedflow_python/monitor_client.py`**: **WebSocket client** used by `health_agent`. Implements:
  - Persistent outbound connection with exponential backoff reconnection (5s, 10s, 20s, 30s).
  - Thread-safe queue for outbound messages.
  - Daemon thread that consumes the queue and sends JSON text frames over WebSocket.
  - URL parameter encoding for special characters in node_id and advertise_ip.

- **`speedflow_python/run_python.py`**: **Pipeline orchestration glue**. Initializes and manages the lifecycle of:
  - GStreamer pipeline (via `core_pipeline.py`).
  - Zenoh publisher and subscriber.
  - Peer orchestrator instance.
  - Probe instances (ROI, Speed).
  - Loop handlers and signal handlers (SIGINT, SIGTERM) for graceful shutdown.
  - Implements a **PID file lock** (`run_python.pid`) to prevent two instances from running simultaneously (which would cause MediaMTX publisher conflicts).

### Server (Central Monitoring & Dashboard)

#### REST API & Data Aggregation

- **`app.py`**: Async Python web server (aiohttp) with lifecycle management:
  - **Startup**: Opens HTTP session and starts the edge registry watchdog.
  - **Shutdown**: Cancels pending tasks, closes all WebSocket connections, flushes data.
  - Uses **`SO_REUSEADDR`** on the TCP socket to allow rapid restart without "Address already in use" errors.
  - Routes:
    - **`GET /`**: Dashboard HTML (single-page app).
    - **`GET /health`**: Health check endpoint.
    - **`GET /api/edges`**: Returns all registered edges with live health state.
    - **`GET /api/clusters`**: Groups edges by cluster (IP subnet or cluster_id from health data).
    - **`GET /api/violations`**: Queries violations from `ViolationStore` with filters (node_id, date, limit, offset).
    - **`GET /api/streams`**: Proxies to MediaMTX API to list active RTSP sessions.
    - **`GET /api/snapshots/{node_id}/{filename}`**: Serves snapshot images (path traversal-safe).
    - **`GET /ws/edge`**: WebSocket endpoint for Edge nodes. Implicit registration: any node connecting is added to the registry. Receives health updates and violations, broadcasts to browser WebSockets.
    - **`GET /ws/server`**: WebSocket endpoint for browsers. Receives live push updates (health + violations) from the server.

- **`edge_registry.py`**: In-memory registry of all connected Edge nodes:
  - Stores `EdgeInfo` (node_id, IP, online status, last heartbeat, health metrics, cluster_id).
  - Implements a **watchdog task** that runs every 5 seconds, checking for heartbeat timeout (15 seconds). Any node that has not sent a heartbeat in 15 seconds is marked **OFFLINE**, and a callback is fired (usually broadcasts an "edge_offline" event to browser WebSockets).
  - Provides methods to query edges: all, online only, or grouped by cluster.

- **`violation_store.py`**: Persistent storage for violations:
  - Organizes files by date and node_id: `violations/{date}/{node_id}/violations.jsonl` and `violations/{date}/{node_id}/{camera_id}_{timestamp_ms}.jpg`.
  - **Async write**: All violations are appended to JSONL files using `aiofiles` to avoid blocking the event loop.
  - **Snapshot handling**: JPEG snapshots (base64-encoded in the violation payload) are decoded and saved separately.
  - **Query method**: Synchronous method that reads JSONL files and returns paginated results (supports filtering by node_id and date).
  - Single `asyncio.Lock` guards all async writes to prevent concurrent write races.

#### Dashboard (Frontend)

- **`static/index.html`**: Single-page application (SPA) with three main panels:
  - **Live Video Panel**: Auto-discovers WebRTC streams by querying the `/api/streams` endpoint (which proxies to MediaMTX) and the `/api/edges` endpoint (which lists active cameras from edge health data). Uses WHEP (WebRTC-HTTP Egress Protocol) to fetch low-latency video from MediaMTX.
  - **Cluster Status Panel**: Displays live edge cards showing node_id, IP, online status, GPU %, CPU %, RAM %, temperature, load score, and FPS per camera. Updates are pushed in real time via the `/ws/server` WebSocket, with DOM diffing to prevent flicker.
  - **Violation Feed Panel**: Shows all violations sorted newest-first. Each violation card displays: camera ID, timestamp, detected license plate, speed, and a thumbnail of the snapshot. Supports filtering by node_id and date range.

- **`mediamtx.yml` / `docker-compose.media.yml`**: MediaMTX (Bluenviron's high-performance RTSP relay) running in Docker:
  - Listens on port 8554 (RTSP input/output).
  - Listens on port 8889 (WebRTC WHEP output).
  - Listens on port 9997 (REST API for stream introspection).
  - **ICE configuration**: Includes `webrtcICEHostNAT1To1IPs` for NAT traversal (clients behind NAT can connect via the server's public IP).
  - **HLS output**: Optional HTTP Live Streaming for compatibility.

---

## Data Flow Diagrams & Protocols

## Architecture Diagram

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
│ │(Multi-   │ │  │ │(Multi-   │ │  │ │(Multi-   │ │
│ │Camera)   │ │  │ │Camera)   │ │  │ │Camera)   │ │
│ └────┬─────┘ │  │ └────┬─────┘ │  │ └────┬─────┘ │
│      │       │  │      │       │  │      │       │
│ ┌────▼─────┐ │  │ ┌────▼─────┐ │  │ ┌────▼─────┐ │
│ │PeerOrch  │ │  │ │PeerOrch  │ │  │ │PeerOrch  │ │
│ │(Voting,  │ │  │ │(Voting,  │ │  │ │(Voting,  │ │
│ │Failover) │ │  │ │Failover) │ │  │ │Failover) │ │
│ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │
│              │  │              │  │              │
│ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │
│ │HealthAg │ │  │ │HealthAg │ │  │ │HealthAg │ │
│ │(metrics,│ │  │ │(metrics,│ │  │ │(metrics,│ │
│ │WebSocket)│ │  │ │WebSocket)│ │  │ │WebSocket)│ │
│ └────┬─────┘ │  │ └────┬─────┘ │  │ └────┬─────┘ │
└──────┼───────┘  └──────┼───────┘  └──────┼───────┘
       │                 │                 │
       │  ① RTSP push    │                 │
       │  rtspclient     │  ② WebSocket    │
       │  sink           │  register +     │
       │                 │  health         │
       ▼                 ▼                 ▼
  ┌───────────────────────────────────────────────────┐
  │        MediaMTX (RTSP ↔ WebRTC Relay)             │
  │   Ports: :8554 (RTSP), :8889 (WHEP), :9997 (API) │
  │   Functions: tiling, format conversion, relay     │
  └─────────────────────┬─────────────────────────────┘
                        │
                        │ ③ WebRTC (low-latency video)
                        │ WHEP (WebRTC-HTTP Egress)
                        ▼
     ┌──────────────────────────────────────────────┐
     │   Central Monitoring Server (aiohttp)        │
     │   - EdgeRegistry (in-memory state)           │
     │   - ViolationStore (JSONL + images)          │
     │   - WebSocket broadcast to browsers          │
     │   REST API: /api/edges, /api/violations, etc.│
     └──────────┬───────────────────────────────────┘
                │ ④ WebSocket push
                │ (health + violations)
                ▼
     ┌──────────────────────────────┐
     │   Browser Dashboard (SPA)    │
     │   - Live video grid (WHEP)   │
     │   - Cluster status (live)    │
     │   - Violation feed           │
     └──────────────────────────────┘
```

## Data Flow Descriptions

### ① Edge Node → MediaMTX (RTSP Video Push)

Each Jetson Edge node runs the DeepStream pipeline (`main.py --mode rtsp_push`), which tiled together all N camera streams into a single composite video grid (e.g., 2×2 for 4 cameras, 4×2 for 8 cameras). This composite video is:

1. Encoded in real time as H.264 using NVIDIA's hardware video encoder (`nvv4l2h264enc`).
2. Pushed continuously to the central MediaMTX relay via `rtspclientsink` (RTSP protocol, TCP transport).
3. The MediaMTX relay stores the RTSP session and broadcasts it to multiple output formats (RTSP passthrough, WebRTC, HLS).

**Purpose**: Centralized video aggregation and relay, so browsers do not need to connect directly to Jetson devices (which may be on restricted networks or behind NAT).

### ② Edge Node → Central Server (WebSocket Health & Violation Data)

The `health_agent.py` daemon runs as a separate process on each Edge:

1. Opens a persistent WebSocket connection to `ws://SERVER:PORT/ws/edge?node_id=<NODE_ID>&advertise_ip=<LAN_IP>`.
   - The query parameters are URL-encoded to handle special characters.
   - The connection implicitly registers the node with the server (no separate registration endpoint needed).

2. Every `HEALTH_INTERVAL` seconds (e.g., 2 seconds), it publishes a health message containing:
   - Hardware metrics: GPU %, CPU %, RAM %, temperature.
   - Load score (weighted formula).
   - FPS per camera (read from file written by the pipeline probe).
   - Active camera list.

3. The daemon also subscribes to `traffic/events/{NODE_ID}/**` on Zenoh (published by the pipeline's `SpeedProbe` via `ZenohPublisher`).
   - When an overspeed violation is published, the daemon captures it and forwards it to the server over the same WebSocket.
   - This avoids the pipeline needing its own WebSocket connection — a single connection per node for all data.

4. If the WebSocket connection drops, the daemon implements **exponential backoff reconnection**: 5s, 10s, 20s, 30s, then retry.

**Note**: The pipeline process (`main.py --mode rtsp_push`) does NOT directly connect to the server. It only publishes events via Zenoh, and the health_agent bridges them.

### ③ MediaMTX → Browser (WebRTC Playback)

The browser dashboard uses the **WHEP (WebRTC-HTTP Egress Protocol)** to fetch streams from MediaMTX at low latency:

1. For each active camera, the dashboard sends an HTTP POST request to `http://SERVER:8889/<node_id>/whep` with an SDP offer.
2. MediaMTX responds with an SDP answer, and the two peers establish a WebRTC connection.
3. ICE (Interactive Connectivity Establishment) negotiates the best path through NAT/firewalls.
4. WebRTC streams inbound video from MediaMTX with minimal latency (~100-500ms, vs. RTSP's 1-5s).

The server does NOT relay video itself — it only proxies health and violation data over WebSocket.

### ④ Central Server → Browser (WebSocket Push)

The browser connects to `ws://SERVER:PORT/ws/server` to receive live updates:

- **Health updates**: Whenever an Edge sends a new health message, the server broadcasts it to all connected browsers.
- **Violation events**: Whenever a violation is received (from any Edge), the server appends it to the ViolationStore and pushes it to all browsers.
- **Edge offline**: When an Edge misses heartbeats for 15 seconds, the watchdog marks it offline and broadcasts an "offline" event to browsers.

The dashboard updates its UI in real time using DOM diffing to prevent flicker and unnecessary re-renders.

### ⑤ Edge ↔ Edge (Zenoh P2P Control)

All inter-node communication (peer discovery, voting, control commands, failover) occurs via Zenoh key expressions (see section below). This includes:

- Health status broadcasts (peers/status/{node_id}).
- Load balancing voting (peers/vote/request, peers/vote/proposal, peers/vote/decision).
- Control commands (peers/control/{node_id}: ADD camera, REMOVE camera, STATUS).
- Vote acknowledgments (peers/vote/ack/{camera_id}: stream is PLAYING).
- Failover coordination (leaderless, deterministic via consistent hashing).

---

## Zenoh Key Expressions & Protocol Reference

Zenoh is a pub-sub framework with hierarchical key expressions (similar to MQTT topics but with richer semantics). All peer communication is **brokerless** in peer mode, using UDP multicast scouting for local discovery.

| Key Expression | Pub | Sub | Payload | Semantics |
|---|---|---|---|---|
| `peers/status/{node_id}` | health_agent | peer_orchestrators | msgpack: GPU%, CPU%, RAM%, temp, load_score, FPS per camera, active cameras | **Heartbeat & metrics**. Published every 2 seconds. If not heard for 15s, peer is marked offline. |
| `peers/vote/request` | peer_orch (RFO sender) | all peer_orchs | msgpack: requester, camera_id, load_score, avg_fps, eps_fps, eps_network_ms, tier | **Request for Offload (RFO)**. Sent when node is overloaded. Initiates voting window. |
| `peers/vote/proposal` | peer_orch (responder) | requester | msgpack: bidder, camera_id, score (F(x)), fps_predicted, rtt_ms | **Bid from responder**. Sent if all ε-constraints are satisfied. Collected for 3 seconds. |
| `peers/vote/decision` | peer_orch (requester) | winner + requester | msgpack: winner, camera_id, from_node, cam_config, ts | **Election result**. Winner receives ADD command; requester prepares to REMOVE. |
| `peers/vote/ack/{camera_id}` | zenoh_subscriber (winner) | requester | msgpack: node_id, camera_id, event: PLAYING | **Acknowledgment**. Stream is PLAYING. Requester waits for this before REMOVE. |
| `peers/control/{node_id}` | peer_orch | zenoh_subscriber | msgpack: cmd (ADD/REMOVE/STATUS), camera_id, source_id, uri, homography, roi_polygon, fps, speed_limit | **Control command**. ADD (with full config) or REMOVE (camera_id only) or STATUS (query). |
| `traffic/events/{node_id}/{camera_id}` | zenoh_publisher (probe) | health_agent | msgpack: type (overspeed), node_id, camera_id, ts, track_id, speed_kmh, license_plate, image_b64, dedup_key | **Overspeed event**. Published by SpeedProbe when vehicle exceeds speed limit. Includes snapshot. |

---

## Speed Measurement Methodology

The system's core innovation is the **perspective transform-based speed measurement**, which converts pixel-space motion to real-world velocity:

1. **Calibration (Offline)**: For each camera, the operator defines:
   - A **source polygon** (4 points) in the video frame representing a known real-world distance (e.g., a lane of 3.75m width).
   - A **target rectangle** in the world plane (e.g., 0 to 3.75m in X, 0 to some distance in Y).
   - The system computes the homography matrix `H` that maps source → target coordinates.

2. **Real-Time Tracking**:
   - For each vehicle, the YOLO detector provides a bounding box: (left, top, width, height).
   - The center X and bottom Y are extracted: `(cx, cy_bottom) = (left + width/2, top + height)`.
   - The homography transforms this point: `(cx, cy_bottom) → (world_x, world_y)`.
   - Typically, only `world_y` (position along the road direction) is used for speed calculation.

3. **Velocity Computation**:
   - A history of world Y positions is maintained per track.
   - Speed = `Δworld_Y / Δtime × 3.6` (to convert m/s to km/h).
   - Raw speeds are noisy, so a **median filter** (window size 3–5 samples) smooths them.

4. **Validity Checks**:
   - Minimum world displacement (e.g., 0.5m across frames) to reject stationary objects.
   - Bounding box area stability (max 3× change) to reject detection artifacts.
   - Minimum track age (0.5 seconds) to reject newly detected objects.
   - Detection confidence threshold (default 0.5) to reject low-quality detections.

5. **Overspeed Detection**:
   - If smoothed speed ≥ camera's `speed_limit_kmh`, a violation is published.
   - A **cooldown timer** per track (2.5 seconds) prevents duplicate alerts for the same vehicle.

---

## License Plate Recognition (LPR) Strategy

1. **Detection Phase**: The LPD (License Plate Detector) secondary classifier scans each vehicle's bounding box and marks potential plate regions.

2. **Recognition Phase**: The LPR classifier reads text from detected plates. However, plate detection and recognition quality vary per frame (due to angle, lighting, focus).

3. **Stabilization Strategy**:
   - The system accumulates plate candidates over **5 frames** per vehicle track.
   - All detected plate texts are collected into a list.
   - The text that appears **most frequently** (voting) is selected as the best candidate.
   - If multiple texts have equal frequency, the one with the highest quality score (based on bbox area, aspect ratio, confidence) wins.
   - Once a plate text is locked to a vehicle, it is displayed for the remainder of that vehicle's track.

4. **Retry Logic**:
   - If no valid plate is detected after 5 frames, up to 3 retry attempts are made (15 frames total).
   - After 3 failed attempts, the vehicle is marked with `plate_locked = None` (no plate read).

This strategy reduces false positives and ensures stable plate display even under poor lighting or angle conditions.

## Prerequisites

- **Camera node**: Docker & Docker Compose
- **Edge nodes**: NVIDIA Jetson (Orin/NX/Nano) with JetPack 6.x and DeepStream SDK 7.x
- **Server**: Any machine with Python 3.8+ (VPS recommended for public access)
- **Python**: 3.8+ on all nodes

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

The Server uses `SO_REUSEADDR` on the TCP socket to survive rapid restarts
without hitting "address already in use". A systemd service file is provided
at `/etc/systemd/system/monitor-server.service` with `Restart=always` and
`RestartSec=10` for production deployments.

### 2. Camera Node — RTSP simulator

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

> **Note**: `main.py` does NOT connect to the Server directly. It publishes overspeed
> events over Zenoh; `health_agent.py` subscribes and forwards them. This avoids
> duplicate WebSocket connections for the same node_id.

### 5. Dashboard — Browser

Open [http://<SERVER_IP>:9090](http://<SERVER_IP>:9090). The dashboard shows:

- **Live Video panel**: auto-discovered WebRTC streams from each Edge (WHEP player).
  Streams appear from both MediaMTX API data and edge health `active_cameras`.
- **Cluster Status panel**: live edge cards with GPU%, CPU%, RAM%, temp, load score,
  FPS per camera (DOM-diff updated to avoid flicker).
- **Violation Feed panel**: live violations sorted newest-first with plate, speed,
  snapshot thumbnails, and filters by node/date.

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

## Zenoh Key Expressions

| Key Expression | Publisher | Subscribers | Payload |
|---|---|---|---|
| `peers/status/{node_id}` | `health_agent.py` | `peer_orchestrator.py` | msgpack — GPU%, CPU%, FPS, temp, load_score |
| `peers/vote/request` | `peer_orchestrator.py` | all nodes | msgpack — RFO: camera, load, ε constraints |
| `peers/vote/proposal` | `peer_orchestrator.py` | requester | msgpack — bid: predicted FPS, capacity |
| `peers/vote/decision` | `peer_orchestrator.py` | winner + requester | msgpack — winner node_id, cam_config |
| `peers/vote/ack/{cam_id}` | `zenoh_subscriber.py` | requester | msgpack — stream is PLAYING |
| `peers/control/{node_id}` | `peer_orchestrator.py` | `zenoh_subscriber.py` | msgpack — ADD / REMOVE / STATUS |
| `traffic/events/{node_id}/{cam_id}` | `zenoh_publisher.py` | `health_agent.py` | msgpack — speed event per vehicle |

## P2P Load Balancing Details

The PeerOrchestrator implements a **Pareto ε‑constraint** voting protocol:

1. **Monitoring** — health agent publishes `load_score` to `peers/status/{node_id}` every 2s.
2. **RFO trigger** — if `load_score > overload_threshold` for `overload_duration_s` seconds, the node selects its worst camera and publishes an RFO to `peers/vote/request` with tiered constraints.
3. **Proposal window** — peers with available capacity bid via `peers/vote/proposal`. Window closes after `vote_window_s` seconds.
4. **Winner selection** — proposals are filtered through ε‑constraint tiers (strict FPS floor → tier 1 → tier 2 → network latency). From the surviving set, the proposal with lowest composite score wins.
5. **Make‑before‑Break** — winner receives an ADD command on `peers/control/{node_id}`, starts the stream, and publishes `peers/vote/ack/{cam}`. The RFO sender waits for this ack before removing the camera. A cooldown is set on the requester side immediately on election publish to prevent duplicate RFOs.
6. **Failover** — if a peer's heartbeat stops for `heartbeat_timeout_s`, each surviving peer runs consistent‑hash on that peer's camera list and rescues orphaned streams.
7. **Cooldown** — per‑camera cooldown prevents thrashing.

All P2P parameters are configurable in `Edge/configs/edge_node.yml` — the `p2p:` section only (scalar runtime values live in `.env`). The FPS prediction model should be calibrated offline per Jetson model.

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
│   │   ├── cameras.yml                 # Multi-camera sources + homography + ROI (per-camera)
│   │   ├── config_infer_primary_yolo11.txt   # DeepStream GIE — YOLO detector
│   │   ├── config_infer_secondary_lpd.txt    # DeepStream GIE — license plate detector
│   │   ├── config_infer_secondary_lpr.txt    # DeepStream GIE — license plate reader
│   │   ├── config_nvdsanalytics.txt          # DeepStream analytics (ROI/line rules)
│   │   ├── config_tracker_NvDCF_perf.yml     # DeepStream NvDCF tracker
│   │   ├── config_tracker_lpd.yml            # DeepStream tracker for LPD
│   │   ├── edge_node.yml               # P2P tuning parameters only (p2p: section)
│   │   ├── labels_lpd.txt              # License plate detector class labels
│   │   ├── labels_lpr.txt              # License plate reader class labels
│   │   └── labels_YOLO.txt             # YOLO detector class labels
│   └── speedflow_python/
│       ├── __init__.py
│       ├── settings.py
│       ├── common.py               # GStreamer helpers (make_element, gst_link)
│       ├── core_pipeline.py        # DeepStream pipeline builder
│       ├── run_python.py           # Pipeline runner + Zenoh + MonitorClient
│       ├── peer_orchestrator.py    # P2P load balancing
│       ├── probes.py               # GStreamer pad probes
│       ├── plate_preprocessor.py
│       ├── camera_config.py
│       ├── analytics.py
│       ├── draw.py
│       ├── io_utils.py
│       ├── zenoh_publisher.py
│       ├── zenoh_subscriber.py
│       ├── zenoh_session.py
│       ├── monitor_client.py       # WS client → Central Monitor
│       └── ...
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
