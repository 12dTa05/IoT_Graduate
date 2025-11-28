# 🐳 Docker Deployment Guide - Traffic Monitoring System

Hướng dẫn triển khai hệ thống Traffic Monitoring trên **NVIDIA Jetson AGX Orin** sử dụng Docker với image `deepstream-l4t-6.4-triton-multiarch`.

---

## 📋 Yêu Cầu Hệ Thống

### Phần Cứng
- **NVIDIA Jetson AGX Orin** (32GB recommended)
- **JetPack 5.x hoặc 6.x** đã cài đặt
- **GPU Memory**: Tối thiểu 4GB available
- **Storage**: Tối thiểu 20GB free space

### Phần Mềm
- **Docker** đã cài đặt
- **NVIDIA Container Runtime** đã cài đặt
- **Docker Compose** (optional, nhưng recommended)

### Kiểm Tra Cài Đặt

```bash
# Kiểm tra Docker
docker --version

# Kiểm tra NVIDIA runtime
docker run --rm --runtime nvidia nvcr.io/nvidia/l4t-base:r35.1.0 nvidia-smi

# Kiểm tra Docker Compose
docker-compose --version
```

---

## 🚀 Quick Start

### 1. Clone Repository (hoặc copy code)

```bash
cd /path/to/IoT_Graduate
```

### 2. Chuẩn Bị Model Files

**QUAN TRỌNG**: YOLO model engine files (`.engine`) cần được build trên Jetson với TensorRT tương ứng.

```bash
# Tạo thư mục models nếu chưa có
mkdir -p DeepStream-YoLo/models

# Build YOLO engine (ví dụ với YOLOv11)
# Làm theo hướng dẫn tại: https://github.com/marcoslucianops/DeepStream-Yolo
cd DeepStream-YoLo
# ... build engine theo hướng dẫn ...
```

### 3. Cấu Hình Environment Variables

```bash
# Copy template
cp .env.example .env

# Edit .env file
nano .env
```

**Cấu hình trong `.env`:**
```bash
# RTSP camera
VIDEO_SOURCE=rtsp://admin:password@192.168.1.64:554/Streaming/Channels/101

# Hoặc video file
# VIDEO_SOURCE=file:///app/test_videos/test.mp4

WEBRTC_SERVER=192.168.0.158
WEBRTC_ROOM=demo
CONFIG_FILE=/app/configs/config_cam.txt
```

### 4. Chuẩn Bị Configuration Files

Đảm bảo các file config tồn tại và đúng paths:

```bash
# Kiểm tra config files
ls -la configs/
# Cần có:
# - config_cam.txt (hoặc config tương tự)
# - config_nvdsanalytics.txt
# - points_source_target.yml (homography points)
```

**Chỉnh sửa paths trong config files** để phù hợp với Docker container:

`configs/config_cam.txt`:
```ini
ANALYTICS_CFG=/app/configs/config_nvdsanalytics.txt
HOMO_YML=/app/configs/points_source_target.yml
VIDEO_FPS=25
MUX_WIDTH=1920
MUX_HEIGHT=1080
```

### 5. Build Docker Image

```bash
# Build image
docker build -t traffic-monitor:latest .

# Kiểm tra image đã build
docker images | grep traffic-monitor
```

### 6. Chạy Container

#### Option A: Sử dụng Docker Compose (Recommended)

```bash
# Start service
docker-compose up -d

# Xem logs
docker-compose logs -f

# Stop service
docker-compose down
```

#### Option B: Sử dụng Docker Run

```bash
docker run -d \
  --name traffic-monitor \
  --runtime nvidia \
  --network host \
  -v $(pwd)/configs:/app/configs:ro \
  -v $(pwd)/DeepStream-YoLo:/app/DeepStream-YoLo:ro \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/output:/app/output \
  -e VIDEO_SOURCE="rtsp://admin:password@192.168.1.64:554/Streaming/Channels/101" \
  -e WEBRTC_SERVER="192.168.0.158" \
  -e WEBRTC_ROOM="demo" \
  -e CONFIG_FILE="/app/configs/config_cam.txt" \
  traffic-monitor:latest

# Xem logs
docker logs -f traffic-monitor
```

---

## 📝 Các Chế Độ Chạy

### 1. RTSP Camera Streaming

```bash
# Sử dụng environment variables
docker run --runtime nvidia --network host \
  -v $(pwd)/configs:/app/configs:ro \
  -v $(pwd)/logs:/app/logs \
  -e VIDEO_SOURCE="rtsp://admin:password@192.168.1.64:554/Streaming/Channels/101" \
  -e WEBRTC_SERVER="192.168.0.158" \
  -e WEBRTC_ROOM="camera1" \
  -e CONFIG_FILE="/app/configs/config_cam.txt" \
  traffic-monitor:latest
```

### 2. Video File Processing

```bash
# Mount video files vào container
docker run --runtime nvidia --network host \
  -v $(pwd)/configs:/app/configs:ro \
  -v $(pwd)/test_videos:/app/test_videos:ro \
  -v $(pwd)/logs:/app/logs \
  -e VIDEO_SOURCE="file:///app/test_videos/test.mp4" \
  -e WEBRTC_SERVER="192.168.0.158" \
  -e WEBRTC_ROOM="test" \
  -e CONFIG_FILE="/app/configs/config_cam.txt" \
  traffic-monitor:latest
```

### 3. Multiple Cameras (Docker Compose)

Edit `docker-compose.yml` để thêm multiple services:

```yaml
services:
  camera1:
    extends: traffic-monitor
    environment:
      - VIDEO_SOURCE=rtsp://admin:password@192.168.1.64:554/Streaming/Channels/101
      - WEBRTC_ROOM=camera1
    container_name: traffic-monitor-cam1

  camera2:
    extends: traffic-monitor
    environment:
      - VIDEO_SOURCE=rtsp://admin:password@192.168.1.96:554/Streaming/Channels/101
      - WEBRTC_ROOM=camera2
    container_name: traffic-monitor-cam2
```

---

## 🔧 Configuration

### Homography Calibration

Để tính toán tốc độ chính xác, cần calibrate homography matrix cho từng camera:

1. **Chọn 4 điểm trên video** (source points)
2. **Đo khoảng cách thực tế** (target points in meters)
3. **Cập nhật file** `configs/points_source_target.yml`:

```yaml
source:
  - [100, 200]   # Top-left
  - [500, 200]   # Top-right
  - [50, 600]    # Bottom-left
  - [550, 600]   # Bottom-right

target:
  - [0, 0]       # Tọa độ thực (meters)
  - [10, 0]
  - [0, 20]
  - [10, 20]
```

### Analytics ROI Configuration

Edit `configs/config_nvdsanalytics.txt` để định nghĩa ROI:

```ini
[property]
enable=1
config-width=1920
config-height=1080

[roi-filtering-stream-0]
enable=1
roi-RF=100;200;500;200;550;600;50;600
```

### Speed Limit Configuration

Edit `speedflow/settings.py`:

```python
SPEED_LIMIT_KMH = 60.0  # Ngưỡng vi phạm tốc độ
VIDEO_FPS = 25.0        # FPS của video
```

---

## 📊 Monitoring & Logs

### Xem Logs Real-time

```bash
# Docker Compose
docker-compose logs -f

# Docker run
docker logs -f traffic-monitor
```

### Kiểm Tra Outputs

```bash
# Speed logs
cat logs/speed_log.csv

# Overspeed snapshots
ls -la logs/overspeed_snaps/
```

### Container Stats

```bash
# CPU, Memory, GPU usage
docker stats traffic-monitor

# GPU memory (inside container)
docker exec traffic-monitor nvidia-smi
```

---

## 🐛 Troubleshooting

### 1. Container không start

**Kiểm tra logs:**
```bash
docker logs traffic-monitor
```

**Các lỗi thường gặp:**

- **"DeepStream not found"**: Kiểm tra base image đúng version
- **"Config file not found"**: Kiểm tra volume mounts và paths
- **"nvstreammux plugin not found"**: DeepStream plugins chưa load

### 2. RTSP Connection Failed

```bash
# Test RTSP stream bên ngoài container
ffplay rtsp://admin:password@192.168.1.64:554/Streaming/Channels/101

# Kiểm tra network connectivity từ container
docker exec traffic-monitor ping 192.168.1.64
```

### 3. WebRTC Không Kết Nối

- Kiểm tra WebRTC server đang chạy
- Kiểm tra firewall/network configuration
- Verify WebSocket URL: `ws://{server}:8080/ws?room={room}&role=pub`

### 4. GPU Not Available

```bash
# Kiểm tra NVIDIA runtime
docker run --rm --runtime nvidia nvcr.io/nvidia/l4t-base:r35.1.0 nvidia-smi

# Kiểm tra Docker daemon config
cat /etc/docker/daemon.json
# Cần có:
# {
#   "default-runtime": "nvidia",
#   "runtimes": {
#     "nvidia": {
#       "path": "nvidia-container-runtime",
#       "runtimeArgs": []
#     }
#   }
# }
```

### 5. Model Engine Not Found

```bash
# Kiểm tra model files
docker exec traffic-monitor ls -la /app/DeepStream-YoLo/

# Rebuild engine nếu cần (trên Jetson)
cd DeepStream-YoLo
# Follow build instructions
```

### 6. Low FPS / Performance Issues

**Giảm resolution:**
```ini
# config_cam.txt
MUX_WIDTH=1280
MUX_HEIGHT=720
```

**Giảm tracker resolution:**
```python
# speedflow/pipeline_webrtc.py
tracker.set_property('tracker-width', 480)
tracker.set_property('tracker-height', 320)
```

**Sử dụng INT8 inference:**
- Build YOLO engine với INT8 quantization
- Xem hướng dẫn tại DeepStream-YoLo docs

---

## 🔄 Updates & Maintenance

### Update Code

```bash
# Pull latest code
git pull

# Rebuild image
docker-compose build

# Restart service
docker-compose up -d
```

### Cleanup

```bash
# Stop và remove containers
docker-compose down

# Remove old images
docker image prune -a

# Clear logs
rm -rf logs/*
```

### Backup Configuration

```bash
# Backup configs
tar -czf configs_backup_$(date +%Y%m%d).tar.gz configs/

# Backup logs
tar -czf logs_backup_$(date +%Y%m%d).tar.gz logs/
```

---

## 📚 Additional Resources

- **DeepStream SDK**: https://developer.nvidia.com/deepstream-sdk
- **DeepStream-YOLO**: https://github.com/marcoslucianops/DeepStream-Yolo
- **Ultralytics DeepStream Guide**: https://docs.ultralytics.com/guides/deepstream-nvidia-jetson/
- **NVIDIA Container Toolkit**: https://github.com/NVIDIA/nvidia-docker

---

## 💡 Tips & Best Practices

1. **Always use host network** khi truy cập RTSP cameras trên local network
2. **Mount logs volume** để persist data khi container restart
3. **Monitor GPU memory** để tránh OOM errors
4. **Calibrate homography** cẩn thận cho từng camera position
5. **Test với video file** trước khi deploy với RTSP cameras
6. **Backup configs** thường xuyên
7. **Use docker-compose** cho production deployment

---

## 📞 Support

Nếu gặp vấn đề, kiểm tra:
1. Logs trong container
2. GPU availability
3. Network connectivity
4. Config file paths
5. Model engine files

Happy monitoring! 🚗💨
