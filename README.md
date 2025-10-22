#  Speed Estimation on NVIDIA Jetson (DeepStream + Multi-RTSP + WebRTC)

Hệ thống đo tốc độ phương tiện giao thông bằng **thiết bị biên NVIDIA Jetson**, sử dụng **DeepStream SDK** kết hợp **YOLO + Tracker + Homography** để tính vận tốc, phát hiện vi phạm và truyền video thời gian thực lên **server WebRTC**.

---

##  Cấu hình thiết bị

| Thành phần | Phiên bản |
|-------------|-----------|
| **Thiết bị** | NVIDIA Jetson AGX Orin 32GB |
| **JetPack** | 5.1.4 |
| **CUDA** | 11.4 |
| **TensorRT** | 8.5.2.2 |
| **DeepStream SDK** | 6.3 |

---
##  Điều kiện tiên quyết

1. **Cài đặt Python cho Jetson (tương thích với DeepStream)**  
    [DeepStream Python Apps – NVIDIA](https://github.com/NVIDIA-AI-IOT/deepstream_python_apps.git)

2. **Tải DeepStream-YOLO và mô hình YOLO muốn sử dụng**  
    [DeepStream-YOLO Repository](https://github.com/marcoslucianops/DeepStream-Yolo.git)

   Làm theo hướng dẫn chi tiết của Ultralytics tại:  
    [Ultralytics – DeepStream on NVIDIA Jetson](https://docs.ultralytics.com/guides/deepstream-nvidia-jetson/)

---

##  Chạy thử nghiệm trên GUI của Jetson

Chạy ứng dụng hiển thị video và tốc độ trực tiếp trên Jetson:

```bash
python3 speed_gui.py
```
chạy streaming lên server
```bash
python3 run_webrtc.py <nguồn rtsp> --server <ip server> --room <tên nguồn trên server> --cfg <file config config_cam.txt> 
```