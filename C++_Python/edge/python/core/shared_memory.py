"""
Shared Memory Manager for C++ <-> Python Communication
Enables zero-copy metadata transfer between DeepStream and Python
"""
import mmap
import struct
import json
from typing import Optional, List, Dict
import logging

logger = logging.getLogger(__name__)


class SharedMemoryManager:
    """
    Manages shared memory for fast C++ to Python communication
    Uses memory-mapped file for zero-copy data transfer
    """
    
    # Memory layout:
    # [4 bytes: metadata_size] [metadata_size bytes: JSON metadata] [padding]
    
    HEADER_SIZE = 4  # Size field (uint32)
    MAX_METADATA_SIZE = 1024 * 1024  # 1 MB max
    
    def __init__(self, name: str = "/deepstream_metadata", size: int = 2 * 1024 * 1024):
        """
        Initialize shared memory
        
        Args:
            name: Shared memory name
            size: Total size in bytes (default 2MB)
        """
        self.name = name
        self.size = size
        self.mmap: Optional[mmap.mmap] = None
        
        try:
            # Create memory-mapped file
            # Note: On Linux, use /dev/shm/; on Windows, use named shared memory
            import platform
            if platform.system() == "Linux":
                import os
                shm_path = f"/dev/shm{name}"
                
                # Create file if doesn't exist
                if not os.path.exists(shm_path):
                    with open(shm_path, 'wb') as f:
                        f.write(b'\x00' * size)
                
                # Open as memory-mapped file
                fd = os.open(shm_path, os.O_RDWR)
                self.mmap = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
                os.close(fd)
                
                logger.info(f"Shared memory initialized: {shm_path} ({size} bytes)")
            else:
                # Windows: use anonymous mmap for now
                # TODO: Implement proper Windows shared memory
                logger.warning("Shared memory not fully supported on Windows, using fallback")
                self.mmap = None
        
        except Exception as e:
            logger.error(f"Failed to initialize shared memory: {e}")
            self.mmap = None
    
    def write_metadata(self, metadata: List[Dict]) -> bool:
        """
        Write metadata to shared memory
        
        Args:
            metadata: List of object dictionaries
            
        Returns:
            True if successful
        """
        if not self.mmap:
            return False
        
        try:
            # Serialize to JSON
            json_str = json.dumps(metadata)
            json_bytes = json_str.encode('utf-8')
            
            if len(json_bytes) > self.MAX_METADATA_SIZE:
                logger.warning(f"Metadata too large: {len(json_bytes)} bytes")
                return False
            
            # Write size header
            self.mmap.seek(0)
            self.mmap.write(struct.pack('I', len(json_bytes)))
            
            # Write JSON data
            self.mmap.write(json_bytes)
            
            return True
        
        except Exception as e:
            logger.error(f"Failed to write metadata: {e}")
            return False
    
    def read_metadata(self) -> Optional[List[Dict]]:
        """
        Read metadata from shared memory
        
        Returns:
            List of object dictionaries or None
        """
        if not self.mmap:
            return None
        
        try:
            # Read size header
            self.mmap.seek(0)
            size_bytes = self.mmap.read(self.HEADER_SIZE)
            
            if len(size_bytes) < self.HEADER_SIZE:
                return None
            
            size = struct.unpack('I', size_bytes)[0]
            
            if size == 0 or size > self.MAX_METADATA_SIZE:
                return None
            
            # Read JSON data
            json_bytes = self.mmap.read(size)
            
            if len(json_bytes) < size:
                return None
            
            # Parse JSON
            json_str = json_bytes.decode('utf-8')
            metadata = json.loads(json_str)
            
            return metadata
        
        except Exception as e:
            logger.error(f"Failed to read metadata: {e}")
            return None
    
    def close(self):
        """Close shared memory"""
        if self.mmap:
            self.mmap.close()
            logger.info("Shared memory closed")


# Singleton instance
_shared_memory_instance: Optional[SharedMemoryManager] = None


def get_shared_memory() -> SharedMemoryManager:
    """Get singleton shared memory instance"""
    global _shared_memory_instance
    
    if _shared_memory_instance is None:
        _shared_memory_instance = SharedMemoryManager()
    
    return _shared_memory_instance
