#!/bin/bash

# Build and run standalone edge node for testing
# No MQTT or Zenoh required

set -e

echo "=========================================="
echo "🚀 Building Standalone Edge Node"
echo "=========================================="

# Build Docker image
echo "📦 Building Docker image..."
docker build -t edge-node-standalone -f edge/Dockerfile .

echo ""
echo "✓ Build complete!"
echo ""
echo "=========================================="
echo "🎯 Running Edge Node"
echo "=========================================="
echo ""
echo "Available options:"
echo ""
echo "1. Run with sample video (file):"
echo "   docker run --runtime nvidia --rm -it \\"
echo "     -p 8000:8000 \\"
echo "     -v \$(pwd)/videos:/app/videos \\"
echo "     edge-node-standalone \\"
echo "     python3 main_edge_standalone.py --source file:///app/videos/sample.mp4"
echo ""
echo "2. Run with RTSP camera:"
echo "   docker run --runtime nvidia --rm -it \\"
echo "     -p 8000:8000 \\"
echo "     edge-node-standalone \\"
echo "     python3 main_edge_standalone.py --source rtsp://camera-ip/stream"
echo ""
echo "3. Run with USB camera:"
echo "   docker run --runtime nvidia --rm -it \\"
echo "     -p 8000:8000 \\"
echo "     --device /dev/video0 \\"
echo "     edge-node-standalone \\"
echo "     python3 main_edge_standalone.py --source v4l2:///dev/video0"
echo ""
echo "4. Run with test pattern (no camera needed):"
echo "   docker run --runtime nvidia --rm -it \\"
echo "     -p 8000:8000 \\"
echo "     edge-node-standalone \\"
echo "     python3 main_edge_standalone.py --source videotestsrc"
echo ""
echo "=========================================="
echo "📊 Access Points:"
echo "=========================================="
echo "API Status:    http://localhost:8000/api/status"
echo "Metadata WS:   ws://localhost:8000/ws/metadata"
echo "WebRTC Signal: ws://localhost:8000/ws/signaling"
echo "=========================================="
