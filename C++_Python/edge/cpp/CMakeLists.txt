cmake_minimum_required(VERSION 3.10)
project(EdgeDeepStream)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# CUDA
find_package(CUDA REQUIRED)
include_directories(${CUDA_INCLUDE_DIRS})

# GStreamer
find_package(PkgConfig REQUIRED)
pkg_check_modules(GSTREAMER REQUIRED gstreamer-1.0)
pkg_check_modules(GSTREAMER_VIDEO REQUIRED gstreamer-video-1.0)
include_directories(${GSTREAMER_INCLUDE_DIRS})

# OpenCV
find_package(OpenCV REQUIRED)
include_directories(${OpenCV_INCLUDE_DIRS})

# DeepStream
set(DEEPSTREAM_DIR "/opt/nvidia/deepstream/deepstream")
include_directories(${DEEPSTREAM_DIR}/sources/includes)
link_directories(${DEEPSTREAM_DIR}/lib)

# Zenoh (optional)
# find_package(zenohc REQUIRED)

# Include directories
include_directories(${CMAKE_SOURCE_DIR}/include)

# Source files
set(SOURCES
    deepstream/ds_pipeline.cpp
    deepstream/ds_probes.cpp
    deepstream/python_api.cpp
    zenoh/zenoh_client.cpp
    zenoh/frame_serializer.cpp
)

# Create shared library for Python binding
add_library(deepstream_wrapper SHARED ${SOURCES})

target_link_libraries(deepstream_wrapper
    ${CUDA_LIBRARIES}
    ${GSTREAMER_LIBRARIES}
    ${OpenCV_LIBS}
    nvdsgst_meta
    nvds_meta
    nvdsgst_helper
    nvdsgst_smartrecord
    nvds_utils
    nvds_msgbroker
    nvdsinfer
    # zenohc::lib  # If using Zenoh
)

# Python bindings (pybind11)
# find_package(pybind11 REQUIRED)
# pybind11_add_module(deepstream_py deepstream_wrapper.cpp)
# target_link_libraries(deepstream_py PRIVATE deepstream_wrapper)
