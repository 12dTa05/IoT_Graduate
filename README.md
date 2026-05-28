# IoT_Graduate — Centralized Video Streaming Multi‑Edge Traffic Monitoring System

A distributed real‑time traffic monitoring system that runs **AI (NVIDIA DeepStream)** on multiple Jetson Edge nodes to measure vehicle speed, detect vehicles, and read license plates.

**Video architecture**: Each Edge pushes a single RTSP stream to a central **MediaMTX** server which relays it to browsers via **WebRTC (WHEP)** — no direct browser-to-Edge connections.

**Control plane**: Edge nodes communicate over **Eclipse Zenoh** (peer mode, UDP multicast scouting) for P2P load balancing, with a **Central Monitoring Server** for aggregation and visualization.

## Key Innovation: P2P Load Balancing

Each Jetson runs an independent **PeerOrchestrator** instance that communicates over Zenoh (peer mode):

- **ε‑constraint Pareto optimization** — when a node is overloaded (low FPS, high GPU), it publishes a **Request For Offload (RFO)**. Peers respond with bids, and the winner is the bid with the lowest composite score `F(x)` that satisfies all tiered constraints (FPS floor, network latency ceiling, stream capacity).
- **Make‑before‑Break migration** — the winning peer adds the camera to its pipeline first, waits for the stream to reach `PLAYING` state, and *then* signals the overloaded peer to remove it. Zero frame loss.
- **Consistent‑hashing failover** — camera-to-node assignments are deterministic (hash ring). When a peer goes offline, its cameras redistribute in `O(1)` per camera.

## Components

| Component | Description |
|---|---|
| **Camera** (`Camera/`) | Docker‑based RTSP camera simulator using MediaMTX. Loops video files as RTSP streams. |
| **Edge** (`Edge/`) | Jetson AI processing node. Runs DeepStream pipelines for speed, LPR, and overspeed alerts. Each node has a PeerOrchestrator, health agent, and RTSP push to central MediaMTX. |
| **Server** (`Server/`) | Central Monitoring Server + MediaMTX. Accepts WebSocket connections from each Edge for health/violation data, serves a live dashboard, and relays RTSP streams to browsers via WebRTC. |

### Edge Node Internals

| Module | Role |
|---|---|
| `main.py` | Entry point — parses `--mode` / `--rtsp-push-url` / `--width` / `--height` args and calls `run_python_mode` |
| `speedflow_python/settings.py` | Loads all config from `Edge/.env` (single source of truth) |
| `speedflow_python/run_python.py` | Pipeline runner — wires Zenoh, PeerOrchestrator, probes, and MonitorClient |
| `speedflow_python/peer_orchestrator.py` | P2P brain — ε‑constraint voting, Make‑before‑Break migration, failover |
| `speedflow_python/core_pipeline.py` | DeepStream pipeline builder — supports `display`, `file`, `rtsp_push` sink types |
| `speedflow_python/probes.py` | GStreamer pad probes — ROI filter, speed calc, FPS stats |
| `speedflow_python/zenoh_publisher.py` | Publishes speed events and overspeed alerts via Zenoh |
| `speedflow_python/zenoh_subscriber.py` | Handles `peers/control/{node_id}` commands (ADD/REMOVE stream) |
| `speedflow_python/zenoh_session.py` | Zenoh session factory (peer mode config) |
| `speedflow_python/monitor_client.py` | Outbound WebSocket client to Central Monitor; daemon thread with auto-reconnect |
| `health_agent.py` | Jetson metrics collector (GPU, CPU, temp, FPS) → `peers/status/{node_id}` + MonitorClient |

### Server Internals

| Module | Role |
|---|---|
| `app.py` | aiohttp server with REST API + WebSocket push to browsers. Uses `SO_REUSEADDR` for crash-safe restarts. |
| `edge_registry.py` | Tracks all registered Edges with live health state; heartbeat watchdog (15s timeout, 5s check interval) |
| `violation_store.py` | Persists violations as JSONL + snapshot images (async save via aiofiles) |
| `static/index.html` | Single‑page dashboard with live video grid + cluster status + violation feed. Discovers streams from both edge health data and MediaMTX API. |
| `mediamtx.yml` / `docker-compose.media.yml` | MediaMTX config: RTSP on :8554, WebRTC on :8889, API on :9997 |

## Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  Zenoh Peer Mode (UDP multicast scouting over LAN switch)        │
│    No broker — every node discovers peers automatically          │
└───────────────────────────────────────────────────────────────────┘
         ▲            ▲            ▲            ▲
         │ peers/    │ peers/     │ peers/     │ peers/
         │ status/   │ vote/      │ control/   │ status/
         ▼            ▼            ▼            ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Jetson A     │  │  Jetson B     │  │  Jetson C     │
│  PeerOrch     │◄─┤  PeerOrch     │◄─┤  PeerOrch     │
│  Pipeline     │  │  Pipeline     │  │  Pipeline     │
│  HealthAgent  │  │  HealthAgent  │  │  HealthAgent  │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
        │                 │                 │
        │  ① RTSP push    │                 │
        │   rtsp://SERVER:8554/<node_id>    │
        ▼                 ▼                 ▼
┌───────────────────────────────────────────────────────────────────┐
│                MediaMTX (bluenviron/mediamtx)                     │
│    RTSP → WebRTC relay on VPS (116.118.9.125)                    │
│    Ports: :8554 (RTSP), :8889 (WHEP), :9997 (API)               │
└──────────────────────────┬────────────────────────────────────────┘
                           │
                           │  ② WHEP (WebRTC) stream
                           ▼
┌───────────────────────────────────────────────────────────────────┐
│                Central Monitoring Server (Server/)                │
│    http://116.118.9.125:9090                                      │
│                                                                   │
│  ┌────────────────────┐  ┌──────────────────────┐                │
│  │   EdgeRegistry      │  │   ViolationStore     │                │
│  │  (live edge state)  │  │  (JSONL + images)    │                │
│  └────────┬───────────┘  └──────────┬───────────┘                │
│           │                        │                            │
│           └──────────┬─────────────┘                            │
│                      │                                          │
│             ┌────────▼────────┐                                 │
│             │  Browser WS     │                                 │
│             │  /ws/server     │                                 │
│             │  (live push)    │                                 │
│             └────────┬────────┘                                 │
└──────────────────────┼──────────────────────────────────────────┘
                       │
                       ▼
               ┌────────────────┐
               │  Dashboard     │
               │  (index.html)  │
               │  Video Grid +  │
               │  Cluster +     │
               │  Violations    │
               └────────────────┘
```

## Data Flows

### Edge → MediaMTX (RTSP video push)

Each Edge opens a single RTSP push connection to `rtsp://SERVER_IP:8554/<node_id>` using `rtspclientsink`. The tiled DeepStream output (all cameras merged into one grid) is encoded as H.264 and pushed over TCP.

### Edge → Server (WebSocket data channel)

The Edge runs **two separate processes** — `health_agent.py` and `main.py --mode rtsp_push`:

1. **`health_agent.py`** (standalone daemon): Opens a persistent WebSocket to
   `ws://SERVER:PORT/ws/edge?node_id=<NODE_ID>&advertise_ip=<LAN_IP>` (implicit
   registration). Pushes health metrics every `HEALTH_INTERVAL` seconds.
   Also subscribes to Zenoh `traffic/events/{NODE_ID}/**` and forwards overspeed
   events to the Server via the same WebSocket.

2. **`main.py --mode rtsp_push`** (pipeline): Does **not** create its own WebSocket
   connection — it publishes overspeed events via Zenoh, which `health_agent.py`
   picks up and forwards. This avoids duplicate WS connections for the same node_id.

   A PID file (`run_python.pid`) prevents two instances from running simultaneously
   (which would cause MediaMTX publisher conflicts).

### Edge ↔ Edge (Zenoh peer mode)

All P2P coordination (status, voting, control, failover) uses Zenoh — see [Zenoh Key Expressions](#zenoh-key-expressions).

### Browser → Dashboard (WebRTC video via MediaMTX WHEP)

The browser connects to `http://SERVER_IP:8889/<node_id>/whep` using the native WHEP (WebRTC-HTTP Egress Protocol). MediaMTX handles the SDP offer/answer and ICE negotiation. The Server does **not** relay video — it only pushes health/violation data over WebSocket.

The dashboard discovers active streams from **both**:
- MediaMTX API (`/api/streams`) — lists all RTSP sessions currently active
- Edge health data (`pipeline.active_cameras`) — bridges the gap before FPS stats are available

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
