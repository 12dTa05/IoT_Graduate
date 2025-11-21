# Build and Run Instructions

## Prerequisites

### On Jetson (Edge Node)

1. **Install DeepStream SDK 6.3+**
   ```bash
   sudo apt install deepstream-6.3
   ```

2. **Install dependencies**
   ```bash
   sudo apt install \
       libgstreamer1.0-dev \
       libgstreamer-plugins-base1.0-dev \
       libopencv-dev \
       cmake \
       build-essential
   
   pip3 install -r requirements.txt
   ```

3. **Install jtop**
   ```bash
   sudo pip3 install jetson-stats
   sudo systemctl restart jtop.service
   ```

---

## Build C++ Components

```bash
cd C++_Python/edge/cpp
chmod +x build.sh
./build.sh
```

This will:
- Create `build/` directory
- Compile C++ code
- Generate `libdeepstream_wrapper.so`
- Copy library to `python/core/`

---

## Convert Models

Follow instructions in `models/README.md` to convert:
1. YOLO11 → TensorRT engine
2. LPRNet → TensorRT engine

Place engines in `models/` directory:
```
models/
├── yolo11n.engine
├── lprnet.engine
├── coco_labels.txt
└── lpr_labels.txt
```

---

## Run Edge Node

### Full system (with DeepStream)

```bash
cd C++_Python/edge

python3 main_edge.py \
    --edge-id edge_1 \
    --mqtt-broker 192.168.1.100 \
    --source rtsp://camera_ip/stream \
    --yolo-config configs/dstest_yolo.txt \
    --lpr-config configs/dstest_lpr.txt \
    --tracker-config configs/config_tracker.txt \
    --analytics-config configs/config_nvdsanalytics.txt
```

### Test mode (without DeepStream)

```bash
python3 main_edge.py \
    --edge-id edge_1 \
    --mqtt-broker 192.168.1.100
```

---

## Run Frontend

```bash
cd C++_Python/edge/frontend
npm install
npm run dev
```

Open http://localhost:5173

---

## Verify Installation

### Check library

```bash
python3 -c "
from python.core.deepstream_wrapper import DeepStreamPipeline
print('DeepStream wrapper loaded successfully')
"
```

### Check MQTT

```bash
# Subscribe to edge topics
mosquitto_sub -h localhost -t 'edge/#' -v
```

### Check metrics

```bash
curl http://localhost:8000/api/metrics
```

---

## Troubleshooting

### "Library not found"

```bash
# Check if library exists
ls -lh cpp/build/libdeepstream_wrapper.so

# Copy manually if needed
cp cpp/build/libdeepstream_wrapper.so python/core/
```

### "Failed to create element"

- Check DeepStream installation: `gst-inspect-1.0 nvinfer`
- Verify config paths are correct
- Check model files exist

### "CUDA out of memory"

- Reduce batch size in configs
- Use smaller YOLO model (yolo11n instead of yolo11m)
- Lower resolution in streammux

### Low FPS

```bash
# Monitor with jtop
sudo jtop

# Check GPU usage should be 70-90%
# If CPU bottleneck, optimize Python code
# If GPU bottleneck, reduce model size
```

---

## Performance Optimization

### 1. DeepStream Config

- Set `batched-push-timeout=40000` for low latency
- Use `live-source=1` for RTSP
- Enable `sync=False` on sink

### 2. Tracker

- Use NvDCF for best performance
- Reduce `tracker-width` and `tracker-height` if needed

### 3. Inference

- Use FP16 precision
- Optimize batch size (1 for latency, 4-8 for throughput)
- Enable TensorRT dynamic shapes

### 4. System

- Set Jetson to MAX performance mode:
  ```bash
  sudo nvpmodel -m 0
  sudo jetson_clocks
  ```

- Disable GUI if running headless:
  ```bash
  sudo systemctl set-default multi-user.target
  ```
