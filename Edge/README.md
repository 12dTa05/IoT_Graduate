# Edge Node – AI Processing Unit for IoT_Graduate (P2P Mode)

## What the Edge Node Does
- **Vehicle detection** (YOLO11s)
- **Speed measurement** using homography (real‑world metres → km/h)
- **License plate detection & recognition** (Vietnamese plates)
- **Overspeed alerts** (snapshots + MQTT notifications)
- **Multi‑output**: HDMI display, MP4 file, WebRTC stream
- **P2P load balancing**: negotiates camera migration with peer Jetsons via Pareto ε-Constraint voting over MQTT

## Architecture (Decentralized — No Master)

Each Jetson is a symmetric **Peer Node**:
- Runs its own DeepStream pipeline + Health Agent + Peer Orchestrator
- Communicates over LAN MQTT: `peers/status/+`, `peers/vote/*`, `peers/control/{node_id}`
- Discovers peers via static config (`edge_node.yml`) + optional mDNS
- Leaderless failover via deterministic consistent hash

## Setup

```bash
cd ~/IoT_Graduate/Edge
chmod +x setup_system.sh
./setup_system.sh
pip3 install -r requirements.txt
```

## Configuration

Edit `configs/edge_node.yml` — set your `node_id`, MQTT broker IP, and static peer list.

## Running

```bash
python3 main.py --backend python --source rtsp://... --mode display
```

For WebRTC:
```bash
python3 main.py --backend python --source rtsp://... --mode webrtc
# Open grid_monitor/index.html in your browser
```
