"""
Updated main_edge.py with DeepStream integration
"""
import asyncio
import argparse
import logging
import uvicorn
from pathlib import Path

from python.core.monitor import SystemMonitor
from python.core.mqtt_client import EdgeMQTTClient
from python.core.deepstream_wrapper import DeepStreamPipeline
from python.api.main import app, set_system_monitor, set_mqtt_client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global pipeline instance
pipeline: DeepStreamPipeline = None


async def main():
    parser = argparse.ArgumentParser(description="Edge Node Runner")
    parser.add_argument("--edge-id", required=True, help="Edge ID (e.g., edge_1)")
    parser.add_argument("--mqtt-broker", default="192.168.1.100", help="MQTT broker IP")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--api-port", type=int, default=8000, help="FastAPI port")
    parser.add_argument("--source", help="RTSP URL or video file path")
    parser.add_argument("--yolo-config", default="configs/dstest_yolo.txt")
    parser.add_argument("--lpr-config", default="configs/dstest_lpr.txt")
    parser.add_argument("--tracker-config", default="configs/config_tracker.txt")
    parser.add_argument("--analytics-config", default="configs/config_nvdsanalytics.txt")
    
    args = parser.parse_args()
    
    logger.info(f"Starting Edge Node: {args.edge_id}")
    
    # Initialize system monitor
    monitor = SystemMonitor(offload_gpu_threshold=80.0, offload_cpu_threshold=85.0)
    set_system_monitor(monitor)
    
    # Initialize MQTT client
    mqtt = EdgeMQTTClient(args.edge_id, args.mqtt_broker, args.mqtt_port)
    set_mqtt_client(mqtt)
    
    # Register MQTT command handlers
    def handle_start_offload(payload):
        target_edge = payload.get("target_edge")
        logger.info(f"Received offload command to {target_edge}")
        # TODO: Start offloading via Zenoh
    
    def handle_stop_offload(payload):
        logger.info("Received stop offload command")
        # TODO: Stop offloading
    
    mqtt.register_command_handler("start_offload", handle_start_offload)
    mqtt.register_command_handler("stop_offload", handle_stop_offload)
    
    # Connect MQTT
    mqtt.connect()
    
    # Start system monitor
    monitor_task = asyncio.create_task(monitor.start())
    
    # Publish metrics periodically
    async def publish_metrics_loop():
        while True:
            metrics = monitor.get_metrics()
            mqtt.publish_metrics(metrics)
            await asyncio.sleep(2.0)
    
    metrics_task = asyncio.create_task(publish_metrics_loop())
    
    # Initialize DeepStream pipeline if source provided
    global pipeline
    if args.source:
        try:
            logger.info("Initializing DeepStream pipeline...")
            pipeline = DeepStreamPipeline()
            
            # Metadata callback
            def on_metadata(objects):
                # Send to WebSocket clients
                # Check for overspeed
                for obj in objects:
                    if obj.get('speed', 0) > 60:
                        mqtt.publish_alert("overspeed", {
                            "track_id": obj['track_id'],
                            "speed": obj['speed'],
                            "plate": obj.get('plate', ''),
                            "timestamp": asyncio.get_event_loop().time()
                        })
            
            pipeline.set_metadata_callback(on_metadata)
            
            # Build pipeline
            success = pipeline.build(
                source_uri=args.source,
                yolo_config=args.yolo_config,
                lpr_config=args.lpr_config,
                tracker_config=args.tracker_config,
                analytics_config=args.analytics_config
            )
            
            if success:
                # Start pipeline
                pipeline.start()
                logger.info("DeepStream pipeline started")
            else:
                logger.error("Failed to build pipeline")
                pipeline = None
        
        except Exception as e:
            logger.error(f"Failed to initialize pipeline: {e}")
            pipeline = None
    
    # Start FastAPI server
    logger.info(f"Starting FastAPI server on port {args.api_port}")
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=args.api_port,
        log_level="info"
    )
    server = uvicorn.Server(config)
    
    try:
        await server.serve()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        # Cleanup
        if pipeline:
            pipeline.stop()
        monitor.stop()
        mqtt.disconnect()
        await monitor_task
        await metrics_task


if __name__ == "__main__":
    asyncio.run(main())
