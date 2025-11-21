"""
MQTT Broker Handler for Center Server
Subscribes to all edge topics and processes messages
"""
import paho.mqtt.client as mqtt
import json
import logging
from typing import Callable, Dict, List
from collections import defaultdict

logger = logging.getLogger(__name__)


class CenterMQTTBroker:
    """MQTT subscriber for center server"""
    
    def __init__(self, broker_host: str = "localhost", broker_port: int = 1883):
        self.broker_host = broker_host
        self.broker_port = broker_port
        
        self.client = mqtt.Client(client_id="center_server")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        
        # Callbacks for different message types
        self.callbacks: Dict[str, List[Callable]] = defaultdict(list)
        
        # Store latest metrics from each edge
        self.edge_metrics: Dict[str, Dict] = {}
        
        self.connected = False
    
    def connect(self):
        """Connect to MQTT broker"""
        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            logger.info(f"Center connecting to MQTT broker at {self.broker_host}:{self.broker_port}")
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
    
    def disconnect(self):
        """Disconnect from broker"""
        self.client.loop_stop()
        self.client.disconnect()
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback when connected"""
        if rc == 0:
            self.connected = True
            logger.info("Center connected to MQTT broker")
            
            # Subscribe to all edge topics
            self.client.subscribe("edge/+/status")
            self.client.subscribe("edge/+/alert")
            logger.info("Subscribed to edge topics")
        else:
            logger.error(f"Connection failed with code {rc}")
    
    def _on_message(self, client, userdata, msg):
        """Handle incoming messages from edges"""
        try:
            topic_parts = msg.topic.split('/')
            edge_id = topic_parts[1]
            msg_type = topic_parts[2]  # 'status' or 'alert'
            
            payload = json.loads(msg.payload.decode())
            
            # Store metrics if status message
            if msg_type == "status":
                self.edge_metrics[edge_id] = payload
            
            # Trigger callbacks
            for callback in self.callbacks[msg_type]:
                callback(edge_id, payload)
                
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    
    def on_status(self, callback: Callable):
        """
        Register callback for status messages
        Callback signature: callback(edge_id: str, metrics: Dict)
        """
        self.callbacks["status"].append(callback)
    
    def on_alert(self, callback: Callable):
        """
        Register callback for alert messages
        Callback signature: callback(edge_id: str, alert_data: Dict)
        """
        self.callbacks["alert"].append(callback)
    
    def send_command(self, edge_id: str, command: Dict):
        """
        Send command to specific edge
        Topic: center/command/{edge_id}
        """
        if not self.connected:
            logger.warning("Not connected to MQTT broker")
            return
        
        topic = f"center/command/{edge_id}"
        payload = json.dumps(command)
        
        self.client.publish(topic, payload, qos=1)
        logger.info(f"Sent command to {edge_id}: {command.get('action')}")
    
    def get_edge_metrics(self, edge_id: str) -> Dict:
        """Get latest metrics for specific edge"""
        return self.edge_metrics.get(edge_id, {})
    
    def get_all_metrics(self) -> Dict[str, Dict]:
        """Get metrics from all edges"""
        return self.edge_metrics.copy()
