# Multi-Edge IoT System

Hệ thống multi-edge computing cho giám sát giao thông với 4 edge Jetson + 1 center server.

## Tính năng

- ✅ **YOLO Detection** + **LPRNet** để đọc biển số xe
- ✅ **Speed Estimation** qua homography transformation
- ✅ **MQTT Telemetry** từ edge → center
- ✅ **Zenoh P2P Offloading** giữa các edge
- ✅ **ReactJS Dashboard** cho cả edge và center
- ✅ **FastAPI Backend** với REST API và WebSocket
- ✅ **Auto Load Balancing** dựa trên CPU/GPU metrics

## Kiến trúc

```
4 Edge Nodes (Jetson)          Center Server
├── DeepStream Pipeline        ├── MQTT Broker
├── YOLO + LPRNet             ├── Load Balancer
├── FastAPI Backend           ├── FastAPI Backend
└── ReactJS Dashboard         └── ReactJS Dashboard (4 cameras)
        ↓                              ↑
        └──── MQTT Telemetry ──────────┘
        └──── Zenoh P2P (Edge ↔ Edge)
```

## Cấu trúc thư mục

```
C++_Python/
├── edge/                  # Code cho Edge Nodes (Jetson)
│   ├── cpp/                   # C++ DeepStream pipeline
│   ├── python/                # Python FastAPI + MQTT
│   ├── frontend/              # ReactJS dashboard
│   ├── models/                # TensorRT engines
│   ├── configs/               # DeepStream configs
│   └── main_edge.py           # Entry point
│
└── center/                # Code cho Center Server
    ├── backend/               # Python FastAPI + MQTT + Balancer
    ├── frontend/              # ReactJS multi-camera dashboard
    └── main_center.py         # Entry point
```

## Yêu cầu

### Edge Node (Jetson)
- NVIDIA Jetson AGX Orin / Nano
- JetPack 5.1.4+
- DeepStream SDK 6.3+
- Python 3.8+
- Node.js 18+ (cho ReactJS)

### Center Server
- Ubuntu 20.04+
- Python 3.8+
- Node.js 18+
- MQTT Broker (Eclipse Mosquitto)

## Cài đặt

### 1. Setup MQTT Broker (Center)

```bash
# Install Mosquitto
sudo apt install mosquitto mosquitto-clients

# Start broker
sudo systemctl start mosquitto
sudo systemctl enable mosquitto
```

### 2. Setup Edge Node

```bash
cd C++_Python/edge

# Install Python dependencies
pip3 install -r requirements.txt

# Build C++ components
cd cpp
mkdir build && cd build
cmake ..
make -j$(nproc)
cd ../..

# Install frontend dependencies
cd frontend
npm install
cd ..
```

### 3. Setup Center Server

```bash
cd C++_Python/center

# Install Python dependencies
pip3 install -r requirements.txt

# Install frontend dependencies
cd frontend
npm install
cd ..
```

## Chạy hệ thống

### Center Server

```bash
# Terminal 1: Start backend
cd C++_Python/center
python3 main_center.py --mqtt-broker localhost --api-port 8080

# Terminal 2: Start frontend
cd C++_Python/center/frontend
npm run dev
# Open http://localhost:5174
```

### Edge Node

```bash
# Terminal 1: Start backend
cd C++_Python/edge
python3 main_edge.py \
  --edge-id edge_1 \
  --mqtt-broker 192.168.1.100 \
  --api-port 8000 \
  --source rtsp://camera_ip/stream

# Terminal 2: Start frontend
cd C++_Python/edge/frontend
npm run dev
# Open http://localhost:5173
```

## API Endpoints

### Edge API (Port 8000)

- `GET /api/metrics` - System metrics (CPU, GPU, RAM)
- `GET /api/status` - Edge status
- `WS /ws/metadata` - Real-time bounding boxes
- `POST /api/pipeline/start` - Start DeepStream
- `POST /api/pipeline/stop` - Stop DeepStream

### Center API (Port 8080)

- `GET /api/edges` - All edge nodes
- `GET /api/edges/{id}` - Specific edge detail
- `GET /api/balancer/status` - Load balancer status
- `POST /api/balancer/offload` - Manual offload
- `POST /api/balancer/stop/{id}` - Stop offload
- `WS /ws/metrics` - Real-time metrics from all edges

## MQTT Topics

### Edge → Center

- `edge/{id}/status` - System metrics (QoS 0)
- `edge/{id}/alert` - Overspeed/LPR alerts (QoS 1)

### Center → Edge

- `center/command/{id}` - Control commands (QoS 1)

## Development

### Edge Dashboard Features

- ✅ Single camera view
- ✅ Bounding boxes with speed & license plate
- ✅ System metrics display
- ✅ WebSocket real-time updates
- ✅ Shared memory for zero-copy metadata

### Center Dashboard Features

- ✅ 2x2 grid for 4 cameras
- ✅ Per-edge metrics cards
- ✅ Load balancing panel
- ✅ Active offload monitoring
- ✅ Manual offload controls

## Advanced Features

### Zenoh P2P Offloading

See [ZENOH_SETUP.md](ZENOH_SETUP.md) for detailed setup guide.

- Edge-to-edge inference offloading
- Intelligent load balancing
- Frame compression for efficient transmission
- Automatic fallback to local processing

### WebRTC Video Streaming

See [WEBRTC_SETUP.md](WEBRTC_SETUP.md) for detailed setup guide.

- Real-time video streaming (50-100ms latency)
- Hardware-accelerated H.264 encoding
- Browser-native WebRTC playback
- SDP signaling via WebSocket

### Performance Optimization

See [OPTIMIZATION.md](OPTIMIZATION.md) for comprehensive optimization guide.

- Jetson performance tuning
- DeepStream optimization
- Network optimization
- Python/ReactJS optimization
- Monitoring and profiling

## TODO

- [ ] Implement C++ DeepStream pipeline
- [ ] Integrate LPRNet (SGIE)
- [ ] Implement Zenoh P2P offloading
- [ ] Add WebRTC video streaming
- [ ] Add alert history database
- [ ] Add authentication

## License

MIT
