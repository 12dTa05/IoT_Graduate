FROM nvcr.io/nvidia/deepstream-l4t:6.4-triton-multiarch

# Metadata
LABEL maintainer="IoT Graduate Project"
LABEL description="Traffic Monitoring System with DeepStream 6.4 + YOLO + WebRTC"

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-gi \
    python3-dev \
    python3-numpy \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstreamer-plugins-bad1.0-dev \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-tools \
    gstreamer1.0-x \
    gstreamer1.0-alsa \
    gstreamer1.0-gl \
    gstreamer1.0-gtk3 \
    gstreamer1.0-qt5 \
    gstreamer1.0-pulseaudio \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY speedflow/ ./speedflow/
COPY configs/ ./configs/
COPY DeepStream-YoLo/ ./DeepStream-YoLo/
COPY deepstream_python_apps/ ./deepstream_python_apps/
COPY run_webrtc.py run_RTSP.py run_file.py ./

# Create directories for outputs
RUN mkdir -p /app/logs /app/logs/overspeed_snaps /app/output

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libgomp.so.1
ENV DEEPSTREAM_PATH=/opt/nvidia/deepstream/deepstream
ENV GST_PLUGIN_PATH=/opt/nvidia/deepstream/deepstream/lib/gst-plugins/

# Add DeepStream Python bindings to PYTHONPATH
ENV PYTHONPATH=/opt/nvidia/deepstream/deepstream/lib:${PYTHONPATH}

# Copy entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Default entrypoint
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default command (can be overridden)
CMD ["python3", "run_webrtc.py"]
