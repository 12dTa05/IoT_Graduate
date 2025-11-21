# Zenoh P2P Offloading Setup Guide

## Overview

Zenoh enables low-latency P2P communication between edges for inference offloading.

---

## Installation

### 1. Install Zenoh Router

```bash
# Install Rust (if not installed)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Install Zenoh router
cargo install zenohd

# Verify installation
zenohd --version
```

### 2. Install Zenoh C Library (for C++ integration)

```bash
# Clone zenoh-c
git clone https://github.com/eclipse-zenoh/zenoh-c.git
cd zenoh-c

# Build
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
sudo make install
```

### 3. Install Zenoh Python (optional)

```bash
pip3 install eclipse-zenoh
```

---

## Configuration

### Zenoh Router Config

Create `zenoh-router.json5`:

```json5
{
  /// Router mode
  mode: "router",
  
  /// Listen endpoints
  listen: {
    endpoints: [
      "tcp/0.0.0.0:7447",
      "udp/0.0.0.0:7447"
    ]
  },
  
  /// Performance tuning
  transport: {
    unicast: {
      /// Low latency mode
      qos: {
        enabled: true
      },
      /// Batch size for throughput
      batch_size: 65535
    }
  },
  
  /// Logging
  plugins: {
    logger: {
      level: "info"
    }
  }
}
```

---

## Running Zenoh Router

### Start Router (Center Server)

```bash
# Run with config
zenohd -c zenoh-router.json5

# Or with default config
zenohd
```

**Expected Output**:
```
[2025-01-21T14:00:00Z INFO  zenohd] Zenoh router v0.10.1
[2025-01-21T14:00:00Z INFO  zenohd] Listening on tcp/0.0.0.0:7447
[2025-01-21T14:00:00Z INFO  zenohd] Listening on udp/0.0.0.0:7447
```

---

## Edge Configuration

### Update Edge Config

```python
# In main_edge.py
from python.core.coordinator import OffloadingCoordinator

# Initialize coordinator
coordinator = OffloadingCoordinator(
    edge_id=args.edge_id,
    system_monitor=monitor
)

# Start auto-offload loop
asyncio.create_task(coordinator.auto_offload_loop())
```

---

## Zenoh Topics

### Topic Structure

```
edge/{edge_id}/inference/request    # Inference requests
edge/{edge_id}/inference/response   # Inference responses
edge/{edge_id}/status               # Edge status
edge/discovery                      # Edge discovery
```

### Message Format

**Inference Request**:
```json
{
  "source_edge_id": "edge_1",
  "frame_number": 12345,
  "width": 1280,
  "height": 720,
  "timestamp": 1234567890.123,
  "frame_data": "<base64_jpeg>"
}
```

**Inference Response**:
```json
{
  "source_edge_id": "edge_1",
  "frame_number": 12345,
  "metadata_json": "[{...}]",
  "processing_time_ms": 25.5
}
```

---

## Offloading Flow

### 1. Detection Phase

```python
# Monitor checks system load
if coordinator.should_offload():
    # Find best target edge
    target = coordinator.find_target_edge()
    
    # Start offloading
    coordinator.start_offloading(target, ratio=0.3)
```

### 2. Frame Offloading

```python
# For each frame
if coordinator.should_offload_frame():
    # Compress frame
    jpeg_data = FrameSerializer.compressJPEG(frame, quality=70)
    
    # Send via Zenoh
    request = InferenceRequest(
        source_edge_id=edge_id,
        frame_number=frame_num,
        frame_data=jpeg_data,
        ...
    )
    zenoh_client.sendInferenceRequest(target_edge, request)
```

### 3. Processing on Target Edge

```python
# Target edge receives request
def on_inference_request(request):
    # Decompress frame
    frame = FrameSerializer.decompressJPEG(request.frame_data)
    
    # Run inference
    metadata = pipeline.process_frame(frame)
    
    # Send response
    response = InferenceResponse(
        source_edge_id=request.source_edge_id,
        frame_number=request.frame_number,
        metadata_json=json.dumps(metadata)
    )
    zenoh_client.sendInferenceResponse(response)
```

### 4. Receive Results

```python
# Source edge receives response
def on_inference_response(response):
    # Merge with local results
    metadata = json.loads(response.metadata_json)
    
    # Send to dashboard
    broadcast_metadata(metadata)
```

---

## Performance Optimization

### 1. Frame Compression

**JPEG Quality vs Size**:
| Quality | Size (KB) | Compression Ratio | Latency (ms) |
|---------|-----------|-------------------|--------------|
| 50 | 30 | 40x | 5 |
| 70 | 50 | 25x | 8 |
| 85 | 80 | 15x | 12 |
| 95 | 150 | 8x | 18 |

**Recommendation**: Quality 70 for best balance

### 2. ROI-based Offloading

Only send vehicle regions instead of full frame:

```python
# Extract vehicle ROI
roi = FrameSerializer.extractROI(frame, x, y, width, height)

# Compress ROI
jpeg_data = FrameSerializer.compressJPEG(roi, quality=70)

# 10x smaller payload!
```

### 3. Adaptive Offloading

Adjust offload ratio based on network latency:

```python
# Measure round-trip time
rtt = response.timestamp - request.timestamp

if rtt < 50:  # ms
    coordinator.offload_ratio = 0.5  # Offload 50%
elif rtt < 100:
    coordinator.offload_ratio = 0.3  # Offload 30%
else:
    coordinator.offload_ratio = 0.1  # Offload 10%
```

---

## Monitoring

### Check Zenoh Status

```bash
# List active sessions
zenohd admin --list-sessions

# Monitor throughput
zenohd admin --stats
```

### Monitor Offloading

```bash
# Check edge metrics
curl http://edge_1:8000/api/metrics

# Check offload status
curl http://center:8080/api/balancer/status
```

---

## Troubleshooting

### "Connection refused"

- Check Zenoh router is running
- Verify firewall allows port 7447
- Check router address in edge config

### High Latency (>100ms)

- Reduce JPEG quality
- Use ROI-based offloading
- Check network bandwidth
- Reduce offload ratio

### Frame Loss

- Increase Zenoh batch size
- Enable QoS in router config
- Check network packet loss

---

## Advanced Features

### 1. Multi-hop Offloading

Edge 1 → Edge 2 → Edge 3 (cascade offloading)

### 2. Load Balancing

Distribute load across multiple edges:

```python
# Round-robin
targets = ["edge_2", "edge_3", "edge_4"]
target = targets[frame_num % len(targets)]
```

### 3. Fallback to Local

If offloading fails, process locally:

```python
try:
    zenoh_client.sendInferenceRequest(target, request)
except Exception:
    # Fallback to local processing
    metadata = pipeline.process_frame(frame)
```

---

## Performance Metrics

### Expected Performance

| Metric | Value |
|--------|-------|
| Zenoh Latency | 5-10 ms |
| Frame Compression | 8 ms |
| Network Transfer (1Gbps) | 10-20 ms |
| Remote Inference | 25 ms |
| **Total Offload Latency** | **50-65 ms** |

### Comparison

| Mode | Latency | Throughput |
|------|---------|------------|
| Local Only | 25 ms | 40 FPS |
| With Offloading (30%) | 35 ms | 55 FPS |
| Full Offload | 60 ms | 70 FPS |

---

## Summary

✅ **Benefits**:
- Distribute load across edges
- Increase total system throughput
- Handle traffic spikes
- Fault tolerance (fallback to local)

⚠️ **Trade-offs**:
- Added latency (50-65ms)
- Network bandwidth usage
- Increased complexity

🎯 **Best Use Cases**:
- Temporary overload situations
- Non-real-time processing
- Batch processing
- Load balancing across edges
