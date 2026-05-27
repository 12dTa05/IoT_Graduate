# speedflow_python/common.py
"""
Shared utilities for the Python backend pipeline.
"""

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst


# ---------------------------------------------------------------------------
# GStreamer helper
# ---------------------------------------------------------------------------

def make_element(name: str, factory: str) -> Gst.Element:
    """Create a GStreamer element, raising a clear error if the factory is missing."""
    element = Gst.ElementFactory.make(factory, name)
    if not element:
        raise RuntimeError(
            f"Failed to create GStreamer element '{factory}' (alias '{name}'). "
            f"Make sure the required GStreamer plugin is installed."
        )
    return element


def gst_link(*elements: Gst.Element) -> None:
    """
    Link a chain of GStreamer elements in order.
    Raises RuntimeError with a descriptive message on failure.
    Replaces bare `assert element.link(next)` calls which are disabled by -O.
    """
    for a, b in zip(elements, elements[1:]):
        if not a.link(b):
            raise RuntimeError(
                f"Failed to link GStreamer elements: "
                f"'{a.get_name()}' → '{b.get_name()}'"
            )
