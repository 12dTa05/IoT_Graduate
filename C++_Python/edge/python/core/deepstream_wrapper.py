"""
Python wrapper for C++ DeepStream pipeline
Uses ctypes to call C++ shared library
"""
import ctypes
import os
import json
from typing import Callable, Optional, Dict, List
import logging

logger = logging.getLogger(__name__)


class DeepStreamPipeline:
    """
    Python wrapper for C++ DeepStream pipeline
    Provides high-level interface to start/stop pipeline and receive metadata
    """
    
    def __init__(self, lib_path: str = None):
        """
        Initialize pipeline wrapper
        
        Args:
            lib_path: Path to libdeepstream_wrapper.so
        """
        if lib_path is None:
            # Default path
            lib_path = os.path.join(
                os.path.dirname(__file__),
                "../../cpp/build/libdeepstream_wrapper.so"
            )
        
        if not os.path.exists(lib_path):
            raise FileNotFoundError(f"DeepStream library not found: {lib_path}")
        
        # Load shared library
        self.lib = ctypes.CDLL(lib_path)
        
        # Define function signatures
        self._setup_functions()
        
        # Pipeline handle
        self.pipeline_handle = None
        
        # Metadata callback
        self.metadata_callback: Optional[Callable[[List[Dict]], None]] = None
        
        # C callback wrapper
        self.c_callback = ctypes.CFUNCTYPE(None, ctypes.c_char_p)(
            self._metadata_callback_wrapper
        )
    
    def _setup_functions(self):
        """Setup C function signatures"""
        # Pipeline* pipeline_create()
        self.lib.pipeline_create.argtypes = []
        self.lib.pipeline_create.restype = ctypes.c_void_p
        
        # void pipeline_destroy(Pipeline* p)
        self.lib.pipeline_destroy.argtypes = [ctypes.c_void_p]
        self.lib.pipeline_destroy.restype = None
        
        # bool pipeline_build(Pipeline* p, const char* source, ...)
        self.lib.pipeline_build.argtypes = [
            ctypes.c_void_p,  # pipeline
            ctypes.c_char_p,  # source_uri
            ctypes.c_char_p,  # yolo_config
            ctypes.c_char_p,  # lpr_config
            ctypes.c_char_p,  # tracker_config
            ctypes.c_char_p,  # analytics_config
        ]
        self.lib.pipeline_build.restype = ctypes.c_bool
        
        # bool pipeline_start(Pipeline* p)
        self.lib.pipeline_start.argtypes = [ctypes.c_void_p]
        self.lib.pipeline_start.restype = ctypes.c_bool
        
        # void pipeline_stop(Pipeline* p)
        self.lib.pipeline_stop.argtypes = [ctypes.c_void_p]
        self.lib.pipeline_stop.restype = None
        
        # bool pipeline_is_running(Pipeline* p)
        self.lib.pipeline_is_running.argtypes = [ctypes.c_void_p]
        self.lib.pipeline_is_running.restype = ctypes.c_bool
        
        # void pipeline_set_callback(Pipeline* p, callback_fn)
        self.lib.pipeline_set_callback.argtypes = [
            ctypes.c_void_p,
            ctypes.CFUNCTYPE(None, ctypes.c_char_p)
        ]
        self.lib.pipeline_set_callback.restype = None
    
    def build(
        self,
        source_uri: str,
        yolo_config: str,
        lpr_config: str,
        tracker_config: str,
        analytics_config: str
    ) -> bool:
        """
        Build DeepStream pipeline
        
        Args:
            source_uri: RTSP URL or file path
            yolo_config: Path to YOLO config file
            lpr_config: Path to LPRNet config file
            tracker_config: Path to tracker config
            analytics_config: Path to analytics config
            
        Returns:
            True if successful
        """
        # Create pipeline
        self.pipeline_handle = self.lib.pipeline_create()
        if not self.pipeline_handle:
            logger.error("Failed to create pipeline")
            return False
        
        # Set callback
        self.lib.pipeline_set_callback(self.pipeline_handle, self.c_callback)
        
        # Build pipeline
        success = self.lib.pipeline_build(
            self.pipeline_handle,
            source_uri.encode('utf-8'),
            yolo_config.encode('utf-8'),
            lpr_config.encode('utf-8'),
            tracker_config.encode('utf-8'),
            analytics_config.encode('utf-8')
        )
        
        if not success:
            logger.error("Failed to build pipeline")
            self.lib.pipeline_destroy(self.pipeline_handle)
            self.pipeline_handle = None
            return False
        
        logger.info("Pipeline built successfully")
        return True
    
    def start(self) -> bool:
        """
        Start pipeline
        
        Returns:
            True if successful
        """
        if not self.pipeline_handle:
            logger.error("Pipeline not built")
            return False
        
        success = self.lib.pipeline_start(self.pipeline_handle)
        if success:
            logger.info("Pipeline started")
        else:
            logger.error("Failed to start pipeline")
        
        return success
    
    def stop(self):
        """Stop pipeline"""
        if self.pipeline_handle:
            self.lib.pipeline_stop(self.pipeline_handle)
            logger.info("Pipeline stopped")
    
    def is_running(self) -> bool:
        """Check if pipeline is running"""
        if not self.pipeline_handle:
            return False
        return self.lib.pipeline_is_running(self.pipeline_handle)
    
    def set_metadata_callback(self, callback: Callable[[List[Dict]], None]):
        """
        Set callback for metadata
        
        Args:
            callback: Function that receives list of object dictionaries
        """
        self.metadata_callback = callback
    
    def _metadata_callback_wrapper(self, json_str: bytes):
        """
        Internal callback wrapper
        Converts C string to Python objects and calls user callback
        """
        if not self.metadata_callback:
            return
        
        try:
            # Parse JSON
            metadata = json.loads(json_str.decode('utf-8'))
            
            # Call user callback
            self.metadata_callback(metadata)
        except Exception as e:
            logger.error(f"Error in metadata callback: {e}")
    
    def __del__(self):
        """Cleanup"""
        if self.pipeline_handle:
            self.stop()
            self.lib.pipeline_destroy(self.pipeline_handle)


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    def on_metadata(objects: List[Dict]):
        """Callback for metadata"""
        for obj in objects:
            if obj['speed'] > 60:
                print(f"Overspeed detected: Track {obj['track_id']} - {obj['speed']} km/h - Plate: {obj['plate']}")
    
    # Create pipeline
    pipeline = DeepStreamPipeline()
    
    # Build
    pipeline.build(
        source_uri="file:///path/to/video.mp4",
        yolo_config="configs/dstest_yolo.txt",
        lpr_config="configs/dstest_lpr.txt",
        tracker_config="configs/config_tracker.txt",
        analytics_config="configs/config_nvdsanalytics.txt"
    )
    
    # Set callback
    pipeline.set_metadata_callback(on_metadata)
    
    # Start
    pipeline.start()
    
    # Run until stopped
    try:
        import time
        while pipeline.is_running():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    
    # Stop
    pipeline.stop()
