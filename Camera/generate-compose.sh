#!/bin/bash

CAMERA_COUNT=2

cat > docker-compose.yml <<EOF
services:
  rtsp_server:
    image: bluenviron/mediamtx:latest
    container_name: rtsp_server
    ports:
      - "8554:8554"  # Port RTSP cho Jetson
      - "8888:8888"  # Port HLS (để xem trên Web nếu cần)
    restart: unless-stopped
EOF

for ((i=1;i<=CAMERA_COUNT;i++))
do
cat >> docker-compose.yml <<EOF
  cam$i:
    build: .
    container_name: cam$i
    depends_on:
      - rtsp_server
    environment:
      - VIDEO_FILE=\${CAM${i}_VIDEO_FILE:-/videos/cam${i}.mp4}
      - RTSP_URL=\${CAM${i}_RTSP_URL:-rtsp://rtsp_server:8554/cam$i}
    volumes:
      - ./videos:/videos
    restart: unless-stopped

EOF

done

echo "Generated docker-compose.yml for ${CAMERA_COUNT} cameras"
