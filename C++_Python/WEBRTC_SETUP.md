# WebRTC Streaming Setup Guide

## Overview

WebRTC enables real-time video streaming from edge DeepStream pipeline to dashboard with ultra-low latency (<100ms).

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  Edge Node                               │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  DeepStream Pipeline                                     │
│         │                                                │
│         ├─→ nvdsosd (with bboxes)                       │
│         │                                                │
│         ├─→ nvv4l2h264enc (H.264 encoding)              │
│         │                                                │
│         ├─→ rtph264pay (RTP payloader)                  │
│         │                                                │
│         └─→ webrtcbin                                    │
│                  │                                       │
│                  ↓                                       │
│         WebRTC Signaling Server (FastAPI WebSocket)     │
│                  │                                       │
└──────────────────┼─────────────────────────────────────┘
                   │
                   │ SDP Offer/Answer
                   │ ICE Candidates
                   │
┌──────────────────┼─────────────────────────────────────┐
│                  ↓                                       │
│         ReactJS Dashboard                                │
│                  │                                       │
│         RTCPeerConnection                                │
│                  │                                       │
│         <video> element                                  │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

---

## Components

### 1. C++ WebRTC Sink

**File**: `edge/cpp/include/webrtc_sink.h`

**Key Features**:
- GStreamer `webrtcbin` integration
- Hardware H.264 encoding (`nvv4l2h264enc`)
- RTP payloading
- SDP offer/answer handling
- ICE candidate exchange

**Pipeline**:
```
nvdsosd → queue → nvv4l2h264enc → rtph264pay → webrtcbin
```

### 2. Python Signaling Server

**File**: `edge/python/core/webrtc_signaling.py`

**Responsibilities**:
- WebSocket signaling endpoint
- SDP offer/answer relay
- ICE candidate relay
- Client connection management

### 3. ReactJS Video Player

**File**: `edge/frontend/src/components/VideoPlayer.jsx`

**Features**:
- WebRTC peer connection
- Automatic signaling
- Video playback
- Metadata overlay (bounding boxes)

---

## Setup

### 1. Install GStreamer WebRTC Plugin

```bash
# Install gst-plugins-bad (contains webrtcbin)
sudo apt install gstreamer1.0-plugins-bad

# Verify
gst-inspect-1.0 webrtcbin
```

### 2. Update DeepStream Pipeline

Modify `ds_pipeline.cpp` to use WebRTC sink instead of fakesink:

```cpp
#include "webrtc_sink.h"

// In Pipeline::build()
WebRTCSink webrtc_sink;
GstElement* sink = webrtc_sink.createSink(GST_PIPELINE(pipeline_));

// Link OSD to WebRTC sink
gst_element_link(nvdsosd_, sink);

// Set callbacks
webrtc_sink.setOnOfferCallback([](const std::string& sdp) {
    // Send to Python signaling server
    signaling_server->send_offer("client_1", sdp);
});

webrtc_sink.setOnIceCandidateCallback([](const std::string& candidate, int index) {
    // Send to Python signaling server
    signaling_server->send_ice_candidate("client_1", candidate, index);
});
```

### 3. Start Signaling Server

The signaling server is integrated into FastAPI:

```python
# In main_edge.py
from python.core.webrtc_signaling import get_signaling_server
from python.api.app import app, set_signaling_server

signaling_server = get_signaling_server()
set_signaling_server(signaling_server)

# Run FastAPI
uvicorn.run(app, host="0.0.0.0", port=8000)
```

### 4. Dashboard Connection

The VideoPlayer component automatically connects:

```javascript
// Connects to ws://localhost:8000/ws/signaling
// Handles SDP offer/answer
// Handles ICE candidates
// Displays video in <video> element
```

---

## Signaling Flow

### 1. Initial Connection

```
Dashboard                 Signaling Server              Edge Pipeline
   │                             │                            │
   ├──── WebSocket Connect ─────→│                            │
   │                             │                            │
   │                             │←──── SDP Offer ────────────┤
   │←──── SDP Offer ─────────────┤                            │
   │                             │                            │
   ├──── SDP Answer ────────────→│                            │
   │                             │──── SDP Answer ───────────→│
```

### 2. ICE Candidate Exchange

```
Dashboard                 Signaling Server              Edge Pipeline
   │                             │                            │
   │                             │←──── ICE Candidate ────────┤
   │←──── ICE Candidate ─────────┤                            │
   │                             │                            │
   ├──── ICE Candidate ─────────→│                            │
   │                             │──── ICE Candidate ────────→│
```

### 3. Media Streaming

```
Dashboard                                              Edge Pipeline
   │                                                         │
   │←──────────────── RTP H.264 Stream ────────────────────┤
   │                                                         │
   │                    (Direct P2P)                         │
```

---

## Configuration

### H.264 Encoder Settings

```cpp
// In webrtc_sink.cpp
g_object_set(G_OBJECT(h264enc_),
    "bitrate", 4000000,           // 4 Mbps for 1280x720
    "profile", 0,                 // Baseline (best compatibility)
    "preset-level", 1,            // UltraFast (low latency)
    "insert-sps-pps", TRUE,       // Insert SPS/PPS for keyframes
    "idrinterval", 30,            // IDR every 30 frames (1 sec @ 30fps)
    nullptr);
```

### RTP Payloader Settings

```cpp
g_object_set(G_OBJECT(rtph264pay_),
    "config-interval", 1,         // Send SPS/PPS every second
    "pt", 96,                     // Payload type
    nullptr);
```

### WebRTC Settings

```cpp
g_object_set(G_OBJECT(webrtcbin_),
    "bundle-policy", 3,           // max-bundle (all media in one connection)
    "stun-server", "stun://stun.l.google.com:19302",
    nullptr);
```

---

## Performance

### Expected Latency

| Component | Latency |
|-----------|---------|
| H.264 Encoding | 10-15 ms |
| RTP Packetization | 1-2 ms |
| Network (LAN) | 5-10 ms |
| WebRTC Jitter Buffer | 20-50 ms |
| Decoding (Browser) | 10-20 ms |
| **Total** | **50-100 ms** |

### Bandwidth Usage

| Resolution | Bitrate | Bandwidth |
|------------|---------|-----------|
| 1280x720 @ 30fps | 4 Mbps | ~500 KB/s |
| 1920x1080 @ 30fps | 6 Mbps | ~750 KB/s |
| 640x480 @ 30fps | 2 Mbps | ~250 KB/s |

---

## Troubleshooting

### "No video displayed"

1. Check WebRTC connection state in browser console
2. Verify STUN server is reachable
3. Check firewall allows UDP traffic
4. Verify H.264 encoder is working

```bash
# Test encoder
gst-launch-1.0 videotestsrc ! nvv4l2h264enc ! fakesink
```

### "High latency (>200ms)"

1. Reduce encoder bitrate
2. Use UltraFast preset
3. Reduce IDR interval
4. Check network latency

### "Connection failed"

1. Check signaling WebSocket is connected
2. Verify SDP offer/answer exchange
3. Check ICE candidates are being exchanged
4. Try TURN server if behind NAT

```javascript
// Add TURN server
iceServers: [
  { urls: 'stun:stun.l.google.com:19302' },
  {
    urls: 'turn:your-turn-server.com:3478',
    username: 'user',
    credential: 'pass'
  }
]
```

---

## Advanced Features

### 1. Multiple Clients

Support multiple dashboard clients viewing same stream:

```cpp
// Create separate webrtcbin for each client
std::map<std::string, WebRTCSink*> client_sinks;

// Use tee element to duplicate stream
GstElement* tee = gst_element_factory_make("tee", "video-tee");
```

### 2. Adaptive Bitrate

Adjust bitrate based on network conditions:

```cpp
// Monitor RTCPeerConnection stats
pc.getStats().then(stats => {
  const packetsLost = stats.packetsLost;
  if (packetsLost > threshold) {
    // Request lower bitrate
  }
});
```

### 3. Recording

Record stream to file:

```cpp
// Add filesink branch
GstElement* filesink = gst_element_factory_make("filesink", "recorder");
g_object_set(G_OBJECT(filesink), "location", "recording.mp4", nullptr);
```

---

## Summary

✅ **Benefits**:
- Ultra-low latency (50-100ms)
- Direct P2P streaming
- Hardware-accelerated encoding
- Browser-native playback
- No additional plugins required

⚠️ **Limitations**:
- Requires STUN/TURN for NAT traversal
- H.264 only (browser compatibility)
- One-to-one streaming (use SFU for multi-client)

🎯 **Best For**:
- Real-time monitoring
- Live dashboards
- Low-latency applications
- Local network deployments
