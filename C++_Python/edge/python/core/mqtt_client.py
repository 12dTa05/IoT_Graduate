"""
MQTT Client for Edge Node
Publishes metrics and alerts to center server
Subscribes to commands from center
"""
import paho.mqtt.client as mqtt
import json
import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class EdgeMQTTClient:
    """MQTT client for edge node communication"""
    
    def __init__(self, edge_id: str, broker_host: str, broker_port: int = 1883):
        self.edge_id = edge_id
        self.broker_host = broker_host
        self.broker_port = broker_port
        
        self.client = mqtt.Client(client_id=f"edge_{edge_id}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        
        self.command_callbacks: Dict[str, Callable] = {}
        self.connected = False
    
    def connect(self):
        """Connect to MQTT broker"""
        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            logger.info(f"Connecting to MQTT broker at {self.broker_host}:{self.broker_port}")
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
    
    def disconnect(self):
        """Disconnect from broker"""
        self.client.loop_stop()
        self.client.disconnect()
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback when connected to broker"""
        if rc == 0:
            self.connected = True
            logger.info(f"Edge {self.edge_id} connected to MQTT broker")
            # Subscribe to command topic
            self.client.subscribe(f"center/command/{self.edge_id}")
            logger.info(f"Subscribed to center/command/{self.edge_id}")
        else:
            logger.error(f"Connection failed with code {rc}")
    
    def _on_disconnect(self, client, userdata, rc):
        """Callback when disconnected"""
        self.connected = False
        logger.warning(f"Disconnected from MQTT broker (rc={rc})")
    
    def _on_message(self, client, userdata, msg):
        """Handle incoming messages"""
        try:
            payload = json.loads(msg.payload.decode())
            action = payload.get("action")
            
            logger.info(f"Received command: {action}")
            
            # Call registered callback
            if action in self.command_callbacks:
                self.command_callbacks[action](payload)
            else:
                logger.warning(f"No handler for action: {action}")
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    
    def publish_metrics(self, metrics: Dict):
        """
        Publish system metrics to center
        Topic: edge/{edge_id}/status
        """
        if not self.connected:
            return
        
        topic = f"edge/{self.edge_id}/status"
        payload = json.dumps({
            **metrics,
            "edge_id": self.edge_id
        })
        
        self.client.publish(topic, payload, qos=0)
    
    def publish_alert(self, alert_type: str, data: Dict):
        """
        Publish alerts (overspeed, license plate detection)
        Topic: edge/{edge_id}/alert
        QoS: 1 (at least once delivery)
        """
        if not self.connected:
            return
        
        topic = f"edge/{self.edge_id}/alert"
        payload = json.dumps({
            "type": alert_type,
            "edge_id": self.edge_id,
            "data": data
        })
        
        self.client.publish(topic, payload, qos=1)
        logger.info(f"Published alert: {alert_type}")
    
    def register_command_handler(self, action: str, callback: Callable):
        """
        Register callback for specific command action
        
        Args:
            action: Command action name (e.g., "start_offload")
            callback: Function to call when command received
        """
        self.command_callbacks[action] = callback
        logger.info(f"Registered handler for action: {action}")
