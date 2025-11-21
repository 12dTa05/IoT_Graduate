"""
Load Balancer for Multi-Edge System
Monitors edge metrics and triggers offloading when needed
"""
import logging
from typing import Dict, Optional
from .mqtt_broker import CenterMQTTBroker

logger = logging.getLogger(__name__)


class LoadBalancer:
    """Automatic load balancing across edges"""
    
    def __init__(self, mqtt_client: CenterMQTTBroker):
        self.mqtt = mqtt_client
        self.edge_metrics: Dict[str, Dict] = {}
        
        # Thresholds for triggering offload
        self.threshold_gpu = 80.0
        self.threshold_cpu = 85.0
        
        # Offload configuration
        self.offload_ratio = 0.3  # Offload 30% of inference
        
        # Track active offloading
        self.active_offloads: Dict[str, str] = {}  # {source_edge: target_edge}
    
    def update_metrics(self, edge_id: str, metrics: Dict):
        """Update metrics for an edge and check if balancing needed"""
        self.edge_metrics[edge_id] = metrics
        
        # Auto-balance if enabled
        self.check_and_balance()
    
    def check_and_balance(self):
        """
        Check all edges and trigger offloading if needed
        Algorithm:
        1. Find overloaded edges (GPU > threshold OR CPU > threshold)
        2. For each overloaded edge, find least loaded edge
        3. Send offload command
        """
        for edge_id, metrics in self.edge_metrics.items():
            gpu_usage = metrics.get("gpu_usage", 0)
            cpu_usage = metrics.get("cpu_usage", 0)
            
            # Check if overloaded
            if gpu_usage > self.threshold_gpu or cpu_usage > self.threshold_cpu:
                # Skip if already offloading
                if edge_id in self.active_offloads:
                    continue
                
                # Find target edge
                target_edge = self.find_least_loaded_edge(exclude=edge_id)
                
                if target_edge:
                    logger.info(f"Triggering offload: {edge_id} -> {target_edge}")
                    self.start_offload(edge_id, target_edge)
            
            # Check if can stop offloading
            elif edge_id in self.active_offloads:
                if gpu_usage < self.threshold_gpu * 0.7 and cpu_usage < self.threshold_cpu * 0.7:
                    logger.info(f"Stopping offload from {edge_id}")
                    self.stop_offload(edge_id)
    
    def find_least_loaded_edge(self, exclude: Optional[str] = None) -> Optional[str]:
        """
        Find edge with lowest load
        Returns edge_id or None if no suitable edge found
        """
        candidates = {
            k: v for k, v in self.edge_metrics.items() 
            if k != exclude and v.get("gpu_usage", 100) < self.threshold_gpu * 0.6
        }
        
        if not candidates:
            return None
        
        # Return edge with lowest GPU usage
        return min(candidates, key=lambda k: candidates[k].get("gpu_usage", 100))
    
    def start_offload(self, source_edge: str, target_edge: str):
        """Send offload command to source edge"""
        command = {
            "action": "start_offload",
            "target_edge": target_edge,
            "offload_ratio": self.offload_ratio
        }
        
        self.mqtt.send_command(source_edge, command)
        self.active_offloads[source_edge] = target_edge
    
    def stop_offload(self, source_edge: str):
        """Stop offloading from source edge"""
        command = {
            "action": "stop_offload"
        }
        
        self.mqtt.send_command(source_edge, command)
        
        if source_edge in self.active_offloads:
            del self.active_offloads[source_edge]
    
    def manual_offload(self, source_edge: str, target_edge: str):
        """Manually trigger offload (from dashboard)"""
        logger.info(f"Manual offload: {source_edge} -> {target_edge}")
        self.start_offload(source_edge, target_edge)
    
    def get_offload_status(self) -> Dict:
        """Get current offloading status"""
        return {
            "active_offloads": self.active_offloads.copy(),
            "edge_count": len(self.edge_metrics),
            "overloaded_edges": [
                edge_id for edge_id, metrics in self.edge_metrics.items()
                if metrics.get("gpu_usage", 0) > self.threshold_gpu or 
                   metrics.get("cpu_usage", 0) > self.threshold_cpu
            ]
        }
