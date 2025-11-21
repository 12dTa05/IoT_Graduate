"""
System Monitor for Jetson using jtop
Monitors CPU, GPU, RAM, Temperature, and Power consumption
"""
from jtop import jtop
import asyncio
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class SystemMonitor:
    """Monitor Jetson system metrics using jtop"""
    
    def __init__(self, offload_gpu_threshold: float = 80.0, offload_cpu_threshold: float = 85.0):
        self.jetson: Optional[jtop] = None
        self.metrics: Dict = {}
        self.offload_gpu_threshold = offload_gpu_threshold
        self.offload_cpu_threshold = offload_cpu_threshold
        self._running = False
    
    async def start(self):
        """Start monitoring loop"""
        self._running = True
        try:
            with jtop() as self.jetson:
                while self._running:
                    if self.jetson.ok():
                        self.metrics = {
                            "cpu_usage": self.jetson.cpu["total"]["user"] + self.jetson.cpu["total"]["system"],
                            "gpu_usage": self.jetson.gpu["gpu"]["status"]["load"] if "gpu" in self.jetson.gpu else 0,
                            "ram_usage": (self.jetson.memory["RAM"]["used"] / self.jetson.memory["RAM"]["tot"]) * 100,
                            "temp": self.jetson.temperature.get("GPU", 0),
                            "power": self.jetson.power["tot"]["power"] if "tot" in self.jetson.power else 0,
                            "timestamp": asyncio.get_event_loop().time()
                        }
                    await asyncio.sleep(1.0)
        except Exception as e:
            logger.error(f"Monitor error: {e}")
    
    def stop(self):
        """Stop monitoring"""
        self._running = False
    
    def get_metrics(self) -> Dict:
        """Get current metrics"""
        return self.metrics.copy()
    
    def should_offload(self) -> bool:
        """
        Decision logic: offload if GPU > threshold OR CPU > threshold
        Returns True if system is overloaded
        """
        gpu_usage = self.metrics.get("gpu_usage", 0)
        cpu_usage = self.metrics.get("cpu_usage", 0)
        
        return gpu_usage > self.offload_gpu_threshold or cpu_usage > self.offload_cpu_threshold
    
    def get_load_level(self) -> str:
        """Get load level: low, medium, high, critical"""
        gpu = self.metrics.get("gpu_usage", 0)
        cpu = self.metrics.get("cpu_usage", 0)
        max_load = max(gpu, cpu)
        
        if max_load < 50:
            return "low"
        elif max_load < 70:
            return "medium"
        elif max_load < 85:
            return "high"
        else:
            return "critical"
