"""
Edge/speedflow_python/tests/test_pipeline_ordering.py

Host-side test that exercises the REAL build_pipeline() ordering via
record-and-trace stubs — no GStreamer, no gi, no DeepStream runtime.

For tiled modes (display / rtsp_push):
  streammux → pgie → tracker → sgie → sgie2 → analytics →
  preosd_convert → preosd_caps(RGBA) → tiler → nvdsosd

For file mode:
  streammux → pgie → tracker → sgie → sgie2 → analytics →
  preosd_convert → preosd_caps → nvdsosd

Every build_pipeline call is self-contained; no shared mutable state leaked
between parameterized runs.
"""

import sys
import importlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Snapshot sys.modules so we can restore on teardown
# ---------------------------------------------------------------------------
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

for mod_name in _MODULE_NAMES_WE_TOUCH:
    sys.modules.pop(mod_name, None)

importlib.invalidate_caches()

_EDGE = Path(__file__).resolve().parents[2]  # .../Edge
_SPEEDFLOW_PY = _EDGE / "speedflow_python"

if str(_SPEEDFLOW_PY) not in sys.path:
    sys.path.insert(0, str(_SPEEDFLOW_PY))


def _stub_module(name: str) -> ModuleType:
    if name not in sys.modules:
        sys.modules[name] = ModuleType(name)
    return sys.modules[name]


# ── Recordable element stubs ───────────────────────────────────────────────

class _StubPad:
    """A pad that records a link between its owner elements."""
    def __init__(self, owner):
        self._owner = owner

    def is_linked(self) -> bool:
        return False

    def link(self, other: "_StubPad") -> None:
        _LINKED_PAIRS.append((self._owner, other._owner))


class _StubGstElement:
    """Minimal element that tracks identity and factory; used for set_property,
    get_static_pad, get_request_pad, add, sync_state_with_parent, connect.
    """
    def __init__(self, name: str, factory: str):
        self.name = name
        self.factory = factory
        self._props: dict = {}

    def set_property(self, key: str, value) -> None:
        self._props[key] = value

    def get_property(self, key: str):
        return self._props.get(key)

    def connect(self, *a, **kw) -> None:
        pass

    def add(self, _child) -> None:
        pass

    def sync_state_with_parent(self) -> None:
        pass

    def get_static_pad(self, _name: str) -> _StubPad:
        return _StubPad(self)

    def get_request_pad(self, _name: str) -> _StubPad:
        return _StubPad(self)


# Per-build recording — reset before each build_pipeline call
def _reset_recording() -> None:
    global _ELEMENTS, _LINKED_PAIRS
    _ELEMENTS = []
    _LINKED_PAIRS = []


_ELEMENTS: list[_StubGstElement] = []
_LINKED_PAIRS: list[tuple[_StubGstElement, _StubGstElement]] = []
_reset_recording()


def _make_element(name: str, factory: str) -> _StubGstElement:
    el = _StubGstElement(name, factory)
    _ELEMENTS.append(el)
    return el


def _gst_link(*elements: _StubGstElement) -> None:
    for a, b in zip(elements, elements[1:]):
        _LINKED_PAIRS.append((a, b))


# ── Stub the heavy / missing deps ──────────────────────────────────────────

_sp = _stub_module("speedflow_python")
_sp.__path__ = []
_sp_common = _stub_module("speedflow_python.common")
_sp_common.make_element = _make_element
_sp_common.gst_link = _gst_link
_sp_settings = _stub_module("speedflow_python.settings")
_sp_settings.INFER_CONFIG = ""
_sp_settings.TRACKER_CFG = ""
_sp_settings.ANALYTICS_CFG = ""
_sp_settings.SGIE_CONFIG = ""
_sp_settings.TRACKER_LIB = ""
_sp_settings.LPR_CONFIG = ""
_sp_camcfg = _stub_module("speedflow_python.camera_config")

# Lightweight dummy camera so source-bin stubs don't crash
class _DummyCamera:
    camera_id = "cam0"
    source_id = 0
    uri = "rtsp://dummy:554/stream"
    record = False
    record_path = "/tmp/dummy.mp4"


_sp_camcfg.CameraConfig = _DummyCamera
_sp_camcfg.compute_tiler_layout = lambda n: (1, 1)
_sp.camera_config = _sp_camcfg
_sp.common = _sp_common
_sp.settings = _sp_settings

_gst = _stub_module("gi.repository.Gst")
_gst.Element = type("_GstElementStub", (), {})  # never instantiated directly
_gst.Pipeline = type("_GstPipelineStub", (), {
    "new": staticmethod(lambda *a: _StubGstElement("pipeline", "pipeline")),
})
_gst.Caps = type("_GstCapsStub", (), {
    "from_string": staticmethod(lambda s: s),
})

# Minimal stubs for PadProbe symbols referenced by dynamic helpers (never called)
_gst.PadProbeReturn = type("_PadProbeReturn", (), {"OK": "OK", "DROP": "DROP"})
_gst.PadProbeType = type("_PadProbeType", (), {"BUFFER": "BUFFER", "BLOCK_DOWNSTREAM": "BLOCK"})
_gst.State = type("_GstStateStub", (), {"NULL": "NULL"})
_gst.init = lambda *a: None

_gi = _stub_module("gi")
_gi.require_version = lambda *a: None

_stub_module("gi.repository")
_stub_module("gi.repository.GLib")

for _name in ("cv2", "numpy"):
    _stub_module(_name)


# ── Load core_pipeline with stubs ──────────────────────────────────────────

def _exec_deps_stubbed(path: Path, attrs: dict) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "speedflow_python.core_pipeline", path
    )
    mod = importlib.util.module_from_spec(spec)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules["speedflow_python.core_pipeline"] = mod
    spec.loader.exec_module(mod)
    return mod


_cp = _exec_deps_stubbed(
    _SPEEDFLOW_PY / "core_pipeline.py",
    {"logger": type("Logger", (), {
        "info": lambda *a, **kw: None,
        "warning": lambda *a, **kw: None,
    })()},
)

build_pipeline = _cp.build_pipeline

# ── Restore real modules for sibling test imports ──────────────────────────

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


# ── Trace helpers ──────────────────────────────────────────────────────────

def _core_chain_up_to_osd() -> list[str]:
    """
    Walk the linked-pair graph from nvstreammux to nvdsosd (inclusive)
    and return factory names in traversal order.
    """
    factory = {e: e.factory for e in _ELEMENTS}
    nexts = {a: b for a, b in _LINKED_PAIRS}

    starts = [e for e in _ELEMENTS if e.factory == "nvstreammux"]
    assert starts, "build_pipeline did not create an nvstreammux element"
    cur = starts[0]

    chain: list[str] = []
    seen = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        chain.append(factory[cur])
        if factory[cur] == "nvdsosd":
            break
        cur = nexts.get(cur)

    return chain


_EXPECTED_TILED = [
    "nvstreammux", "nvinfer", "nvtracker", "nvinfer", "nvinfer",
    "nvdsanalytics", "nvvideoconvert", "capsfilter", "nvmultistreamtiler",
    "nvdsosd",
]

_EXPECTED_FILE = [
    "nvstreammux", "nvinfer", "nvtracker", "nvinfer", "nvinfer",
    "nvdsanalytics", "nvvideoconvert", "capsfilter", "nvdsosd",
]


# ── Tests ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset():
    """Fresh recording slate for each test."""
    _reset_recording()


_DUMMY_CAMS = [_DummyCamera(), _DummyCamera()]


def test_display_chain_rgba_before_tiler():
    """Tiled display: RGBA capsfilter → tiler → nvdsosd."""
    build_pipeline(_DUMMY_CAMS, sink_type="display")
    chain = _core_chain_up_to_osd()
    assert chain == _EXPECTED_TILED, f"display chain: {chain}"


def test_rtsp_push_chain_rgba_before_tiler():
    """Tiled RTSP push: same RGBA-before-tiler ordering."""
    build_pipeline(_DUMMY_CAMS, sink_type="rtsp_push", rtsp_push_url="rtsp://x")
    chain = _core_chain_up_to_osd()
    assert chain == _EXPECTED_TILED, f"rtsp_push chain: {chain}"


def test_file_chain_no_tiler():
    """File mode: no tiler, caps → nvdsosd directly."""
    build_pipeline(_DUMMY_CAMS, sink_type="file")
    chain = _core_chain_up_to_osd()
    assert chain == _EXPECTED_FILE, f"file chain: {chain}"
    assert "nvmultistreamtiler" not in chain


def test_file_chain_caps_before_osd():
    """File mode: capsfilter → nvdsosd consecutive."""
    build_pipeline(_DUMMY_CAMS, sink_type="file")
    chain = _core_chain_up_to_osd()
    caps_idx = chain.index("capsfilter")
    osd_idx = chain.index("nvdsosd")
    assert caps_idx + 1 == osd_idx, (
        f"capsfilter({caps_idx}) should be immediately before nvdsosd({osd_idx})"
    )
