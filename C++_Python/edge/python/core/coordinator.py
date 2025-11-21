"""
Offloading Coordinator
Decides when and where to offload inference based on system load
"""
import logging
from typing import Optional, Dict
import asyncio

logger = logging.getLogger(__name__)


class OffloadingCoordinator:
    """
    Coordinates inference offloading to other edges
    Makes decisions based on system load and available edges
    """
    
    def __init__(self, edge_id: str, system_monitor, zenoh_client=None):
        self.edge_id = edge_id
        self.monitor = system_monitor
        self.zenoh = zenoh_client
        
        # Offloading thresholds
        self.gpu_threshold = 80.0  # Start offloading when GPU > 80%
        self.cpu_threshold = 85.0  # Start offloading when CPU > 85%
        
        # Offloading state
        self.is_offloading = False
        self.target_edge = None
        self.offload_ratio = 0.0  # Percentage of frames to offload
        
        # Available edges (from discovery)
        self.available_edges: Dict[str, Dict] = {}
        
        # Frame counter for offloading
        self.frame_count = 0
    
    def should_offload(self) -> bool:
        """
        Decide if we should offload based on current system load
        
        Returns:
            True if should offload
        """
        if not self.monitor:
            return False
        
        metrics = self.monitor.get_metrics()
        gpu_usage = metrics.get('gpu_usage', 0)
        cpu_usage = metrics.get('cpu_usage', 0)
        
        # Check if overloaded
        is_overloaded = (gpu_usage > self.gpu_threshold or 
                        cpu_usage > self.cpu_threshold)
        
        if is_overloaded:
            logger.info(f"System overloaded: GPU={gpu_usage}%, CPU={cpu_usage}%")
            return True
        
        # If currently offloading, check if we can stop
        if self.is_offloading:
            # Stop offloading if load drops below 70% of threshold
            if (gpu_usage < self.gpu_threshold * 0.7 and 
                cpu_usage < self.cpu_threshold * 0.7):
                logger.info("Load decreased, stopping offload")
                self.stop_offloading()
        
        return False
    
    def find_target_edge(self) -> Optional[str]:
        """
        Find best edge to offload to
        
        Returns:
            Edge ID or None if no suitable edge found
        """
        if not self.available_edges:
            logger.warning("No available edges for offloading")
            return None
        
        # Find edge with lowest GPU usage
        best_edge = None
        lowest_gpu = 100.0
        
        for edge_id, info in self.available_edges.items():
            if edge_id == self.edge_id:
                continue  # Skip self
            
            gpu_usage = info.get('gpu_usage', 100)
            
            # Only consider edges with GPU < 50%
            if gpu_usage < 50 and gpu_usage < lowest_gpu:
                lowest_gpu = gpu_usage
                best_edge = edge_id
        
        if best_edge:
            logger.info(f"Selected {best_edge} for offloading (GPU: {lowest_gpu}%)")
        else:
            logger.warning("No suitable edge found (all busy)")
        
        return best_edge
    
    def start_offloading(self, target_edge: str, ratio: float = 0.3):
        """
        Start offloading to target edge
        
        Args:
            target_edge: Target edge ID
            ratio: Percentage of frames to offload (0.0-1.0)
        """
        self.is_offloading = True
        self.target_edge = target_edge
        self.offload_ratio = ratio
        self.frame_count = 0
        
        logger.info(f"Started offloading to {target_edge} (ratio: {ratio*100}%)")
    
    def stop_offloading(self):
        """Stop offloading"""
        if self.is_offloading:
            logger.info(f"Stopped offloading to {self.target_edge}")
            self.is_offloading = False
            self.target_edge = None
            self.offload_ratio = 0.0
    
    def should_offload_frame(self) -> bool:
        """
        Decide if current frame should be offloaded
        
        Returns:
            True if this frame should be offloaded
        """
        if not self.is_offloading:
            return False
        
        self.frame_count += 1
        
        # Offload every Nth frame based on ratio
        # e.g., ratio=0.3 means offload 3 out of 10 frames
        if self.offload_ratio >= 1.0:
            return True
        
        interval = int(1.0 / self.offload_ratio) if self.offload_ratio > 0 else 999999
        return (self.frame_count % interval) == 0
    
    def update_available_edges(self, edges: Dict[str, Dict]):
        """
        Update list of available edges from discovery
        
        Args:
            edges: Dict of edge_id -> edge_info
        """
        self.available_edges = edges
        logger.debug(f"Updated available edges: {list(edges.keys())}")
    
    async def auto_offload_loop(self):
        """
        Automatic offloading loop
        Continuously monitors load and adjusts offloading
        """
        while True:
            try:
                if self.should_offload() and not self.is_offloading:
                    # Find target and start offloading
                    target = self.find_target_edge()
                    if target:
                        self.start_offloading(target, ratio=0.3)
                
                await asyncio.sleep(5.0)  # Check every 5 seconds
                
            except Exception as e:
                logger.error(f"Error in auto offload loop: {e}")
                await asyncio.sleep(5.0)
    
    def get_status(self) -> Dict:
        """Get current offloading status"""
        return {
            "is_offloading": self.is_offloading,
            "target_edge": self.target_edge,
            "offload_ratio": self.offload_ratio,
            "available_edges": len(self.available_edges)
        }
