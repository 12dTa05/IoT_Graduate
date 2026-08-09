"""
Edge/speedflow_python/tests/test_pipeline_ordering.py

Deterministic host-side test for core chain element ordering:
  tiled  (display / rtsp_push): analytics → preosd_convert → preosd_caps(RGBA) → tiler → nvdsosd
  file                         : analytics → preosd_convert → preosd_caps → nvdsosd

No GStreamer, no gi, no DeepStream — pure name-list assertion.
"""

import sys
import importlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# --- Snapshot sys.modules before any mutation so we can restore on teardown ---
_MODULE_NAMES_WE_TOUCH = frozenset({
    "speedflow_python",
    "speedflow_python.common",
    "speedflow_python.settings",
    "speedflow_python.camera_config",
    "speedflow_python.core_pipeline",
    "gi",
    "gi.repository",
    "gi.repository.Gst",
    "gi.repository.GLib",
    "cv2",
    "numpy",
})
_ORIGINAL_MODULES = {k: sys.modules[k] for k in _MODULE_NAMES_WE_TOUCH if k in sys.modules}


# --- Module restamp: clear sys.modules so this test runs cleanly after any ---
# sibling test that may have left real module objects behind.
for mod_name in _MODULE_NAMES_WE_TOUCH:
    sys.modules.pop(mod_name, None)

importlib.invalidate_caches()

_EDGE = Path(__file__).resolve().parents[2]  # .../Edge
_SPEEDFLOW_PY = _EDGE / "speedflow_python"

# Make "speedflow_python" resolvable for relative imports
if str(_SPEEDFLOW_PY) not in sys.path:
    sys.path.insert(0, str(_SPEEDFLOW_PY))


def _stub_module(name: str) -> ModuleType:
    if name not in sys.modules:
        sys.modules[name] = ModuleType(name)
    return sys.modules[name]


# ── Stub the heavy / missing deps the modules import at load time ──────────
def _make_stubs(attrs: dict):  # noqa: ANN001
    for name, attr_map in attrs.items():
        m = _stub_module(name)
        for k, v in attr_map.items():
            setattr(m, k, v)


# Build the stubbed package before core_pipeline tries relative imports
# IMPORTANT: stub both the full dotted name AND the bare name under
# the parent package so 'from .camera_config import ...' resolves.
# Use EMPTY __path__ (like sibling tests) to avoid loading real modules.
_sp = _stub_module("speedflow_python")
_sp.__path__ = []
_sp_common = _stub_module("speedflow_python.common")
_sp_common.make_element = lambda *a, **kw: None
_sp_common.gst_link = lambda *a, **kw: None
_sp_settings = _stub_module("speedflow_python.settings")
_sp_settings.INFER_CONFIG = ""
_sp_settings.TRACKER_CFG = ""
_sp_settings.ANALYTICS_CFG = ""
_sp_settings.SGIE_CONFIG = ""
_sp_settings.TRACKER_LIB = ""
_sp_settings.LPR_CONFIG = ""
_sp_camcfg = _stub_module("speedflow_python.camera_config")
_sp_camcfg.CameraConfig = object
_sp_camcfg.compute_tiler_layout = lambda n: (1, 1)
# Also register under parent so 'from . import camera_config' finds it
_sp.camera_config = _sp_camcfg
_sp.common = _sp_common
_sp.settings = _sp_settings

# Comprehensive Gst stub — must provide all symbols used at module scope
class _GstElement:
    pass

class _GstPipeline:
    pass

class _GstCaps:
    @staticmethod
    def from_string(s):
        return s

class _GstElementFactory:
    @staticmethod
    def make(*a, **kw):
        return None

class _GstPadProbeReturn:
    OK = "OK"
    DROP = "DROP"

class _GstPadProbeType:
    BUFFER = "BUFFER"
    BLOCK_DOWNSTREAM = "BLOCK_DOWNSTREAM"

class _GstState:
    NULL = "NULL"

_gst = _stub_module("gi.repository.Gst")
_gst.Element = _GstElement
_gst.Pipeline = _GstPipeline
_gst.Caps = _GstCaps
_gst.ElementFactory = _GstElementFactory
_gst.PadProbeReturn = _GstPadProbeReturn
_gst.PadProbeType = _GstPadProbeType
_gst.State = _GstState
_gst.init = lambda *a, **kw: None

_gi = _stub_module("gi")
_gi.require_version = lambda *a, **kw: None

_stub_module("gi.repository")  # namespace package
_stub_module("gi.repository.GLib")

_make_stubs({
    "cv2": {},
    "numpy": {},
})


def _exec_deps_stubbed(path: Path, attrs: dict) -> ModuleType:
    """exec_module a source file against stubbed dependencies."""
    spec = importlib.util.spec_from_file_location(
        "speedflow_python.core_pipeline", path
    )
    mod = importlib.util.module_from_spec(spec)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules["speedflow_python.core_pipeline"] = mod
    spec.loader.exec_module(mod)
    return mod


# Load core_pipeline with stubs
_cp = _exec_deps_stubbed(
    _SPEEDFLOW_PY / "core_pipeline.py",
    {
        "logger": type("Logger", (), {
            "info": lambda *a, **kw: None,
            "warning": lambda *a, **kw: None,
        })(),
    },
)

get_core_chain_order = _cp.get_core_chain_order


# ── Restore candidate deps NOW (module level) so sibling test files, which  ─
# are imported by pytest right after this module during collection, see the
# real numpy/gi/cv2 instead of the load-time stubs.  The speedflow_python
# stub is kept in place on purpose: pytest imports the package
# __init__.py when it collects tests inside it, and that would fail without
# our stub.  It is removed in fixture teardown instead.
def _restore_original_modules(skip_speedflow_python: bool = False):
    for mod_name in _MODULE_NAMES_WE_TOUCH:
        if skip_speedflow_python and mod_name.startswith("speedflow_python"):
            continue
        sys.modules.pop(mod_name, None)
    sys.modules.update(_ORIGINAL_MODULES)
    importlib.invalidate_caches()


_restore_original_modules(skip_speedflow_python=True)


@pytest.fixture(scope="module", autouse=True)
def _restore_sys_modules():
    yield
    _restore_original_modules(skip_speedflow_python=True)


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────

def test_display_chain_has_tiler_between_caps_and_osd():
    """Display mode: preosd_caps → tiler → nvdsosd (RGBA before tiler)."""
    order = get_core_chain_order("display")
    assert "nvmultistreamtiler" in order
    caps_idx = order.index("capsfilter")
    tiler_idx = order.index("nvmultistreamtiler")
    osd_idx = order.index("nvdsosd")
    assert caps_idx < tiler_idx < osd_idx, (
        f"Expected capsfilter({caps_idx}) < tiler({tiler_idx}) < nvdsosd({osd_idx}), "
        f"got {order}"
    )


def test_rtsp_push_chain_has_tiler_between_caps_and_osd():
    """RTSP push mode: same RGBA-before-tiler ordering as display."""
    order = get_core_chain_order("rtsp_push")
    caps_idx = order.index("capsfilter")
    tiler_idx = order.index("nvmultistreamtiler")
    osd_idx = order.index("nvdsosd")
    assert caps_idx < tiler_idx < osd_idx


def test_file_chain_no_tiler():
    """File mode has no tiler element."""
    order = get_core_chain_order("file")
    assert "nvmultistreamtiler" not in order


def test_file_chain_caps_before_osd():
    """File mode: capsfilter → nvdsosd (no tiler between)."""
    order = get_core_chain_order("file")
    caps_idx = order.index("capsfilter")
    osd_idx = order.index("nvdsosd")
    assert caps_idx < osd_idx


def test_tiled_chain_analytics_before_caps():
    """analytics must precede the RGBA convert in both modes."""
    for sink in ("display", "rtsp_push", "file"):
        order = get_core_chain_order(sink)
        ana_idx = order.index("nvdsanalytics")
        caps_idx = order.index("capsfilter")
        assert ana_idx < caps_idx, f"{sink}: analytics({ana_idx}) before capsfilter({caps_idx})"


def test_all_chains_end_with_nvdsosd():
    """Every chain must terminate at nvdsosd (sink chain attaches after it)."""
    for sink in ("display", "rtsp_push", "file"):
        assert get_core_chain_order(sink)[-1] == "nvdsosd", f"{sink}: last element is nvdsosd"


# ──────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback

    tests = [
        test_display_chain_has_tiler_between_caps_and_osd,
        test_rtsp_push_chain_has_tiler_between_caps_and_osd,
        test_file_chain_no_tiler,
        test_file_chain_caps_before_osd,
        test_tiled_chain_analytics_before_caps,
        test_all_chains_end_with_nvdsosd,
    ]

    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
