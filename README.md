# 🚗 Speed Estimation on NVIDIA Jetson (DeepStream + Multi-RTSP + WebRTC)

Hệ thống đo tốc độ phương tiện giao thông bằng **thiết bị biên NVIDIA Jetson**, sử dụng **DeepStream SDK** kết hợp **YOLO + Tracker + Homography** để tính vận tốc, phát hiện vi phạm và truyền video thời gian thực lên **server WebRTC**.

---

## 📋 Cấu hình thiết bị

| Thành phần | Phiên bản |
|-------------|-----------|
| **Thiết bị** | NVIDIA Jetson AGX Orin 32GB |
| **JetPack** | 5.1.4+ |
| **CUDA** | 11.4 |
| **TensorRT** | 8.5.2.2+ |
| **DeepStream SDK** | 6.3+ |
| **Python** | 3.8+ |

---

## 🔧 Cài đặt hệ thống

### 1. Cài đặt JetPack và DeepStream SDK

Đảm bảo JetPack 5.1.4+ đã được cài đặt trên Jetson của bạn. Nếu chưa có, tải và cài đặt từ [NVIDIA SDK Manager](https://developer.nvidia.com/sdk-manager).

Cài đặt DeepStream SDK:
```bash
sudo apt update
sudo apt install deepstream-6.3
```

### 2. Cài đặt Python bindings cho DeepStream

Clone repository DeepStream Python Apps:
```bash
cd ~
git clone https://github.com/NVIDIA-AI-IOT/deepstream_python_apps.git
cd deepstream_python_apps
git submodule update --init
```

Cài đặt các dependencies:
```bash
sudo apt-get install python3-gi python3-dev python3-gst-1.0 \
    python-gi-dev git python-dev python3 python3-pip \
    python3.8-dev cmake g++ build-essential libglib2.0-dev \
    libglib2.0-dev-bin libgstreamer1.0-dev libtool m4 autoconf automake \
    libgirepository1.0-dev libcairo2-dev
```

Build và cài đặt pyds (Python bindings):
```bash
cd deepstream_python_apps/bindings
mkdir build
cd build
cmake ..
make
pip3 install ./pyds-*.whl
```

### 3. Cài đặt DeepStream-YOLO

Clone repository DeepStream-YOLO:
```bash
cd ~/
git clone https://github.com/marcoslucianops/DeepStream-Yolo.git
```

Làm theo hướng dẫn chi tiết tại: [Ultralytics – DeepStream on NVIDIA Jetson](https://docs.ultralytics.com/guides/deepstream-nvidia-jetson/)

### 4. Cài đặt dependencies của project

Clone project này và cài đặt dependencies:
```bash
cd ~/
git clone <repository-url> IoT_Graduate
cd IoT_Graduate
pip3 install -r requirements.txt
```

### 5. Chuẩn bị YOLO model

Đảm bảo bạn đã có file TensorRT engine cho YOLO11 trong thư mục `DeepStream-YoLo/`:
```bash
# Nếu bạn có file ONNX, convert sang TensorRT engine
cd DeepStream-YoLo
# Follow instructions from DeepStream-YOLO repository
```

File engine cần có tên: `model_b1_gpu0_fp32.engine` (hoặc cập nhật đường dẫn trong `config_infer_primary_yolo11.txt`)

---

## 🚀 Chạy hệ thống

### Chế độ 1: Xử lý video file

Xử lý video từ file và xuất ra video có overlay tốc độ:

```bash
python3 run_file.py <đường_dẫn_video> --homo configs/points_source_target.yml --out output/output.mp4
```

**Ví dụ:**
```bash
python3 run_file.py videos/traffic.mp4 --homo configs/points_source_target.yml --out output/result.mp4
```

### Chế độ 2: GUI trên Jetson

Chạy ứng dụng với giao diện đồ họa hiển thị trực tiếp:

```bash
python3 speed_gui.py
```

### Chế độ 3: Streaming RTSP

Xử lý nguồn RTSP camera:

```bash
python3 run_RTSP.py <rtsp_url>
```

**Ví dụ:**
```bash
python3 run_RTSP.py rtsp://192.168.1.100:554/stream
```

### Chế độ 4: Streaming lên WebRTC server

Xử lý và stream video lên WebRTC server:

```bash
python3 run_webrtc.py <nguồn_rtsp> --server <ip_server> --room <tên_phòng> --cfg config_cam.txt
```

**Ví dụ:**
```bash
python3 run_webrtc.py rtsp://192.168.1.100:554/stream --server 192.168.1.50 --room camera01 --cfg config_cam.txt
```

---

## ⚙️ Cấu hình

### Homography Configuration

File `configs/points_source_target.yml` chứa các điểm calibration cho homography transformation. Bạn cần calibrate lại cho camera của mình:

```yaml
source_points:
  - [x1, y1]
  - [x2, y2]
  - [x3, y3]
  - [x4, y4]

target_points:
  - [x1, y1]
  - [x2, y2]
  - [x3, y3]
  - [x4, y4]
```

### YOLO Configuration

Chỉnh sửa `DeepStream-YoLo/config_infer_primary_yolo11.txt` để điều chỉnh:
- Threshold phát hiện
- Kích thước input
- Batch size
- Precision (FP32/FP16)

### Speed Limit

Giới hạn tốc độ mặc định được set trong code. Để thay đổi, chỉnh sửa file `speedflow/settings.py` hoặc truyền qua command line arguments.

---

## 📊 Kết quả

Sau khi xử lý, bạn sẽ có:

- **Video đầu ra**: `output/` - Video có overlay tốc độ và bounding boxes
- **Logs**: `logs/speed_log.csv` - Log chi tiết tốc độ các xe
- **Snapshots**: `logs/overspeed_snaps/` - Ảnh chụp xe vi phạm tốc độ

---

## 🐛 Troubleshooting

### Lỗi: "pyds module not found"
```bash
# Kiểm tra pyds đã cài đặt chưa
python3 -c "import pyds; print(pyds.__version__)"

# Nếu chưa có, cài đặt lại theo bước 2
```

### Lỗi: "Failed to create nvinfer"
- Kiểm tra file engine tồn tại: `DeepStream-YoLo/model_b1_gpu0_fp32.engine`
- Kiểm tra đường dẫn trong config file
- Đảm bảo model tương thích với TensorRT version

### Lỗi: "GStreamer pipeline failed"
```bash
# Tăng debug level để xem chi tiết lỗi
export GST_DEBUG=3
python3 run_file.py <video>
```

### Performance không tốt
- Sử dụng FP16 precision thay vì FP32
- Giảm resolution input
- Tối ưu tracker settings trong code

---

## 📁 Cấu trúc thư mục

```
IoT_Graduate/
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
├── run_file.py               # Video file processing
├── run_RTSP.py               # RTSP stream processing
├── run_webrtc.py             # WebRTC streaming
├── speed_gui.py              # GUI application
├── config_cam.txt            # Camera configuration
├── requirements.txt          # Python dependencies
├── videos/                   # Input videos
├── output/                   # Processed videos
└── logs/                     # Logs and snapshots
```

---

## 📝 Ghi chú

- Hệ thống yêu cầu NVIDIA Jetson với GPU để chạy DeepStream
- Đảm bảo đủ dung lượng disk cho video output và logs
- Calibrate homography points cho từng vị trí camera khác nhau
- Điều chỉnh speed limit phù hợp với từng khu vực

---

## 📞 Liên hệ & Hỗ trợ

Nếu gặp vấn đề, vui lòng:
1. Kiểm tra logs trong thư mục `logs/`
2. Chạy với `GST_DEBUG=3` để xem chi tiết lỗi GStreamer
3. Kiểm tra GPU usage: `tegrastats`