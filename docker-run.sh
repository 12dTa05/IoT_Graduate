#!/bin/bash
# Helper script to build and run Traffic Monitor Docker container

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
IMAGE_NAME="traffic-monitor"
IMAGE_TAG="latest"
CONTAINER_NAME="traffic-monitor"
ACTION=""

# Print usage
usage() {
    echo "Usage: $0 [build|run|stop|logs|shell|clean]"
    echo ""
    echo "Commands:"
    echo "  build   - Build Docker image"
    echo "  run     - Run container (interactive)"
    echo "  start   - Start container (detached)"
    echo "  stop    - Stop container"
    echo "  logs    - Show container logs"
    echo "  shell   - Open shell in running container"
    echo "  clean   - Remove container and image"
    echo ""
    exit 1
}

# Check if .env file exists
check_env() {
    if [ ! -f .env ]; then
        echo -e "${YELLOW}Warning: .env file not found${NC}"
        echo "Creating .env from .env.example..."
        if [ -f .env.example ]; then
            cp .env.example .env
            echo -e "${GREEN}.env file created. Please edit it with your configuration.${NC}"
            exit 0
        else
            echo -e "${RED}Error: .env.example not found${NC}"
            exit 1
        fi
    fi
}

# Build Docker image
build_image() {
    echo -e "${GREEN}Building Docker image: ${IMAGE_NAME}:${IMAGE_TAG}${NC}"
    docker build -t ${IMAGE_NAME}:${IMAGE_TAG} .
    echo -e "${GREEN}Build completed successfully!${NC}"
}

# Run container interactively
run_container() {
    check_env
    source .env
    
    echo -e "${GREEN}Running container: ${CONTAINER_NAME}${NC}"
    echo "Configuration:"
    echo "  VIDEO_SOURCE: ${VIDEO_SOURCE}"
    echo "  WEBRTC_SERVER: ${WEBRTC_SERVER}"
    echo "  WEBRTC_ROOM: ${WEBRTC_ROOM}"
    echo "  CONFIG_FILE: ${CONFIG_FILE}"
    echo ""
    
    docker run -it --rm \
        --name ${CONTAINER_NAME} \
        --runtime nvidia \
        --network host \
        -v $(pwd)/configs:/app/configs:ro \
        -v $(pwd)/DeepStream-YoLo:/app/DeepStream-YoLo:ro \
        -v $(pwd)/logs:/app/logs \
        -v $(pwd)/output:/app/output \
        -e VIDEO_SOURCE="${VIDEO_SOURCE}" \
        -e WEBRTC_SERVER="${WEBRTC_SERVER}" \
        -e WEBRTC_ROOM="${WEBRTC_ROOM}" \
        -e CONFIG_FILE="${CONFIG_FILE}" \
        -e CUDA_VISIBLE_DEVICES=0 \
        ${IMAGE_NAME}:${IMAGE_TAG}
}

# Start container in background
start_container() {
    check_env
    source .env
    
    echo -e "${GREEN}Starting container: ${CONTAINER_NAME}${NC}"
    
    docker run -d \
        --name ${CONTAINER_NAME} \
        --runtime nvidia \
        --network host \
        --restart unless-stopped \
        -v $(pwd)/configs:/app/configs:ro \
        -v $(pwd)/DeepStream-YoLo:/app/DeepStream-YoLo:ro \
        -v $(pwd)/logs:/app/logs \
        -v $(pwd)/output:/app/output \
        -e VIDEO_SOURCE="${VIDEO_SOURCE}" \
        -e WEBRTC_SERVER="${WEBRTC_SERVER}" \
        -e WEBRTC_ROOM="${WEBRTC_ROOM}" \
        -e CONFIG_FILE="${CONFIG_FILE}" \
        -e CUDA_VISIBLE_DEVICES=0 \
        ${IMAGE_NAME}:${IMAGE_TAG}
    
    echo -e "${GREEN}Container started successfully!${NC}"
    echo "Use '$0 logs' to view logs"
}

# Stop container
stop_container() {
    echo -e "${YELLOW}Stopping container: ${CONTAINER_NAME}${NC}"
    docker stop ${CONTAINER_NAME} 2>/dev/null || echo "Container not running"
    docker rm ${CONTAINER_NAME} 2>/dev/null || echo "Container already removed"
    echo -e "${GREEN}Container stopped${NC}"
}

# Show logs
show_logs() {
    echo -e "${GREEN}Showing logs for: ${CONTAINER_NAME}${NC}"
    docker logs -f ${CONTAINER_NAME}
}

# Open shell in container
open_shell() {
    echo -e "${GREEN}Opening shell in: ${CONTAINER_NAME}${NC}"
    docker exec -it ${CONTAINER_NAME} /bin/bash
}

# Clean up
clean_all() {
    echo -e "${YELLOW}Cleaning up...${NC}"
    stop_container
    echo "Removing image: ${IMAGE_NAME}:${IMAGE_TAG}"
    docker rmi ${IMAGE_NAME}:${IMAGE_TAG} 2>/dev/null || echo "Image already removed"
    echo -e "${GREEN}Cleanup completed${NC}"
}

# Main script
if [ $# -eq 0 ]; then
    usage
fi

ACTION=$1

case $ACTION in
    build)
        build_image
        ;;
    run)
        run_container
        ;;
    start)
        start_container
        ;;
    stop)
        stop_container
        ;;
    logs)
        show_logs
        ;;
    shell)
        open_shell
        ;;
    clean)
        clean_all
        ;;
    *)
        echo -e "${RED}Unknown command: $ACTION${NC}"
        usage
        ;;
esac
