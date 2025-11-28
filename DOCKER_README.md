# 🐳 Docker Quick Start - File Display Mode

Hướng dẫn nhanh để chạy Traffic Monitoring với **file MP4** và hiển thị kết quả trên màn hình host.

---

## 📋 Yêu Cầu

- **NVIDIA Jetson AGX Orin** với JetPack 5.x/6.x
- **Docker** và **NVIDIA Container Runtime** đã cài đặt
- **X Server** đang chạy (để hiển thị output)

---

## 🚀 Quick Start (3 bước)

### Bước 1: Chuẩn bị video file

```bash
# Tạo thư mục test_videos
mkdir -p test_videos

# Copy video file của bạn vào
cp /path/to/your/video.mp4 test_videos/test.mp4
```

### Bước 2: Setup X11 permissions

```bash
# Cho phép Docker container truy cập X server
xhost +local:docker

# Tạo X authority file
touch /tmp/.docker.xauth
xauth nlist $DISPLAY | sed -e 's/^..../ffff/' | xauth -f /tmp/.docker.xauth nmerge -
```

### Bước 3: Chạy với Docker Compose

```bash
# Build và chạy
docker-compose up --build

# Hoặc chạy ở background
docker-compose up -d --build

# Xem logs
docker-compose logs -f
```

**Kết quả**: Cửa sổ hiển thị video với bounding boxes và tốc độ sẽ xuất hiện trên màn hình!

---

## 🎯 Chạy với video khác

### Cách 1: Sửa file .env

```bash
# Copy template
cp .env.example .env

# Edit .env
nano .env
```

Thay đổi:
```bash
VIDEO_FILE=/app/test_videos/your_video.mp4
```

### Cách 2: Override environment variable

```bash
VIDEO_FILE=/app/test_videos/another_video.mp4 docker-compose up
```

### Cách 3: Chạy trực tiếp với docker run

```bash
docker run -it --rm \
  --runtime nvidia \
  --network host \
  -e DISPLAY=$DISPLAY \
  -e VIDEO_FILE=/app/test_videos/test.mp4 \
  -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
  -v /tmp/.docker.xauth:/tmp/.docker.xauth:rw \
  -v $(pwd)/test_videos:/app/test_videos:ro \
  -v $(pwd)/configs:/app/configs:ro \
  -v $(pwd)/DeepStream-YoLo:/app/DeepStream-YoLo:ro \
  -v $(pwd)/logs:/app/logs \
  traffic-monitor:latest
```

---

## 📁 Cấu Trúc Thư Mục

```
IoT_Graduate/
├── test_videos/           # Đặt video files ở đây
│   └── test.mp4
├── configs/               # Configuration files
│   ├── config_nvdsanalytics.txt
│   └── points_source_target.yml
├── DeepStream-YoLo/       # YOLO model files
├── logs/                  # Output logs và snapshots
│   └── overspeed_snaps/
├── Dockerfile
├── docker-compose.yml
└── .env
```

---

## ⚙️ Configuration

### Homography Points

Edit `configs/points_source_target.yml` để calibrate cho video của bạn:

```yaml
source:  # 4 điểm trên video (pixel coordinates)
  - [100, 200]
  - [500, 200]
  - [50, 600]
  - [550, 600]

target:  # Khoảng cách thực tế (meters)
  - [0, 0]
  - [10, 0]
  - [0, 20]
  - [10, 20]
```

### Speed Settings

Edit `speedflow/settings.py`:

```python
VIDEO_FPS = 25.0           # FPS của video
SPEED_LIMIT_KMH = 60.0     # Ngưỡng vi phạm tốc độ
```

---

## � Xem Kết Quả

### Logs

```bash
# Real-time logs
docker-compose logs -f

# Speed calculations
cat logs/speed_log.csv
```

### Overspeed Snapshots

```bash
# Xem ảnh các phương tiện vi phạm
ls -la logs/overspeed_snaps/
```

---

## 🐛 Troubleshooting

### Không hiển thị cửa sổ

**Giải pháp:**
```bash
# Kiểm tra DISPLAY
echo $DISPLAY

# Cho phép X11 forwarding
xhost +local:docker

# Kiểm tra X authority
ls -la /tmp/.docker.xauth
```

### "Cannot open display"

**Giải pháp:**
```bash
# Export DISPLAY
export DISPLAY=:0

# Tạo lại X authority
xauth nlist $DISPLAY | sed -e 's/^..../ffff/' | xauth -f /tmp/.docker.xauth nmerge -
```

### Video file not found

**Giải pháp:**
```bash
# Kiểm tra file tồn tại
ls -la test_videos/

# Kiểm tra path trong .env
cat .env | grep VIDEO_FILE

# Path phải là /app/test_videos/... (path trong container)
```

### Low FPS / Lag

**Giải pháp:**
- Giảm resolution trong `speedflow/settings.py`
- Sử dụng video có resolution thấp hơn
- Kiểm tra GPU memory: `nvidia-smi`

---

## � Stop Container

```bash
# Stop
docker-compose down

# Stop và xóa volumes
docker-compose down -v

# Revoke X11 permissions
xhost -local:docker
```

---

## � Tips

1. **Test với video ngắn** (30-60s) trước khi chạy video dài
2. **Calibrate homography** cẩn thận để tính tốc độ chính xác
3. **Check logs** để debug nếu có vấn đề
4. **Mount logs volume** để lưu kết quả
5. **Sử dụng video có FPS ổn định** (25 hoặc 30 FPS)

---

## 📞 Common Commands

```bash
# Build image
docker-compose build

# Run (foreground)
docker-compose up

# Run (background)
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down

# Rebuild and run
docker-compose up --build

# Shell vào container
docker-compose exec traffic-monitor bash
```

Happy testing! 🚗💨
