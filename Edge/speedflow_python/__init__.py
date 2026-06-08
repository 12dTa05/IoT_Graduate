# speedflow_python/__init__.py
"""
Python-based speed measurement and license plate recognition module.
"""

from .core_pipeline import build_pipeline
from .probes import SpeedProbe, ROIFilterProbe
from .plate_preprocessor import PlatePreprocessorProbe
from .common import make_element, gst_link
from . import settings      # access via settings.VIDEO_FPS etc.
from . import speedflow_c   # C extension; available → speedflow_c.is_available()
from .offload_publisher import OffloadPublisher
from .offload_receiver  import OffloadReceiver

__all__ = [
    'build_pipeline',
    'SpeedProbe',
    'ROIFilterProbe',
    'PlatePreprocessorProbe',
    'make_element',
    'gst_link',
    'settings',
    'speedflow_c',
    'OffloadPublisher',
    'OffloadReceiver',
]
