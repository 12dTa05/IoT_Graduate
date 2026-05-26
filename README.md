# IoT_Graduate — P2P Multi‑Edge Traffic Monitoring System

A distributed real‑time traffic monitoring system that runs **AI (NVIDIA DeepStream)** on multiple Jetson Edge nodes to measure vehicle speed, detect vehicles, and read license plates. The architecture is **fully decentralized P2P** — there is no central Master. Every Edge node runs its own **PeerOrchestrator** that coordinates with peers over MQTT.

## Key Innovations

Each Jetson runs an independent **PeerOrchestrator** instance that communicates over a shared MQTT bus:

- **ε‑constraint Pareto optimization** — when a node is overloaded (low FPS, high GPU), it publishes a **Request For Offload (RFO)**. Peers respond with bids, and the winner is the bid with the lowest composite score `F(x)` that satisfies all tiered constraints (FPS floor, network latency ceiling, stream capacity).
- **Make‑before‑Break migration** — the winning peer adds the camera to its pipeline first, waits for the stream to reach `PLAYING` state, and *then* signals the overloaded peer to remove it. Zero frame loss.
- **Consistent‑hashing failover** — camera-to-node assignments are deterministic (hash ring). When a peer goes offline, its cameras redistribute in `O(1)` per camera.
- **Embedded MQTT broker with automatic failover** — no separate broker machine required. One Edge node runs Mosquitto as a subprocess. When it dies, `BrokerWatcher` walks a priority list and promotes the next live node; all clients reconnect transparently.

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
| `speedflow_python/run_python.py` | Pipeline runner — wires MQTT, BrokerManager, PeerOrchestrator, signaling, probes |
| `speedflow_python/broker_manager.py` | Starts/stops Mosquitto subprocess; monitors and auto-restarts it |
| `speedflow_python/peer_orchestrator.py` | P2P brain — ε‑constraint voting, Make‑before‑Break, failover, BrokerWatcher |
| `speedflow_python/peer_discovery.py` | Static registry + optional mDNS peer discovery |
| `speedflow_python/signaling.py` | Embedded WebRTC signaling server (one per Jetson) |
| `speedflow_python/grid_monitor/index.html` | Browser‑based grid monitor; connects to any signaling server |
| `speedflow_python/probes.py` | GStreamer pad probes — ROI filter, speed calc, FPS stats |
| `speedflow_python/mqtt_publisher.py` | Publishes speed events and overspeed alerts to MQTT |
| `speedflow_python/mqtt_subscriber.py` | Handles `peers/control/{node_id}` commands (ADD/REMOVE stream); supports broker reconnect |
| `health_agent.py` | Jetson metrics collector → `peers/status/{node_id}`; adds broker penalty to load score |

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Jetson A  [BROKER]                                              │
│  Mosquitto ← BrokerManager                                       │
│  PeerOrch + BrokerWatcher   ◄──── peers/status, vote, control   │
│  Signaling + Pipeline                                            │
└──────────────────────────────────────────────────────────────────┘
         ▲                    ▲                    ▲
         │ MQTT               │ MQTT               │ MQTT
         ▼                    ▼                    ▼
┌──────────────┐  ┌──────────────────┐  ┌──────────────┐
│  Jetson B     │  │  Jetson C         │  │  Jetson …     │
│  PeerOrch     │  │  PeerOrch         │  │  PeerOrch     │
│  BrokerWatch  │  │  BrokerWatch      │  │  BrokerWatch  │
│  Signaling    │  │  Signaling        │  │  Signaling    │
│  Pipeline     │  │  Pipeline         │  │  Pipeline     │
└──────────────┘  └──────────────────┘  └──────────────┘
        ▲                                       ▲
        │ RTSP                                  │ RTSP
        ▼                                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Camera Node (Docker / MediaMTX)                │
│       rtsp://camera_ip:8554/cam_01 … rtsp://.../cam_0N          │
└──────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- **Camera node**: Docker & Docker Compose
- **Edge nodes**: NVIDIA Jetson (Orin/NX/Nano) with JetPack 6.x and DeepStream SDK 7.x
- **Mosquitto**: installed on every Edge node — `sudo apt install mosquitto` (only one node runs it at a time; others are on standby)
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

### 2. Edge Node — Install Mosquitto (every node)

```bash
sudo apt install mosquitto -y
# Do NOT enable/start it as a system service — BrokerManager controls the process.
sudo systemctl disable mosquitto
sudo systemctl stop mosquitto
```

### 3. Edge Node — Configure `.env` and `edge_node.yml`

**`Edge/.env`** — set on **every** Jetson:

```ini
NODE_ID=jetson_A              # unique per node
MQTT_BROKER_HOST=192.168.1.10 # IP of the initial broker node (jetson_A)
MQTT_BROKER_PORT=1883
SIGNALING_PORT=8080
MAX_STREAMS=4

# Broker role — set true on exactly ONE node
BROKER_ENABLED=true
BROKER_PORT=1883
BROKER_PENALTY_SCORE=20.0     # load_score bonus added to the broker node
```

**`Edge/configs/edge_node.yml`** — **identical** on every node:

```yaml
broker:
  enabled: true           # matches BROKER_ENABLED in .env
  port: 1883
  probe_interval_s: 3.0
  dead_threshold_count: 3
  candidate_timeout_s: 15.0
  penalty_score: 20.0
  priority_order:         # failover chain — top = first promoted
    - jetson_A
    - jetson_B
    - jetson_C
```

> The `priority_order` list must be **identical on every node** and kept in sync manually.

### 4. Edge Node — Launch

```bash
cd Edge
./setup_system.sh             # system deps (first time only)
pip3 install -r requirements.txt

# Display mode (HDMI out):
python3 main.py --source rtsp://<CAMERA_IP>:8554/cam_01 --mode display

# File mode (save to MP4):
python3 main.py --source rtsp://<CAMERA_IP>:8554/cam_01 --mode file --output result.mp4

# Full P2P mode with orchestration + embedded broker + signaling:
python3 -m speedflow_python.run_python
```

The node with `BROKER_ENABLED=true` automatically starts Mosquitto. All other nodes connect to it. If it dies, `BrokerWatcher` promotes the next node in `priority_order` without any manual intervention.

### 5. Grid Monitor — Browser

Open `http://<JETSON_IP>:8080` in any browser. The grid monitor connects directly to the Jetson's embedded signaling server and displays all active streams.

## MQTT Topics

| Topic | Direction | Payload |
|---|---|---|
| `peers/status/{node_id}` | Peer → MQTT | JSON — GPU%, CPU%, FPS, temp, load_score, **is_broker**, **broker_host** |
| `peers/vote/request` | Peer → MQTT | JSON — RFO: candidate cameras, load, constraints |
| `peers/vote/proposal` | Peer → MQTT | JSON — bid: predicted FPS, available capacity |
| `peers/vote/decision` | Peer → MQTT | JSON — winner node_id, camera assignment |
| `peers/vote/ack/{cam_id}` | Peer → MQTT | JSON — stream is PLAYING |
| `peers/control/{node_id}` | MQTT → Peer | JSON — ADD / REMOVE camera command |
| `peers/event/speed` | Peer → MQTT | JSON — speed event per vehicle |
| `peers/event/overspeed` | Peer → MQTT | JSON — overspeed alert + snapshot path |

## P2P Load Balancing Details

The PeerOrchestrator implements a **Pareto ε‑constraint** voting protocol:

1. **Monitoring** — health agent publishes `load_score` every 2s:
   ```
   base    = 0.5·GPU% + 0.3·CPU% + 0.2·RAM%
   fps_pen = max(0, (TARGET_FPS − avg_fps) / TARGET_FPS) × 30
   broker  = BROKER_PENALTY_SCORE  (only on the broker node)
   score   = min(100, base + fps_pen + broker)
   ```
   The broker penalty keeps the broker node's score artificially high, deprioritizing it in all camera auctions.

2. **RFO trigger** — if `load_score > overload_threshold` for `overload_duration_s` seconds, the node selects its worst camera and publishes an RFO.
3. **Proposal window** — peers with available capacity publish proposals. The broker node is **always excluded** via an explicit `ε0` guard (`is_broker == true → skip bid`).
4. **Winner selection** — proposals are filtered through ε‑constraint tiers (strict FPS floor → tier 1 → tier 2 → network latency). Lowest `F(x)` wins.
5. **Make‑before‑Break** — winner adds the stream, publishes `peers/vote/ack/{cam}`, requester waits for ack before removing.
6. **Camera failover** — consistent‑hash redistributes orphaned cameras when a peer goes offline.
7. **Cooldown** — per‑camera cooldown prevents thrashing.

## Embedded Broker Failover Details

`BrokerWatcher` runs as a background thread inside every `PeerOrchestrator`:

1. **Probe** — TCP‑connects to `MQTT_BROKER_HOST:MQTT_BROKER_PORT` every `probe_interval_s`.
2. **Declare dead** — after `dead_threshold_count` consecutive failures (~9 seconds with defaults).
3. **Walk `priority_order`**:
   - Skip nodes whose heartbeat is older than `heartbeat_timeout_s` (already dead).
   - If **this node** is the next candidate → call `BrokerManager.start()` to spawn Mosquitto.
   - All nodes probe `candidate_ip:broker_port` for up to `candidate_timeout_s` seconds.
   - First to respond → call `on_broker_change(new_host, new_port)` on every MQTT client (PeerOrchestrator, MQTTCommandSubscriber, HealthAgent).
   - Clients disconnect; their retry loops reconnect to the new host automatically.
4. **If all candidates exhausted** → log `CRITICAL` and wait for manual intervention.

The `broker_host` field in every heartbeat payload lets BrokerWatcher on remote nodes know what IP to probe when a candidate becomes the new broker.

All parameters are configurable in `Edge/configs/edge_node.yml`. The FPS prediction model should be calibrated offline per Jetson hardware model.

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
│       ├── broker_manager.py       # Mosquitto subprocess lifecycle
│       ├── peer_orchestrator.py    # P2P load balancing + BrokerWatcher
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
