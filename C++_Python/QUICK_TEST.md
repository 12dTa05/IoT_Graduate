# Quick Test Guide - Standalone Edge Node

## 🎯 Mục đích

Test edge node độc lập **KHÔNG CẦN**:
- ❌ MQTT broker
- ❌ Zenoh router
- ❌ Center server
- ❌ Multiple edges

Chỉ cần:
- ✅ NVIDIA Jetson (hoặc GPU với CUDA)
- ✅ DeepStream SDK
- ✅ Docker với NVIDIA runtime
- ✅ Video file hoặc camera

---

## 🚀 Quick Start (3 bước)

### Bước 1: Chuẩn bị video test

```bash
# Tạo thư mục videos
mkdir -p videos

# Download sample video (hoặc copy video của bạn)
wget https://example.com/sample-traffic.mp4 -O videos/sample.mp4

# Hoặc copy video có sẵn
cp /path/to/your/video.mp4 videos/sample.mp4
```

### Bước 2: Build Docker image

```bash
# Từ thư mục C++_Python
chmod +x run_edge_standalone.sh
./run_edge_standalone.sh
```

### Bước 3: Run container

```bash
# Option 1: Với video file
docker run --runtime nvidia --rm -it \
  -p 8000:8000 \
  -v $(pwd)/videos:/app/videos \
  -v $(pwd)/edge/models:/app/edge/models \
  edge-node-standalone \
  python3 main_edge_standalone.py --source file:///app/videos/sample.mp4

# Option 2: Với test pattern (không cần video)
docker run --runtime nvidia --rm -it \
  -p 8000:8000 \
  edge-node-standalone \
  python3 main_edge_standalone.py --source videotestsrc
```

---

## 🐳 Hoặc dùng Docker Compose (Dễ hơn)

```bash
# Edit docker-compose.edge.yml để set video source
nano docker-compose.edge.yml

# Run
docker-compose -f docker-compose.edge.yml up

# Stop
docker-compose -f docker-compose.edge.yml down
```

---

## 📊 Kiểm tra kết quả

### 1. Xem logs

```bash
# Trong terminal sẽ thấy:
# ✓ Vehicle: ID=1, Speed=45.2 km/h, Plate=29A-12345
# ⚠️  OVERSPEED: ID=2, Speed=75.3 km/h, Plate=30B-67890
# 📊 Stats: Frames=100, Objects=15, Overspeed=3, FPS=28.5
```

### 2. Check API

```bash
# Status
curl http://localhost:8000/api/status

# Response:
# {
#   "mqtt_connected": false,
#   "monitor_running": false,
#   "pipeline_running": true,
#   "metadata_websockets": 0,
#   "webrtc_clients": 0
# }
```

### 3. WebSocket metadata

```bash
# Install wscat
npm install -g wscat

# Connect to metadata stream
wscat -c ws://localhost:8000/ws/metadata

# Sẽ nhận được JSON metadata mỗi frame:
# [
#   {
#     "track_id": 1,
#     "x": 100,
#     "y": 200,
#     "width": 150,
#     "height": 200,
#     "speed": 45.2,
#     "plate": "29A-12345",
#     "class": "car"
#   }
# ]
```

### 4. Test với Python client

```python
import asyncio
import websockets
import json

async def test_metadata():
    uri = "ws://localhost:8000/ws/metadata"
    async with websockets.connect(uri) as websocket:
        for i in range(10):
            data = await websocket.recv()
            objects = json.loads(data)
            print(f"Frame {i}: {len(objects)} objects detected")
            for obj in objects:
                print(f"  - ID={obj['track_id']}, Speed={obj.get('speed', 0):.1f} km/h")

asyncio.run(test_metadata())
```

---

## 🎥 Video Sources

### File video

```bash
--source file:///app/videos/sample.mp4
```

### RTSP camera

```bash
--source rtsp://admin:password@192.168.1.100:554/stream
```

### USB camera

```bash
# Mount device
docker run --runtime nvidia --rm -it \
  -p 8000:8000 \
  --device /dev/video0 \
  edge-node-standalone \
  python3 main_edge_standalone.py --source v4l2:///dev/video0
```

### Test pattern (không cần camera)

```bash
--source videotestsrc
```

---

## 🔧 Troubleshooting

### "Failed to build pipeline"

**Kiểm tra models:**
```bash
# Models phải có trong edge/models/
ls -lh edge/models/
# yolo11n.engine
# lprnet.engine
```

**Nếu chưa có, convert models:**
```bash
# Xem hướng dẫn trong edge/models/README.md
cd edge/models
# Convert YOLO
trtexec --onnx=yolo11n.onnx --saveEngine=yolo11n.engine --fp16
```

### "CUDA out of memory"

**Giảm resolution hoặc batch size:**
```bash
# Edit configs/dstest_yolo.txt
nano edge/configs/dstest_yolo.txt

# Thay đổi:
# batch-size=1  (đã là 1, OK)
# Hoặc giảm resolution trong streammux
```

### "No video output"

**Kiểm tra video file:**
```bash
# Test với gst-launch
docker exec -it edge-node-test bash
gst-launch-1.0 filesrc location=/app/videos/sample.mp4 ! decodebin ! autovideosink
```

### "Pipeline FPS too low"

**Check GPU usage:**
```bash
# Trên Jetson
sudo jtop

# Nếu GPU < 70%, tăng performance
sudo nvpmodel -m 0
sudo jetson_clocks
```

---

## 📈 Expected Output

### Console logs:

```
==========================================================
🚀 STANDALONE EDGE NODE - TEST MODE
==========================================================
📝 Configuration:
   Source: file:///app/videos/sample.mp4
   API Port: 8000
==========================================================

2025-01-21 15:30:00 - INFO - Initializing standalone edge node
2025-01-21 15:30:01 - INFO - Shared memory initialized
2025-01-21 15:30:01 - INFO - WebRTC signaling server initialized
2025-01-21 15:30:02 - INFO - Building pipeline with source: file:///app/videos/sample.mp4
2025-01-21 15:30:05 - INFO - ✓ DeepStream pipeline built successfully
2025-01-21 15:30:05 - INFO - Starting DeepStream pipeline
2025-01-21 15:30:06 - INFO - ✓ Pipeline started successfully
==========================================================
🎥 Edge Node is RUNNING
==========================================================
📹 Source: file:///app/videos/sample.mp4
🌐 API: http://localhost:8000
📊 Metrics: http://localhost:8000/api/status
🔌 WebSocket Metadata: ws://localhost:8000/ws/metadata
📡 WebRTC Signaling: ws://localhost:8000/ws/signaling
==========================================================

2025-01-21 15:30:07 - INFO - ✓ Vehicle: ID=1, Speed=45.2 km/h, Plate=29A-12345
2025-01-21 15:30:07 - INFO - ✓ Vehicle: ID=2, Speed=52.8 km/h, Plate=30B-67890
2025-01-21 15:30:08 - WARNING - ⚠️  OVERSPEED: ID=3, Speed=75.3 km/h, Plate=51C-11111
2025-01-21 15:30:10 - INFO - 📊 Stats: Frames=100, Objects=15, Overspeed=3, FPS=28.5
```

---

## 🎯 Success Criteria

✅ **Pipeline running**: Logs show "Pipeline started successfully"  
✅ **Detection working**: Logs show vehicle detections  
✅ **Speed calculation**: Speed values appear in logs  
✅ **LPR working**: License plates appear in logs  
✅ **API responding**: `curl http://localhost:8000/api/status` returns JSON  
✅ **WebSocket working**: Can connect to `ws://localhost:8000/ws/metadata`  
✅ **FPS > 20**: Stats show FPS >= 20  

---

## 🚀 Next Steps

Sau khi test standalone thành công:

1. **Add MQTT**: Uncomment MQTT code trong `main_edge.py`
2. **Add Zenoh**: Enable Zenoh P2P offloading
3. **Deploy multiple edges**: Run 4 edge nodes
4. **Deploy center**: Start center server
5. **Full system test**: Test toàn bộ hệ thống

---

## 📝 Notes

- Standalone mode **BỎ QUA** MQTT và Zenoh
- Vẫn có **WebSocket** cho metadata streaming
- Vẫn có **WebRTC signaling** cho video streaming
- Vẫn có **FastAPI** cho REST API
- **Không cần** system monitor (jtop)
- **Không cần** coordinator (offloading)

Đây là cách **NHANH NHẤT** để test DeepStream pipeline!
