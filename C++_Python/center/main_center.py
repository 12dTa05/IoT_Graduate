"""
Main entry point for Center Server
Starts MQTT broker, load balancer, and FastAPI server
"""
import asyncio
import argparse
import logging
import uvicorn

from backend.core.mqtt_broker import CenterMQTTBroker
from backend.core.balancer import LoadBalancer
from backend.api.main import app, set_mqtt_broker, set_load_balancer, add_alert

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser(description="Center Server Runner")
    parser.add_argument("--mqtt-broker", default="localhost", help="MQTT broker IP")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--api-port", type=int, default=8080, help="FastAPI port")
    
    args = parser.parse_args()
    
    logger.info("Starting Center Server")
    
    # Initialize MQTT broker
    mqtt = CenterMQTTBroker(args.mqtt_broker, args.mqtt_port)
    set_mqtt_broker(mqtt)
    
    # Initialize load balancer
    balancer = LoadBalancer(mqtt)
    set_load_balancer(balancer)
    
    # Register MQTT callbacks
    def on_status_update(edge_id: str, metrics: dict):
        logger.debug(f"Received metrics from {edge_id}: GPU={metrics.get('gpu_usage')}%")
        balancer.update_metrics(edge_id, metrics)
    
    def on_alert_received(edge_id: str, alert_data: dict):
        logger.info(f"Alert from {edge_id}: {alert_data.get('type')}")
        add_alert(edge_id, alert_data)
    
    mqtt.on_status(on_status_update)
    mqtt.on_alert(on_alert_received)
    
    # Connect MQTT
    mqtt.connect()
    
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
        mqtt.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
