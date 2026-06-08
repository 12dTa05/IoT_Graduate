# Edge/speedflow_cpp/Makefile
#
# Builds speedflow_cpp.so — the C/C++ acceleration library for the
# SpeedFlow pipeline probes.
#
# Usage (on Jetson with JetPack):
#   make                      # default: auto-detect CUDA version
#   make CUDA_VER=12.2        # explicit CUDA version
#   make clean
#
# The resulting speedflow_cpp.so is loaded at runtime by
# speedflow_python/speedflow_c.py via ctypes.  Place it next to the
# Python package (Edge/speedflow_cpp/) or anywhere on LD_LIBRARY_PATH.

# ---------------------------------------------------------------------------
# CUDA version — read from nvcc if not provided
# ---------------------------------------------------------------------------
CUDA_VER ?= $(shell nvcc --version 2>/dev/null \
               | grep "release" \
               | sed 's/.*release \([0-9]*\.[0-9]*\).*/\1/')

ifeq ($(strip $(CUDA_VER)),)
    $(warning CUDA_VER not detected — set it explicitly: make CUDA_VER=12.2)
    CUDA_VER := 12.2
endif

CUDA_PATH := /usr/local/cuda-$(CUDA_VER)

# ---------------------------------------------------------------------------
# Compiler / archiver
# ---------------------------------------------------------------------------
CXX  := g++
AR   := ar

TARGET := speedflow_cpp.so

SRCS := speedflow.cpp plate_enhance.cpp

# ---------------------------------------------------------------------------
# Include / library paths
# ---------------------------------------------------------------------------
# OpenCV — use pkg-config; fall back to common JetPack locations
OPENCV_CFLAGS := $(shell pkg-config --cflags opencv4 2>/dev/null \
                          || pkg-config --cflags opencv 2>/dev/null \
                          || echo "-I/usr/include/opencv4")
OPENCV_LIBS   := $(shell pkg-config --libs opencv4 2>/dev/null \
                          || pkg-config --libs opencv 2>/dev/null \
                          || echo "-lopencv_core -lopencv_imgproc")

CXXFLAGS := \
    -std=c++14              \
    -O3                     \
    -march=armv8-a+simd     \
    -fPIC                   \
    -Wall                   \
    -Wextra                 \
    -Wno-unused-parameter   \
    -I.                     \
    $(OPENCV_CFLAGS)

LDFLAGS  := \
    -shared                 \
    -Wl,-soname,$(TARGET)   \
    $(OPENCV_LIBS)          \
    -lstdc++                \
    -lm

# ---------------------------------------------------------------------------
# Build rules
# ---------------------------------------------------------------------------
OBJS := $(SRCS:.cpp=.o)

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CXX) -o $@ $^ $(LDFLAGS)
	@echo "[speedflow_cpp] Built $(TARGET) (CUDA_VER=$(CUDA_VER))"

%.o: %.cpp speedflow.h Makefile
	$(CXX) -c $(CXXFLAGS) -o $@ $<

clean:
	rm -f $(OBJS) $(TARGET)
	@echo "[speedflow_cpp] Cleaned."
