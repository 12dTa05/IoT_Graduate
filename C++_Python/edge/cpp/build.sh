#!/bin/bash
# Build script for C++ DeepStream components

set -e

echo "Building DeepStream C++ components..."

# Create build directory
cd "$(dirname "$0")"
mkdir -p build
cd build

# CMake configure
echo "Configuring with CMake..."
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CXX_STANDARD=14

# Build
echo "Building..."
make -j$(nproc)

echo "Build complete!"
echo "Library: $(pwd)/libdeepstream_wrapper.so"

# Copy to Python directory for easy access
cp libdeepstream_wrapper.so ../../python/core/

echo "Copied library to python/core/"
