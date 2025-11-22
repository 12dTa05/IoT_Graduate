"""
Standalone Edge Node for Testing
Runs DeepStream pipeline without MQTT/Zenoh dependencies
"""
import asyncio
import argparse
import logging
import signal
import sys
import time

# Performance optimization
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    logging.info("Using uvloop for better performance")
except ImportError:
    logging.warning("uvloop not available, using default event loop")

from python.core.deepstream_wrapper import DeepStreamPipeline
from python.core.shared_memory import get_shared_memory
from python.core.webrtc_signaling import get_signaling_server
from python.api import (
    app,
    set_shared_memory,
    set_signaling_server
)
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class StandaloneEdgeNode:
    """Standalone edge node for testing (no MQTT/Zenoh)"""
    
    def __init__(self, source_uri: str):
        self.source_uri = source_uri
        
        # Components
        self.pipeline = None
        self.shared_memory = None
        self.signaling_server = None
        
        # Stats
        self.frame_count = 0
        self.object_count = 0
        self.overspeed_count = 0
        self.start_time = None
        
        # Running flag
        self.running = False
    
    def initialize(self):
        """Initialize components"""
        logger.info("Initializing standalone edge node")
        
        # 1. Shared memory
        self.shared_memory = get_shared_memory()
        logger.info("Shared memory initialized")
        
        # 2. WebRTC signaling server
        self.signaling_server = get_signaling_server()
        logger.info("WebRTC signaling server initialized")
        
        # 3. DeepStream pipeline
        self.pipeline = DeepStreamPipeline()
        
        # Set metadata callback
        def on_metadata(objects):
            self.frame_count += 1
            self.object_count += len(objects)
            
            # Write to shared memory for WebSocket clients
            if self.shared_memory:
                self.shared_memory.write_metadata(objects)
            
            # Log detections
            for obj in objects:
                speed = obj.get('speed', 0)
                plate = obj.get('plate', '')
                track_id = obj.get('track_id', 0)
                
                if speed > 60:
                    self.overspeed_count += 1
                    logger.warning(
                        f"⚠️  OVERSPEED: ID={track_id}, Speed={speed:.1f} km/h, "
                        f"Plate={plate or 'N/A'}"
                    )
                elif speed > 0:
                    logger.info(
                        f"✓ Vehicle: ID={track_id}, Speed={speed:.1f} km/h, "
                        f"Plate={plate or 'N/A'}"
                    )
            
            # Print stats every 100 frames
            if self.frame_count % 100 == 0:
                elapsed = time.time() - self.start_time
                fps = self.frame_count / elapsed if elapsed > 0 else 0
                logger.info(
                    f"📊 Stats: Frames={self.frame_count}, "
                    f"Objects={self.object_count}, "
                    f"Overspeed={self.overspeed_count}, "
                    f"FPS={fps:.1f}"
                )
        
        self.pipeline.set_metadata_callback(on_metadata)
        
        # Build pipeline
        logger.info(f"Building pipeline with source: {self.source_uri}")
        success = self.pipeline.build(
            source_uri=self.source_uri,
            yolo_config="configs/dstest_yolo.txt",
            lpr_config="configs/dstest_lpr.txt",
            tracker_config="configs/config_tracker.txt",
            analytics_config="configs/config_nvdsanalytics.txt"
        )
        
        if not success:
            logger.error("Failed to build DeepStream pipeline")
            return False
        
        logger.info("✓ DeepStream pipeline built successfully")
        
        # Set global instances for FastAPI
        set_shared_memory(self.shared_memory)
        set_signaling_server(self.signaling_server)
        
        return True
    
    def start(self):
        """Start pipeline"""
        logger.info("Starting DeepStream pipeline")
        
        if not self.pipeline.start():
            logger.error("Failed to start DeepStream pipeline")
            return False
        
        self.start_time = time.time()
        self.running = True
        
        logger.info("✓ Pipeline started successfully")
        logger.info("=" * 60)
        logger.info("🎥 Edge Node is RUNNING")
        logger.info("=" * 60)
        logger.info(f"📹 Source: {self.source_uri}")
        logger.info(f"🌐 API: http://localhost:8000")
        logger.info(f"📊 Metrics: http://localhost:8000/api/status")
        logger.info(f"🔌 WebSocket Metadata: ws://localhost:8000/ws/metadata")
        logger.info(f"📡 WebRTC Signaling: ws://localhost:8000/ws/signaling")
        logger.info("=" * 60)
        
        return True
    
    def stop(self):
        """Stop pipeline"""
        logger.info("Stopping edge node")
        
        self.running = False
        
        if self.pipeline:
            self.pipeline.stop()
        
        if self.shared_memory:
            self.shared_memory.close()
        
        # Print final stats
        if self.start_time:
            elapsed = time.time() - self.start_time
            fps = self.frame_count / elapsed if elapsed > 0 else 0
            
            logger.info("=" * 60)
            logger.info("📊 FINAL STATISTICS")
            logger.info("=" * 60)
            logger.info(f"Total Frames: {self.frame_count}")
            logger.info(f"Total Objects: {self.object_count}")
            logger.info(f"Overspeed Alerts: {self.overspeed_count}")
            logger.info(f"Runtime: {elapsed:.1f} seconds")
            logger.info(f"Average FPS: {fps:.1f}")
            logger.info("=" * 60)
        
        logger.info("✓ Edge node stopped")


async def main(args):
    """Main entry point"""
    
    # Create edge node
    edge = StandaloneEdgeNode(source_uri=args.source)
    
    # Initialize
    if not edge.initialize():
        logger.error("Failed to initialize edge node")
        return 1
    
    # Start
    if not edge.start():
        logger.error("Failed to start edge node")
        return 1
    
    # Setup signal handlers
    def signal_handler(sig, frame):
        logger.info("Received shutdown signal")
        edge.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start FastAPI server in background
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=args.port,
        log_level="info"
    )
    server = uvicorn.Server(config)
    
    try:
        await server.serve()
    except KeyboardInterrupt:
        edge.stop()
    
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone Edge Node (No MQTT/Zenoh)")
    parser.add_argument(
        "--source",
        default="file:///app/videos/sample.mp4",
        help="Video source URI (file://, rtsp://, or v4l2://)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="FastAPI port"
    )
    
    args = parser.parse_args()
    
    # Print banner
    print("=" * 60)
    print("🚀 STANDALONE EDGE NODE - TEST MODE")
    print("=" * 60)
    print("📝 Configuration:")
    print(f"   Source: {args.source}")
    print(f"   API Port: {args.port}")
    print("=" * 60)
    print()
    
    exit_code = asyncio.run(main(args))
    sys.exit(exit_code)
