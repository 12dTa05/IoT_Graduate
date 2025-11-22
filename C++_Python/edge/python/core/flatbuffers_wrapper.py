"""
FlatBuffers Python Wrapper
Zero-copy deserialization for high-performance metadata access
"""
import numpy as np
from typing import List, Dict, Optional
import logging

# Import generated FlatBuffers code
try:
    from schemas.metadata import FrameMetadata, DetectionObject
    FLATBUFFERS_AVAILABLE = True
except ImportError:
    FLATBUFFERS_AVAILABLE = False
    logging.warning("FlatBuffers not available, falling back to JSON")

logger = logging.getLogger(__name__)


class FlatBuffersMetadata:
    """
    Zero-copy wrapper for FlatBuffers metadata
    Provides dict-like interface for compatibility
    """
    
    def __init__(self, buffer: bytes):
        """
        Initialize from FlatBuffers binary buffer
        
        Args:
            buffer: FlatBuffers binary data
        """
        if not FLATBUFFERS_AVAILABLE:
            raise ImportError("FlatBuffers not installed")
        
        self.buffer = buffer
        self.frame = FrameMetadata.FrameMetadata.GetRootAs(buffer, 0)
    
    def get_frame_number(self) -> int:
        """Get frame number (zero-copy)"""
        return self.frame.FrameNumber()
    
    def get_timestamp(self) -> float:
        """Get timestamp (zero-copy)"""
        return self.frame.Timestamp()
    
    def get_fps(self) -> float:
        """Get FPS (zero-copy)"""
        return self.frame.Fps()
    
    def get_object_count(self) -> int:
        """Get object count (zero-copy)"""
        return self.frame.ObjectCount()
    
    def get_objects(self) -> List[Dict]:
        """
        Get all objects as list of dicts
        Note: This creates Python objects, not zero-copy
        Use iterate_objects() for zero-copy iteration
        """
        objects = []
        for i in range(self.frame.ObjectsLength()):
            obj = self.frame.Objects(i)
            objects.append(self._object_to_dict(obj))
        return objects
    
    def iterate_objects(self):
        """
        Iterate objects with zero-copy access
        
        Yields:
            DetectionObject: FlatBuffers object (zero-copy)
        """
        for i in range(self.frame.ObjectsLength()):
            yield self.frame.Objects(i)
    
    def _object_to_dict(self, obj) -> Dict:
        """Convert FlatBuffers object to dict"""
        bbox = obj.Bbox()
        return {
            'track_id': obj.TrackId(),
            'x': bbox.X(),
            'y': bbox.Y(),
            'width': bbox.Width(),
            'height': bbox.Height(),
            'class_id': obj.ClassId(),
            'class_name': obj.ClassName().decode('utf-8') if obj.ClassName() else '',
            'confidence': obj.Confidence(),
            'speed': obj.Speed(),
            'plate': obj.Plate().decode('utf-8') if obj.Plate() else '',
            'plate_confidence': obj.PlateConfidence(),
            'timestamp': obj.Timestamp(),
            'is_overspeed': obj.IsOverspeed()
        }
    
    def to_dict(self) -> Dict:
        """
        Convert entire frame to dict (for compatibility)
        Note: This is NOT zero-copy
        """
        return {
            'frame_number': self.get_frame_number(),
            'timestamp': self.get_timestamp(),
            'fps': self.get_fps(),
            'object_count': self.get_object_count(),
            'objects': self.get_objects()
        }
    
    def to_json(self) -> str:
        """Convert to JSON string (for debugging)"""
        import json
        return json.dumps(self.to_dict())


class FlatBuffersSharedMemory:
    """
    Shared memory manager optimized for FlatBuffers
    Uses memory-mapped file for zero-copy transfer
    """
    
    # Memory layout:
    # [4 bytes: size] [size bytes: FlatBuffers data]
    
    HEADER_SIZE = 4
    MAX_BUFFER_SIZE = 2 * 1024 * 1024  # 2MB
    
    def __init__(self, name: str = "/deepstream_flatbuffers"):
        self.name = name
        self.mmap = None
        self._init_shared_memory()
    
    def _init_shared_memory(self):
        """Initialize shared memory"""
        try:
            import mmap
            import os
            import platform
            
            if platform.system() == "Linux":
                shm_path = f"/dev/shm{self.name}"
                
                # Create file if doesn't exist
                if not os.path.exists(shm_path):
                    with open(shm_path, 'wb') as f:
                        f.write(b'\x00' * (self.HEADER_SIZE + self.MAX_BUFFER_SIZE))
                
                # Open as memory-mapped file
                fd = os.open(shm_path, os.O_RDWR)
                self.mmap = mmap.mmap(fd, self.HEADER_SIZE + self.MAX_BUFFER_SIZE,
                                     mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
                os.close(fd)
                
                logger.info(f"FlatBuffers shared memory initialized: {shm_path}")
            else:
                logger.warning("Shared memory not fully supported on Windows")
                self.mmap = None
        
        except Exception as e:
            logger.error(f"Failed to initialize shared memory: {e}")
            self.mmap = None
    
    def write_buffer(self, buffer: bytes) -> bool:
        """
        Write FlatBuffers binary to shared memory
        
        Args:
            buffer: FlatBuffers binary data
            
        Returns:
            True if successful
        """
        if not self.mmap:
            return False
        
        size = len(buffer)
        if size > self.MAX_BUFFER_SIZE:
            logger.warning(f"Buffer too large: {size} bytes")
            return False
        
        try:
            # Write size header
            self.mmap.seek(0)
            self.mmap.write(size.to_bytes(self.HEADER_SIZE, byteorder='little'))
            
            # Write FlatBuffers data
            self.mmap.write(buffer)
            
            return True
        
        except Exception as e:
            logger.error(f"Failed to write buffer: {e}")
            return False
    
    def read_buffer(self) -> Optional[bytes]:
        """
        Read FlatBuffers binary from shared memory (zero-copy)
        
        Returns:
            FlatBuffers binary data or None
        """
        if not self.mmap:
            return None
        
        try:
            # Read size header
            self.mmap.seek(0)
            size_bytes = self.mmap.read(self.HEADER_SIZE)
            size = int.from_bytes(size_bytes, byteorder='little')
            
            if size == 0 or size > self.MAX_BUFFER_SIZE:
                return None
            
            # Read FlatBuffers data (zero-copy via memoryview)
            buffer = self.mmap.read(size)
            
            return buffer
        
        except Exception as e:
            logger.error(f"Failed to read buffer: {e}")
            return None
    
    def read_metadata(self) -> Optional[FlatBuffersMetadata]:
        """
        Read and parse metadata (zero-copy)
        
        Returns:
            FlatBuffersMetadata object or None
        """
        buffer = self.read_buffer()
        if buffer is None:
            return None
        
        try:
            return FlatBuffersMetadata(buffer)
        except Exception as e:
            logger.error(f"Failed to parse metadata: {e}")
            return None
    
    def close(self):
        """Close shared memory"""
        if self.mmap:
            self.mmap.close()
            logger.info("FlatBuffers shared memory closed")


# Singleton instance
_flatbuffers_shared_memory = None


def get_flatbuffers_shared_memory() -> FlatBuffersSharedMemory:
    """Get singleton shared memory instance"""
    global _flatbuffers_shared_memory
    
    if _flatbuffers_shared_memory is None:
        _flatbuffers_shared_memory = FlatBuffersSharedMemory()
    
    return _flatbuffers_shared_memory
