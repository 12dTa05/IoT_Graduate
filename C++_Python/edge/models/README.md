# Model Conversion Guide

Hướng dẫn convert YOLO và LPRNet sang TensorRT engine cho DeepStream.

## 1. YOLO11 Conversion

### Bước 1: Export ONNX

```bash
# Clone Ultralytics YOLO
git clone https://github.com/ultralytics/ultralytics.git
cd ultralytics

# Install dependencies
pip install ultralytics

# Export YOLO11n to ONNX
yolo export model=yolo11n.pt format=onnx simplify=True opset=12
```

### Bước 2: Convert ONNX → TensorRT

```bash
# Using trtexec (included with TensorRT)
/usr/src/tensorrt/bin/trtexec \
    --onnx=yolo11n.onnx \
    --saveEngine=yolo11n.engine \
    --fp16 \
    --workspace=4096 \
    --minShapes=images:1x3x640x640 \
    --optShapes=images:1x3x640x640 \
    --maxShapes=images:1x3x640x640 \
    --verbose
```

### Bước 3: Copy to models directory

```bash
cp yolo11n.engine ../C++_Python/edge/models/
```

---

## 2. LPRNet Conversion

### Bước 1: Get LPRNet model

```bash
# Clone LPRNet repository
git clone https://github.com/sirius-ai/LPRNet_Pytorch.git
cd LPRNet_Pytorch

# Download pretrained weights
# (hoặc train model của bạn)
```

### Bước 2: Export to ONNX

```python
# export_lprnet.py
import torch
import torch.onnx
from model.LPRNet import LPRNet

# Load model
lprnet = LPRNet(class_num=68, dropout_rate=0.5)
lprnet.load_state_dict(torch.load('weights/Final_LPRNet_model.pth'))
lprnet.eval()

# Dummy input (batch_size, channels, height, width)
dummy_input = torch.randn(1, 3, 24, 94)

# Export
torch.onnx.export(
    lprnet,
    dummy_input,
    "lprnet.onnx",
    export_params=True,
    opset_version=12,
    do_constant_folding=True,
    input_names=['input'],
    output_names=['output'],
    dynamic_axes={
        'input': {0: 'batch_size'},
        'output': {0: 'batch_size'}
    }
)

#Simplifier
model_onnx = onnx.load("lprnet.onnx")
model_simp, check = simplify(model_onnx)

if check:
    onnx.save(model_simp, "lprnet_simplified.onnx")
    print("Simplified model saved as lprnet_simplified.onnx")
else:
    print("Failed to simplify model")
```

### Bước 3: Convert ONNX → TensorRT

```bash
/usr/src/tensorrt/bin/trtexec \
    --onnx=lprnet.onnx \
    --saveEngine=lprnet.engine \
    --fp16 \
    --workspace=2048 \
    --minShapes=input:1x3x24x94 \
    --optShapes=input:16x3x24x94 \
    --maxShapes=input:16x3x24x94 \
    --verbose
```

### Bước 4: Copy to models directory

```bash
cp lprnet.engine ../C++_Python/edge/models/
```

---

## 3. Label Files

### COCO Labels (coco_labels.txt)

```
person
bicycle
car
motorcycle
airplane
bus
train
truck
...
```

### LPR Labels (lpr_labels.txt)

```
0
1
2
3
4
5
6
7
8
9
A
B
C
...
Z
-
```

---

## 4. Verify Models

### Test YOLO engine

```bash
cd C++_Python/edge
python3 -c "
import ctypes
lib = ctypes.CDLL('cpp/build/libdeepstream_wrapper.so')
print('Library loaded successfully')
"
```

### Test full pipeline

```bash
python3 main_edge.py \
    --edge-id edge_1 \
    --source file:///path/to/test_video.mp4 \
    --mqtt-broker localhost
```

---

## 5. Performance Tuning

### YOLO Optimization

- Use FP16 for 2x speedup
- Reduce input size (640x640 → 416x416) for faster inference
- Adjust NMS threshold in config

### LPRNet Optimization

- Batch size 16 for better throughput
- FP16 precision
- Crop only vehicle ROI before inference

### Expected Performance (Jetson AGX Orin)

| Model | Precision | Latency | FPS |
|-------|-----------|---------|-----|
| YOLO11n | FP16 | ~15ms | 60+ |
| LPRNet | FP16 | ~5ms | 200+ |
| **Total** | - | **~25ms** | **40 FPS** |

---

## Troubleshooting

### Error: "Failed to parse ONNX"

- Check ONNX opset version (use 12)
- Simplify ONNX graph with `onnx-simplifier`

### Error: "Out of memory"

- Reduce workspace size
- Use smaller batch size
- Enable FP16

### Low FPS

- Check GPU utilization with `jtop`
- Reduce resolution
- Optimize tracker config
