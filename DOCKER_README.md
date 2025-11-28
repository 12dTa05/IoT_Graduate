# Docker Deployment Guide - IoT Traffic Monitoring System

Hướng dẫn triển khai hệ thống giám sát tốc độ xe sử dụng Docker với NVIDIA DeepStream L4T 6.4 Triton.

## Yêu cầu hệ thống

### Phần cứng
- **NVIDIA Jetson AGX Orin** (hoặc Jetson Xavier/Orin NX)
- Tối thiểu 8GB RAM
- 20GB dung lượng trống

### Phần mềm
- **JetPack 5.1+** đã cài đặt
- **Docker** và **NVIDIA Container Runtime**
- **Docker Compose** (tùy chọn nhưng khuyến nghị)

## Cài đặt Docker trên Jetson

Nếu chưa cài Docker, chạy lệnh sau:

```bash
# Cài đặt Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Cài đặt NVIDIA Container Runtime (thường đã có sẵn với JetPack)
sudo apt-get install -y nvidia-container-runtime

# Cài đặt Docker Compose
sudo apt-get install -y docker-compose
```

Khởi động lại để áp dụng quyền:
```bash
sudo reboot
```

## Chuẩn bị YOLO Model

Hệ thống cần file TensorRT engine cho YOLO11. Bạn có 2 lựa chọn:

### Option 1: Sử dụng model có sẵn
Nếu bạn đã có file `.engine`, copy vào thư mục:
```bash
cp your_model.engine DeepStream-YoLo/model_b1_gpu0_fp32.engine
```

### Option 2: Build model từ ONNX
```bash
cd DeepStream-YoLo

# Download YOLO11 ONNX model (ví dụ: yolo11s)
wget https://github.com/ultralytics/assets/releases/download/v0.0.0/yolo11s.onnx

# Model sẽ được build tự động khi chạy lần đầu
# Hoặc build thủ công bằng DeepStream tools
```

## Build Docker Image

```bash
cd /path/to/IoT_Graduate

# Build image (mất khoảng 2-3 phút)
docker build -t iot-traffic-monitor:latest .
```

> [!NOTE]
> **Optimized Build Process**
> 
> Dockerfile đã được tối ưu để sử dụng pyds có sẵn trong base image thay vì build từ source:
> - ✅ Build nhanh hơn (2-3 phút thay vì 15-20 phút)
> - ✅ Không cần compile, giảm lỗi build
> - ✅ Dễ maintain khi update DeepStream version

## Chuẩn bị Video và Cấu hình

### 1. Tạo thư mục videos
```bash
mkdir -p videos output logs
```

### 2. Copy video test vào thư mục
```bash
cp /path/to/your/video.mp4 videos/input.mp4
```

### 3. Cấu hình homography (tùy chọn)
Nếu cần calibrate lại điểm homography cho camera của bạn, chỉnh sửa file:
```bash
configs/points_source_target.yml
```

## Chạy Container

### Cách 1: Sử dụng Docker Compose (Khuyến nghị)

```bash
# Chạy container
docker-compose up

# Hoặc chạy ở chế độ background
docker-compose up -d

# Xem logs
docker-compose logs -f

# Dừng container
docker-compose down
```

### Cách 2: Sử dụng Docker Run

```bash
docker run --runtime nvidia \
  -v $(pwd)/videos:/app/videos \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/DeepStream-YoLo:/app/DeepStream-YoLo \
  -e VIDEO_FILE=/app/videos/input.mp4 \
  -e OUTPUT_FILE=/app/output/output.mp4 \
  -e SPEED_LIMIT=60 \
  --name traffic-monitor \
  iot-traffic-monitor:latest
```

## Tùy chỉnh cấu hình

Chỉnh sửa file `docker-compose.yml` hoặc truyền biến môi trường:

```yaml
environment:
  - VIDEO_FILE=/app/videos/input.mp4      # Video đầu vào
  - OUTPUT_FILE=/app/output/output.mp4    # Video đầu ra
  - SPEED_LIMIT=60                        # Giới hạn tốc độ (km/h)
  - VIDEO_FPS=25                          # FPS của video
  - MUX_WIDTH=1920                        # Độ phân giải xử lý
  - MUX_HEIGHT=1080
```

## Kết quả

Sau khi xử lý xong:

- **Video đầu ra**: `output/output.mp4` - Video có overlay tốc độ và bounding boxes
- **Logs**: `logs/speed_log.csv` - Log tốc độ các xe
- **Snapshots**: `logs/overspeed_snaps/` - Ảnh chụp xe vi phạm tốc độ

## Xử lý nhiều video

Để xử lý nhiều video, tạo script:

```bash
#!/bin/bash
for video in videos/*.mp4; do
    filename=$(basename "$video" .mp4)
    docker run --runtime nvidia \
        -v $(pwd)/videos:/app/videos \
        -v $(pwd)/output:/app/output \
        -v $(pwd)/logs:/app/logs \
        -v $(pwd)/DeepStream-YoLo:/app/DeepStream-YoLo \
        -e VIDEO_FILE=/app/videos/$(basename "$video") \
        -e OUTPUT_FILE=/app/output/${filename}_processed.mp4 \
        iot-traffic-monitor:latest
done
```

## Troubleshooting

### Docker Build Errors

#### Lỗi: "No space left on device"
- Vấn đề: Không đủ dung lượng disk
- Giải pháp: 
  ```bash
  # Xóa unused images và containers
  docker system prune -a
  # Cần ít nhất 10GB trống
  ```

#### Lỗi: "pyds module not found" khi chạy
- Vấn đề: pyds không được cài đúng cách
- Giải pháp: Kiểm tra base image có đúng version không (cần DeepStream 6.4+)

### Runtime Errors

### Lỗi: "Failed to create nvinfer"
- Kiểm tra file engine đã tồn tại: `DeepStream-YoLo/model_b1_gpu0_fp32.engine`
- Kiểm tra đường dẫn trong `DeepStream-YoLo/config_infer_primary_yolo11.txt`

### Lỗi: "Could not find library libnvds_nvmultiobjecttracker.so"
- Đảm bảo đang dùng image DeepStream L4T đúng phiên bản
- Kiểm tra runtime: `docker run --runtime nvidia ...`

### Video đầu ra bị lag hoặc không mượt
- Giảm resolution: `MUX_WIDTH=1280 MUX_HEIGHT=720`
- Tăng bitrate encoder trong `speedflow/pipeline_file.py`

### Tốc độ tính toán không chính xác
- Calibrate lại homography points trong `configs/points_source_target.yml`
- Kiểm tra FPS của video: `VIDEO_FPS=<your_fps>`

### Không phát hiện được xe
- Kiểm tra model YOLO có phù hợp không
- Điều chỉnh threshold trong `DeepStream-YoLo/config_infer_primary_yolo11.txt`:
  ```
  pre-cluster-threshold=0.25  # Giảm xuống 0.2 nếu cần
  ```

## Performance Tips

### Tối ưu cho Jetson AGX Orin
1. Sử dụng FP16 precision cho TensorRT engine
2. Giảm tracker resolution nếu cần:
   ```python
   tracker.set_property('tracker-width', 512)
   tracker.set_property('tracker-height', 384)
   ```
3. Sử dụng DLA (Deep Learning Accelerator) nếu có

### Monitor GPU Usage
```bash
# Trong container
watch -n 1 nvidia-smi

# Hoặc từ host
sudo tegrastats
```

## Cấu trúc thư mục

```
IoT_Graduate/
├── Dockerfile                  # Docker image definition
├── docker-compose.yml          # Docker Compose config
├── docker-entrypoint.sh        # Container entrypoint script
├── requirements.txt            # Python dependencies
├── .dockerignore              # Files to exclude from build
├── .env.example               # Environment variables template
├── speedflow/                 # Core application code
│   ├── pipeline_file.py       # File processing pipeline
│   ├── probes.py             # Speed calculation logic
│   ├── homography.py         # Coordinate transformation
│   └── settings.py           # Configuration settings
├── configs/                   # Configuration files
│   ├── config_nvdsanalytics.txt
│   └── points_source_target.yml
├── DeepStream-YoLo/          # YOLO model files
│   ├── config_infer_primary_yolo11.txt
│   ├── model_b1_gpu0_fp32.engine
│   └── labels.txt
├── videos/                    # Input videos (mounted)
├── output/                    # Processed videos (mounted)
└── logs/                      # Logs and snapshots (mounted)
```

## Liên hệ & Hỗ trợ

Nếu gặp vấn đề, vui lòng:
1. Kiểm tra logs: `docker-compose logs -f`
2. Kiểm tra GPU: `nvidia-smi` hoặc `tegrastats`
3. Xem DeepStream debug: Tăng `GST_DEBUG=3` trong docker-compose.yml
