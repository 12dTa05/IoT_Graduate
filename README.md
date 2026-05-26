# IoT_Graduate — P2P Multi‑Edge Traffic Monitoring System

A distributed real‑time traffic monitoring system that runs **AI (NVIDIA DeepStream)** on multiple Jetson Edge nodes to measure vehicle speed, detect vehicles, and read license plates. The architecture is **fully decentralized P2P** — there is no central Master. Every Edge node runs its own **PeerOrchestrator** that coordinates with peers over MQTT.

## Key Innovation: P2P Load Balancing

Each Jetson runs an independent **PeerOrchestrator** instance that communicates over a shared MQTT bus:

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
| `speedflow_python/run_python.py` | Pipeline runner — wires MQTT, PeerOrchestrator, signaling, probes |
| `speedflow_python/peer_orchestrator.py` | P2P brain — ε‑constraint voting, Make‑before‑Break migration, failover |
| `speedflow_python/peer_discovery.py` | Static registry + optional mDNS peer discovery |
| `speedflow_python/signaling.py` | Embedded WebRTC signaling server (one per Jetson) |
| `speedflow_python/grid_monitor/index.html` | Browser‑based grid monitor; connects to any signaling server |
| `speedflow_python/probes.py` | GStreamer pad probes — ROI filter, speed calc, FPS stats |
| `speedflow_python/mqtt_publisher.py` | Publishes speed events and overspeed alerts to MQTT |
| `speedflow_python/mqtt_subscriber.py` | Handles `peers/control/{node_id}` commands (ADD/REMOVE stream) |
| `health_agent.py` | Jetson metrics collector (GPU, CPU, temp, FPS) → `peers/status/{node_id}` |

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      MQTT Broker                             │
│  (runs on any Jetson or separate machine, e.g. 192.168.1.100) │
└──────────────────────────────────────────────────────────────┘
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
- **MQTT Broker**: Mosquitto (runs on any Jetson or separate Ubuntu machine)
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

### 2. MQTT Broker (one machine)

```bash
sudo apt update && sudo apt install mosquitto -y
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
```

The broker IP goes into every Edge node's `.env` as `MQTT_BROKER_HOST`.

### 3. Edge Node — Configure `.env`

```bash
cd Edge
cp .env .env.bak       # keep the original
```

Edit `Edge/.env` to match your deployment:

```ini
NODE_ID=jetson_A
MQTT_BROKER_HOST=192.168.1.100
MQTT_BROKER_PORT=1883
SIGNALING_PORT=8080
MAX_STREAMS=4
```

All config lives in this single file — paths, thresholds, FPS model, everything.

### 4. Edge Node — Launch

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

### 5. Grid Monitor — Browser

Open [http://<JETSON_IP>:8080](http://<JETSON_IP>:8080) in any browser. The grid monitor connects directly to the Jetson's embedded signaling server and displays all active streams.

## MQTT Topics

| Topic | Direction | Payload |
|---|---|---|
| `peers/status/{node_id}` | Peer → MQTT | JSON — GPU%, CPU%, FPS, temp, load_score |
| `peers/vote/request` | Peer → MQTT | JSON — RFO: candidate cameras, load, constraints |
| `peers/vote/proposal` | Peer → MQTT | JSON — bid: predicted FPS, available capacity |
| `peers/vote/decision` | Peer → MQTT | JSON — winner node_id, camera assignment |
| `peers/vote/ack/{cam_id}` | Peer → MQTT | JSON — stream is PLAYING |
| `peers/control/{node_id}` | MQTT → Peer | `ADD <cam_id> <rtsp_url>` / `REMOVE <cam_id>` |
| `peers/event/speed` | Peer → MQTT | JSON — speed event per vehicle |
| `peers/event/overspeed` | Peer → MQTT | JSON — overspeed alert + snapshot path |

## P2P Load Balancing Details

The PeerOrchestrator implements a **Pareto ε‑constraint** voting protocol:

1. **Monitoring** — health agent publishes `load_score = w₁·GPU% + w₂·(1−FPS/TARGET) + w₃·temp%` every 2s.
2. **RFO trigger** — if `load_score > overload_threshold` for `overload_duration_s` seconds, the node selects its worst camera (highest processing cost) and publishes an RFO with tiered constraints.
3. **Proposal window** — peers with available capacity publish proposals (predicted FPS after accepting the camera). Window closes after `vote_window_s` seconds.
4. **Winner selection** — proposals are filtered through ε‑constraint tiers (strict FPS floor → tier 1 → tier 2 → network latency). From the surviving set, the proposal with lowest composite score `F(x) = α·predicted_fps + β·network_rtt` wins.
5. **Make‑before‑Break** — winner receives a `peers/control` ADD command, starts the stream, and publishes `peers/vote/ack/{cam}`. The RFO sender waits for this ack before removing the camera from its own pipeline.
6. **Failover** — if a peer's heartbeat (status topic) stops for `heartbeat_timeout_s`, each surviving peer runs consistent‑hash on that peer's camera list. The first hash‑match peer adds the orphaned camera (with jitter).
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
│   ├── health_agent.py             # Hardware metrics → MQTT
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
│       ├── peer_discovery.py       # Static + mDNS discovery
│       ├── signaling.py            # WebRTC signaling server
│       ├── grid_monitor/           # Browser viewer
│       │   └── index.html
│       ├── probes.py               # GStreamer pad probes
│       ├── mqtt_publisher.py       # Speed/event publisher
│       ├── mqtt_subscriber.py      # Control command subscriber
│       ├── webrtc_client.py        # WebRTC client for pipeline
│       ├── camera_config.py        # Camera config management
│       ├── common.py               # Shared utilities
│       └── ...                     # analytics, homography, draw, etc.
└── README.md
```
