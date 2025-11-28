FROM nvcr.io/nvidia/deepstream-l4t:6.4-triton-multiarch

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-dev \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gst-rtsp-server-1.0 \
    libgstrtspserver-1.0-0 \
    libgirepository1.0-dev \
    libcairo2-dev \
    pkg-config \
    git \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
RUN pip3 install --no-cache-dir -r requirements.txt

# Verify pyds is available (should be pre-installed in DeepStream 6.4 image)
# If not available, we'll install from the samples directory
RUN python3 -c "import pyds" 2>/dev/null || \
    (echo "pyds not found in base image, checking for pre-built wheel..." && \
     find /opt/nvidia/deepstream -name "pyds*.whl" -exec pip3 install {} \; || \
     echo "Warning: pyds installation may need manual setup")

# Copy application code
COPY speedflow/ /app/speedflow/
COPY configs/ /app/configs/
COPY DeepStream-YoLo/ /app/DeepStream-YoLo/
COPY run_file.py /app/
COPY config_cam.txt /app/

# Create necessary directories
RUN mkdir -p /app/logs /app/output /app/logs/overspeed_snaps /app/models /app/videos

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV GST_DEBUG=2
ENV CUDA_VER=11.4
ENV LD_LIBRARY_PATH=/opt/nvidia/deepstream/deepstream/lib:$LD_LIBRARY_PATH

# Copy entrypoint script
COPY docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["--help"]
