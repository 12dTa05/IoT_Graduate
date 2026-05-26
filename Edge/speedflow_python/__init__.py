# speedflow_python/__init__.py
"""
Python-based speed measurement and license plate recognition module.
"""

from .core_pipeline import build_pipeline
from .probes import SpeedProbe, ROIFilterProbe
from .plate_preprocessor import PlatePreprocessorProbe
from .common import make_element, gst_link, WebRTCSession
from . import settings  # access via settings.VIDEO_FPS etc.

__all__ = [
    'build_pipeline',
    'SpeedProbe',
    'ROIFilterProbe',
    'PlatePreprocessorProbe',
    'make_element',
    'gst_link',
    'WebRTCSession',
    'settings',
]
