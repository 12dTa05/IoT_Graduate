# IoT_Graduate — P2P Multi‑Edge Traffic Monitoring System

A distributed real‑time traffic monitoring system that runs **AI (NVIDIA DeepStream)** on multiple Jetson Edge nodes to measure vehicle speed, detect vehicles, and read license plates. The architecture is **fully decentralized P2P** — there is no central Master or broker. Every Edge node runs its own **PeerOrchestrator** that coordinates with peers over **Eclipse Zenoh** (peer mode, UDP multicast scouting).

## Key Innovation: P2P Load Balancing

Each Jetson runs an independent **PeerOrchestrator** instance that communicates over Zenoh (peer mode):

- **ε‑constraint Pareto optimization** — when a node is overloaded (low FPS, high GPU), it publishes a **Request For Offload (RFO)**. Peers respond with bids, and the winner is the bid with the lowest composite score `F(x)` that satisfies all tiered constraints (FPS floor, network latency ceiling, stream capacity).
- **Make‑before‑Break migration** — the winning peer adds the camera to its pipeline first, waits for the stream to reach `PLAYING` state, and *then* signals the overloaded peer to remove it. Zero frame loss.
- **Consistent‑hashing failover** — camera-to-node assignments are deterministic (hash ring). When a peer goes offline, its cameras redistribute in `O(1)` per camera.

## Components

| Component | Description |
|---|---|
| **Camera** (`Camera/`) | Docker‑based RTSP camera simulator using MediaMTX. Loops video files as RTSP streams. |
| **Edge** (`Edge/`) | Jetson AI processing node. Runs DeepStream pipelines for speed, LPR, and overspeed alerts. Each node has an embedded **PeerOrchestrator**, **signaling server** (WebRTC), and **health agent**. |

### Edge Node Internals

| Module | Role |
|---|---|
| `main.py` | Entry point — python‑only, no `--backend` flag |
| `speedflow_python/settings.py` | Loads all config from `Edge/.env` (single source of truth) |
| `speedflow_python/run_python.py` | Pipeline runner — wires Zenoh, PeerOrchestrator, signaling, probes |
| `speedflow_python/peer_orchestrator.py` | P2P brain — ε‑constraint voting, Make‑before‑Break migration, failover |
| `speedflow_python/peer_discovery.py` | No-op shim (Zenoh scouting handles peer discovery) |
| `speedflow_python/signaling.py` | Embedded WebRTC signaling server (one per Jetson) |
| `speedflow_python/grid_monitor/index.html` | Browser‑based grid monitor; connects to any signaling server |
| `speedflow_python/probes.py` | GStreamer pad probes — ROI filter, speed calc, FPS stats |
| `speedflow_python/zenoh_publisher.py` | Publishes speed events and overspeed alerts via Zenoh |
| `speedflow_python/zenoh_subscriber.py` | Handles `peers/control/{node_id}` commands (ADD/REMOVE stream) |
| `speedflow_python/zenoh_session.py` | Zenoh session factory (peer mode config) |
| `health_agent.py` | Jetson metrics collector (GPU, CPU, temp, FPS) → `peers/status/{node_id}` |

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
│  Signaling    │  │  Signaling    │  │  Signaling    │
│  Pipeline     │  │  Pipeline     │  │  Pipeline     │
│  Grid Monitor │  │  Grid Monitor │  │  Grid Monitor │
└──────────────┘  └──────────────┘  └──────────────┘
        ▲                                   ▲
        │ RTSP                              │ RTSP
        ▼                                   ▼
┌──────────────────────────────────────────────────────────────┐
│                 Camera Node (Docker / MediaMTX)              │
│      rtsp://camera_ip:8554/cam_01 ... rtsp://.../cam_0N     │
└──────────────────────────────────────────────────────────────┘
```

## Prerequisites

- **Camera node**: Docker & Docker Compose
- **Edge nodes**: NVIDIA Jetson (Orin/NX/Nano) with JetPack 6.x and DeepStream SDK 7.x
- **Python**: 3.8+ on all nodes

## Quick Start

### 1. Camera Node — RTSP simulator

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

### 2. Edge Node — Configure `.env`

```bash
cd Edge
cp .env .env.bak       # keep the original
```

Edit `Edge/.env` to match your deployment:

```ini
NODE_ID=jetson_A
SIGNALING_PORT=8080
MAX_STREAMS=4
```

All config lives in this single file — paths, thresholds, everything.

### 3. Edge Node — Launch

```bash
cd Edge
./setup_system.sh             # system deps (first time only)
pip3 install -r requirements.txt

# Display mode (HDMI out):
python3 main.py --source rtsp://<CAMERA_IP>:8554/cam_01 --mode display

# File mode (save to MP4):
python3 main.py --source rtsp://<CAMERA_IP>:8554/cam_01 --mode file --output result.mp4

# Full P2P mode with orchestration + signaling:
python3 -m speedflow_python.run_python
```

### 4. Grid Monitor — Browser

Open [http://<JETSON_IP>:8080](http://<JETSON_IP>:8080) in any browser. The grid monitor connects directly to the Jetson's embedded signaling server and displays all active streams.

## Zenoh Key Expressions

| Key Expression | Publisher | Subscribers | Payload |
|---|---|---|---|
| `peers/status/{node_id}` | `health_agent.py` | `peer_orchestrator.py` | msgpack — GPU%, CPU%, FPS, temp, load_score |
| `peers/vote/request` | `peer_orchestrator.py` | `peer_orchestrator.py` (all nodes) | msgpack — RFO: camera, load, ε constraints |
| `peers/vote/proposal` | `peer_orchestrator.py` | `peer_orchestrator.py` (requester) | msgpack — bid: predicted FPS, capacity |
| `peers/vote/decision` | `peer_orchestrator.py` | `peer_orchestrator.py` (winner + requester) | msgpack — winner node_id, cam_config |
| `peers/vote/ack/{cam_id}` | `zenoh_subscriber.py` | `peer_orchestrator.py` (requester) | msgpack — stream is PLAYING |
| `peers/control/{node_id}` | `peer_orchestrator.py` | `zenoh_subscriber.py` | msgpack — ADD / REMOVE / STATUS commands |
| `traffic/events/{node_id}/{cam_id}` | `zenoh_publisher.py` | *(any consumer)* | msgpack — speed event per vehicle |

## P2P Load Balancing Details

The PeerOrchestrator implements a **Pareto ε‑constraint** voting protocol:

1. **Monitoring** — health agent publishes `load_score` to `peers/status/{node_id}` every 2s.
2. **RFO trigger** — if `load_score > overload_threshold` for `overload_duration_s` seconds, the node selects its worst camera and publishes an RFO to `peers/vote/request` with tiered constraints.
3. **Proposal window** — peers with available capacity bid via `peers/vote/proposal`. Window closes after `vote_window_s` seconds.
4. **Winner selection** — proposals are filtered through ε‑constraint tiers (strict FPS floor → tier 1 → tier 2 → network latency). From the surviving set, the proposal with lowest composite score wins.
5. **Make‑before‑Break** — winner receives a ADD command on `peers/control/{node_id}`, starts the stream, and publishes `peers/vote/ack/{cam}`. The RFO sender waits for this ack before removing the camera.
6. **Failover** — if a peer's heartbeat stops for `heartbeat_timeout_s`, each surviving peer runs consistent‑hash on that peer's camera list and rescues orphaned streams.
7. **Cooldown** — per‑camera cooldown prevents thrashing.

All parameters are configurable in `Edge/configs/edge_node.yml` (the FPS prediction model should be calibrated offline per Jetson model).

## Project Structure

```
IoT_Graduate/
├── Camera/                         # RTSP camera simulator
│   ├── .env                        # Compose variables
│   ├── docker-compose.yml          # MediaMTX + cam containers
│   ├── Dockerfile                  # ffmpeg loop sender
│   ├── generate-compose.sh         # generate N-camera compose
│   └── videos/                     # .mp4 loop files
├── Edge/                           # AI processing node
│   ├── .env                        # Single source of truth (all config)
│   ├── .gitignore
│   ├── main.py                     # Entry point
│   ├── health_agent.py             # Hardware metrics → Zenoh
│   ├── speed_gui.py                # PyQt5 calibration GUI
│   ├── requirements.txt
│   ├── configs/                    # DeepStream configs
│   │   ├── cameras.yml             # RTSP source definitions
│   │   ├── config_infer_primary_yolo11.txt
│   │   ├── config_infer_secondary_lpd.txt
│   │   ├── config_infer_secondary_lpr.txt
│   │   ├── config_nvdsanalytics.txt
│   │   ├── config_tracker_*.yml
│   │   ├── edge_node.yml           # P2P parameters
│   │   ├── labels_*.txt
│   │   └── points_*.yml            # Homography calibration points
│   └── speedflow_python/
│       ├── settings.py             # .env loader
│       ├── core_pipeline.py        # GStreamer pipeline builder
│       ├── run_python.py           # Full pipeline runner
│       ├── peer_orchestrator.py    # P2P load balancing
│       ├── peer_discovery.py       # No-op shim (Zenoh scouting)
│       ├── signaling.py            # WebRTC signaling server
│       ├── grid_monitor/           # Browser viewer
│       │   └── index.html
│       ├── probes.py               # GStreamer pad probes
│       ├── zenoh_publisher.py      # Speed/event publisher
│       ├── zenoh_subscriber.py     # Control command subscriber
│       ├── zenoh_session.py        # Zenoh peer-mode factory
│       ├── webrtc_client.py        # WebRTC client for pipeline
│       ├── camera_config.py        # Camera config management
│       ├── common.py               # Shared utilities
│       └── ...                     # analytics, homography, draw, etc.
└── README.md
```
