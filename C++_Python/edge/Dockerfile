FROM nvcr.io/nvidia/deepstream-l4t:6.4-triton-multiarch

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-dev \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libopencv-dev \
    libjsoncpp-dev \
    cmake \
    build-essential \
    git \
    wget \
    nano \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY edge/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Copy edge code
COPY edge /app/edge

# Build C++ components
WORKDIR /app/edge/cpp
RUN mkdir -p build && cd build && \
    cmake .. && \
    make -j$(nproc) && \
    cp libdeepstream_wrapper.so ../../python/core/ || true

# Copy sample video for testing (if available)
# You can mount your own video at runtime
RUN mkdir -p /app/videos

# Set environment variables
ENV PYTHONPATH=/app/edge:$PYTHONPATH
ENV LD_LIBRARY_PATH=/opt/nvidia/deepstream/deepstream/lib:$LD_LIBRARY_PATH

# Create simplified test script
WORKDIR /app/edge

# Expose ports
EXPOSE 8000 5173

# Default command - run standalone test
CMD ["python3", "main_edge_standalone.py"]
