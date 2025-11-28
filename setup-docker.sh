#!/bin/bash
# Setup script for Traffic Monitor Docker (File Display Mode)

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== Traffic Monitor Docker Setup (File Display Mode) ===${NC}"
echo ""

# Step 1: Check Docker
echo -e "${YELLOW}[1/5] Checking Docker installation...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "${RED}ERROR: Docker is not installed${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker found: $(docker --version)${NC}"

# Step 2: Check NVIDIA runtime
echo -e "${YELLOW}[2/5] Checking NVIDIA runtime...${NC}"
if ! docker run --rm --runtime nvidia nvcr.io/nvidia/l4t-base:r35.1.0 nvidia-smi &> /dev/null; then
    echo -e "${RED}ERROR: NVIDIA runtime not available${NC}"
    echo "Please install nvidia-docker2"
    exit 1
fi
echo -e "${GREEN}✓ NVIDIA runtime OK${NC}"

# Step 3: Create directories
echo -e "${YELLOW}[3/5] Creating directories...${NC}"
mkdir -p test_videos
mkdir -p logs/overspeed_snaps
mkdir -p output
mkdir -p configs
echo -e "${GREEN}✓ Directories created${NC}"

# Step 4: Setup X11
echo -e "${YELLOW}[4/5] Setting up X11 permissions...${NC}"
if [ -z "$DISPLAY" ]; then
    echo -e "${RED}WARNING: DISPLAY not set, using :0${NC}"
    export DISPLAY=:0
fi

# Allow Docker to access X server
xhost +local:docker > /dev/null 2>&1 || {
    echo -e "${YELLOW}WARNING: Could not run xhost (X server might not be running)${NC}"
}

# Create X authority file
touch /tmp/.docker.xauth
xauth nlist $DISPLAY 2>/dev/null | sed -e 's/^..../ffff/' | xauth -f /tmp/.docker.xauth nmerge - 2>/dev/null || {
    echo -e "${YELLOW}WARNING: Could not create X authority file${NC}"
}
echo -e "${GREEN}✓ X11 setup complete${NC}"

# Step 5: Create .env file
echo -e "${YELLOW}[5/5] Creating .env file...${NC}"
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo -e "${GREEN}✓ .env file created from template${NC}"
    else
        cat > .env << EOF
# Video file path (inside container)
VIDEO_FILE=/app/test_videos/test.mp4

# Display
DISPLAY=:0

# CUDA device
CUDA_VISIBLE_DEVICES=0
EOF
        echo -e "${GREEN}✓ .env file created${NC}"
    fi
else
    echo -e "${GREEN}✓ .env file already exists${NC}"
fi

echo ""
echo -e "${GREEN}=== Setup Complete! ===${NC}"
echo ""
echo "Next steps:"
echo "1. Copy your video file to test_videos/ directory:"
echo "   ${YELLOW}cp /path/to/video.mp4 test_videos/test.mp4${NC}"
echo ""
echo "2. Build and run the container:"
echo "   ${YELLOW}docker-compose up --build${NC}"
echo ""
echo "3. Or use the helper script:"
echo "   ${YELLOW}./docker-run.sh build${NC}"
echo "   ${YELLOW}./docker-run.sh run${NC}"
echo ""
