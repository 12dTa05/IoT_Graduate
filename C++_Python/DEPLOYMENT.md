# Quick Start Deployment Guide

## Prerequisites Checklist

### Hardware
- [ ] 4x NVIDIA Jetson devices (Nano/Orin)
- [ ] 4x USB/IP cameras
- [ ] 1x Server for center node (can be x86 or Jetson)
- [ ] Network switch (Gigabit recommended)
- [ ] Power supplies

### Software (Edge Nodes)
- [ ] JetPack 5.1+ installed
- [ ] CUDA 11.4+
- [ ] GStreamer 1.0
- [ ] DeepStream SDK 6.3+
- [ ] Python 3.8+
- [ ] Node.js 18+ (for dashboard)

### Software (Center Server)
- [ ] Python 3.8+
- [ ] Node.js 18+
- [ ] Docker & Docker Compose

---

## Step 1: Deploy MQTT Broker (Center)

```bash
# On center server
cd C++_Python

# Start Mosquitto
docker-compose up -d

# Verify
docker ps
mosquitto_sub -h localhost -t 'edge/#' -v
```

---

## Step 2: Deploy Center Server

### Backend

```bash
cd center

# Install Python dependencies
pip3 install -r requirements.txt

# Run center server
python3 main_center.py --mqtt-broker localhost
```

### Frontend

```bash
cd center/frontend

# Install dependencies
npm install

# Development mode
npm run dev

# Production build
npm run build
npm run preview
```

**Access**: http://center-ip:5173

---

## Step 3: Deploy Edge Nodes

### For Each Edge (edge_1, edge_2, edge_3, edge_4)

#### 1. Build C++ Components

```bash
cd edge/cpp

# Install dependencies
sudo apt install \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-plugins-bad \
    libopencv-dev \
    libjsoncpp-dev

# Build
chmod +x build.sh
./build.sh

# Verify
ls -lh ../python/core/libdeepstream_wrapper.so
```

#### 2. Convert Models to TensorRT

```bash
cd edge/models

# YOLO (example with YOLOv8)
yolo export model=yolo11n.pt format=onnx

trtexec \
    --onnx=yolo11n.onnx \
    --saveEngine=yolo11n.engine \
    --fp16 \
    --workspace=4096

# LPRNet (if you have the model)
# Follow models/README.md for detailed instructions
```

#### 3. Calibrate Homography Matrix

```bash
cd edge/configs

# Edit homography.yml with your camera calibration points
# Use a tool or manually mark 4 points on road with known distances
nano homography.yml
```

#### 4. Install Python Dependencies

```bash
cd edge

# Install
pip3 install -r requirements.txt

# Verify jtop (Jetson stats)
sudo jtop
```

#### 5. Run Edge Node

```bash
# Set max performance
sudo nvpmodel -m 0
sudo jetson_clocks

# Run edge node
python3 main_edge.py \
    --edge-id edge_1 \
    --mqtt-broker <center-ip> \
    --source rtsp://camera-ip/stream \
    --port 8000
```

#### 6. Deploy Edge Dashboard

```bash
cd edge/frontend

# Install
npm install

# Development
npm run dev

# Production
npm run build
npm run preview
```

**Access**: http://edge-ip:5173

---

## Step 4: Verify System

### Check MQTT Communication

```bash
# On center server
mosquitto_sub -h localhost -t 'edge/#' -v

# You should see:
# edge/edge_1/metrics {...}
# edge/edge_1/alerts {...}
```

### Check Edge API

```bash
# Check metrics
curl http://edge-1-ip:8000/api/metrics

# Check status
curl http://edge-1-ip:8000/api/status
```

### Check Center API

```bash
# List edges
curl http://center-ip:8080/api/edges

# Balancer status
curl http://center-ip:8080/api/balancer/status
```

### Check Dashboards

1. **Edge Dashboard** (http://edge-ip:5173)
   - Should show video stream
   - Bounding boxes with speed/plate
   - System metrics

2. **Center Dashboard** (http://center-ip:5173)
   - Should show 2x2 grid with 4 cameras
   - Edge metrics
   - Load balancing controls

---

## Step 5: Optional - Zenoh P2P

### Install Zenoh Router (Center)

```bash
# Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Install zenohd
cargo install zenohd

# Run router
zenohd
```

### Install Zenoh C Library (Edges)

```bash
# Clone and build
git clone https://github.com/eclipse-zenoh/zenoh-c.git
cd zenoh-c
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
sudo make install

# Rebuild C++ with Zenoh support
cd ../../edge/cpp
./build.sh
```

---

## Troubleshooting

### Edge Node Won't Start

1. Check DeepStream installation:
   ```bash
   gst-inspect-1.0 nvinfer
   ```

2. Check model files exist:
   ```bash
   ls -lh models/*.engine
   ```

3. Check camera stream:
   ```bash
   gst-launch-1.0 uridecodebin uri=rtsp://camera/stream ! autovideosink
   ```

### No Video in Dashboard

1. Check WebRTC signaling:
   - Open browser console
   - Look for WebSocket connection errors

2. Check firewall:
   ```bash
   sudo ufw allow 8000/tcp
   sudo ufw allow 5173/tcp
   ```

3. Check STUN server reachable:
   ```bash
   ping stun.l.google.com
   ```

### MQTT Not Working

1. Check Mosquitto running:
   ```bash
   docker ps | grep mosquitto
   ```

2. Check firewall:
   ```bash
   sudo ufw allow 1883/tcp
   sudo ufw allow 9001/tcp
   ```

3. Test connection:
   ```bash
   mosquitto_pub -h center-ip -t test -m "hello"
   mosquitto_sub -h center-ip -t test
   ```

---

## Performance Tuning

See [OPTIMIZATION.md](OPTIMIZATION.md) for detailed tuning guide.

**Quick wins**:
```bash
# Max performance mode
sudo nvpmodel -m 0
sudo jetson_clocks

# Increase network buffers
sudo sysctl -w net.core.rmem_max=134217728
sudo sysctl -w net.core.wmem_max=134217728
```

---

## Production Deployment

### Create Systemd Services

**Edge Node** (`/etc/systemd/system/edge-node.service`):
```ini
[Unit]
Description=Edge Node
After=network.target

[Service]
Type=simple
User=nvidia
WorkingDirectory=/home/nvidia/IoT_Graduate/C++_Python/edge
ExecStart=/usr/bin/python3 main_edge.py --edge-id edge_1 --mqtt-broker center-ip
Restart=always

[Install]
WantedBy=multi-user.target
```

**Enable and start**:
```bash
sudo systemctl enable edge-node
sudo systemctl start edge-node
sudo systemctl status edge-node
```

---

## Summary

✅ **Deployment Steps**:
1. MQTT Broker (Center)
2. Center Backend + Frontend
3. Edge C++ Build
4. Edge Model Conversion
5. Edge Python Backend
6. Edge Frontend
7. Verification

⏱️ **Estimated Time**: 2-4 hours per edge node (first time)

📊 **Expected Performance**:
- Latency: 50-100ms (WebRTC)
- FPS: 30-40 (per edge)
- Accuracy: >85% (LPR), >90% (detection)
