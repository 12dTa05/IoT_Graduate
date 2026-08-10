"""
Edge/tests/test_offload_counters.py

Focused host tests verifying offload E2E counters:
  - OffloadPublisher: encoded/enqueued/sent/dropped/send_errors counters
  - OffloadReceiver: received/queue_dropped/processed/errors/results_sent counters
  - SpeedProbe.inject_offload_result increments results_received

All tests use fake/real stubs for telemetry and zenoh — no GStreamer, no
hardware, no network. Run on host with `conda run -n DoAn python3
tests/test_offload_counters.py`.
"""

import sys
import tempfile
import types
import traceback
from pathlib import Path

import numpy as np

EDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EDGE))


def _install_host_stubs():
    """
    Provide the non-hardware imports needed by this host test.

    Runs before every _load(), and is deliberately *unconditional*: sibling
    tests may have poisoned sys.modules with partial fakes (e.g. a msgpack
    stub without packb/unpackb, or a gi stub without require_version). We
    re-stamp or fully replace every module this test needs so collection
    order never matters.
    """
    # dotenv: only load_dotenv is used by settings.py
    dotenv = types.ModuleType("dotenv")
    setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    sys.modules["dotenv"] = dotenv

    package = sys.modules.get("speedflow_python")
    if package is None:
        package = types.ModuleType("speedflow_python")
        package.__path__ = [str(EDGE / "speedflow_python")]
        sys.modules["speedflow_python"] = package

    settings = sys.modules.get("speedflow_python.settings")
    if settings is None:
        settings = types.ModuleType("speedflow_python.settings")
        sys.modules["speedflow_python.settings"] = settings
    for key, value in {
            "ROOT": EDGE,
            "NODE_ID": "host-test",
            "HEALTH_INTERVAL": 1.0,
            "HEALTH_LOG_EVERY": 30,
            "TARGET_FPS": 25.0,
            "FPS_STATS_FILE": str(EDGE / "logs" / "fps_stats.json"),
            "MONITOR_URL": "",
            "ADVERTISE_IP": "",
            "LOAD_POLICY": "fps_dominant",
            "LOAD_MODEL": "",
            "TELEMETRY_INTERVAL": 1.0,
            "JPEG_QUALITY": 85,
            "SNAP_DIR": str(EDGE / "snapshots"),
            "MAX_SNAPSHOT_PER_ID": 5,
            "MIN_WORLD_DISPL_M": 0.5,
            "MAX_ABS_KMH": 200.0,
            "BBOX_AREA_JUMP": 2.0,
            "MIN_DET_CONF": 0.3,
            "MEDIAN_WINDOW": 5,
            "LICENSE_PLATE_CLASS_IDS": {0},
            "VEHICLE_CLASS_IDS": {2, 3, 5, 7},
            "SPEED_LOG": str(EDGE / "logs" / "speed.log"),
            "CAMERAS_YML": str(EDGE / "configs" / "cameras.yml"),
            "VIDEO_FPS": 30,
        }.items():
            setattr(settings, key, value)

    # zenoh_session: only make_session is used at import edge (never called).
    session = types.ModuleType("speedflow_python.zenoh_session")
    setattr(session, "make_session", lambda: None)
    sys.modules["speedflow_python.zenoh_session"] = session

    # numpy: real
    import numpy
    sys.modules["numpy"] = numpy

    # cv2: not installed on this host; the modules under test use only the
    # attributes below (encode/decode/resize/constants).
    cv2 = types.ModuleType("cv2")
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.IMREAD_COLOR = 1
    cv2.COLOR_RGBA2BGR = 4
    cv2.COLOR_GRAY2BGR = 8
    cv2.resize = lambda img, size, **kw: img
    cv2.cvtColor = lambda img, code: img
    cv2.imencode = lambda ext, img, params: (True, types.SimpleNamespace(tobytes=lambda: b"fake_jpeg"))
    cv2.imdecode = lambda arr, flag: None
    cv2.imwrite = lambda *a, **kw: True
    sys.modules["cv2"] = cv2

    # msgpack: MUST be the real module (packb/unpackb for fake payloads).
    # Sibling tests may install a broken msgpack stub — drop it first so the
    # real import actually resolves.
    sys.modules.pop("msgpack", None)
    import msgpack
    sys.modules["msgpack"] = msgpack

    # queue/threading: stdlib
    import queue
    import threading
    sys.modules["queue"] = queue
    sys.modules["threading"] = threading

    # gi / Gst / pyds: only needed by probes.py on host. Full self-contained
    # stub, installed unconditionally so a previous sibling's partial gi
    # (missing require_version / repository) can't break the probes import.
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    gi.repository = types.ModuleType("gi.repository")
    Gst = types.ModuleType("gi.repository.Gst")
    Gst.PadProbeReturn = types.SimpleNamespace(OK=0, DROP=1, REMOVE=2)
    Gst.PadProbeType = types.SimpleNamespace(BUFFER=16)
    Gst.Buffer = object
    Gst.Pad = object
    gi.repository.Gst = Gst
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = gi.repository
    sys.modules["gi.repository.Gst"] = Gst

    pyds = types.ModuleType("pyds")
    pyds.nvds_acquire_display_meta_from_pool = lambda *a, **k: None
    pyds.nvds_add_display_meta_to_frame = lambda *a, **k: None
    # Minimal cast helpers so osd_sink_pad_buffer_probe can run on host.
    pyds.gst_buffer_get_nvds_batch_meta = lambda _h: None   # overridden per test
    pyds.NvDsFrameMeta  = types.SimpleNamespace(cast=lambda d: d)
    pyds.NvDsObjectMeta = types.SimpleNamespace(cast=lambda d: d)
    sys.modules["pyds"] = pyds

    # speedflow_c stub: native .so not present on host. Fully replaced so a
    # partial sibling stub (missing functions) cannot break probes.py.
    sf = types.ModuleType("speedflow_python.speedflow_c")
    sf.point_in_polygon = lambda *a, **k: True
    sf.median_speed = lambda vals: (sum(vals) / len(vals)) if vals else 0.0
    sf.center_distance = lambda *a, **k: 0.0
    sf.compute_speed_kmh = lambda *a, **k: 0.0
    sf.valid_measurement = lambda *a, **k: True
    sf.plate_quality = lambda *a, **k: 1.0
    sf.enhance_bgr_inplace = lambda *a, **k: None
    sf.perspective_batch = lambda m, arr: arr
    sys.modules["speedflow_python.speedflow_c"] = sf

    # draw stub: probes.py imports add_polygon_display at import time.
    draw = types.ModuleType("speedflow_python.draw")
    draw.add_polygon_display = lambda *a, **k: None
    sys.modules["speedflow_python.draw"] = draw

    # camera_config stub: probes.py imports CameraManager, CameraConfig.
    cam_cfg = types.ModuleType("speedflow_python.camera_config")
    class _StubCameraManager:
        def get_config(self, source_id):
            return None
    cam_cfg.CameraManager = _StubCameraManager
    cam_cfg.CameraConfig = object
    sys.modules["speedflow_python.camera_config"] = cam_cfg


def _load(name, relpath):
    """Load modules without importing speedflow_python/__init__.py (needs gi)."""
    _install_host_stubs()

    module_name = name if "." in name else f"speedflow_python.{name}"
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location(module_name, EDGE / relpath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {module_name}")
    mod = module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ======================================================================
# Fake session for OffloadPublisher / OffloadReceiver
# ======================================================================

class _FakePub:
    """Fake publisher capturing put() calls."""
    def __init__(self, key):
        self.key = key
        self.put_calls = []

    def put(self, data):
        self.put_calls.append(data)


class _FakeSession:
    """Fake Zenoh session declaring fake publishers."""
    def __init__(self):
        self.pubs = {}

    def declare_publisher(self, key):
        pub = _FakePub(key)
        self.pubs[key] = pub
        return pub

    def declare_subscriber(self, key, handler):
        # No-op for tests; real session wires handler to key
        pass

    def close(self):
        pass


class _FakeSample:
    """Fake Zenoh sample for OffloadReceiver tests."""
    def __init__(self, payload_dict):
        import msgpack
        self._payload_bytes = msgpack.packb(payload_dict, use_bin_type=True)

    @property
    def payload(self):
        class _P:
            def __init__(self, b):
                self._b = b
            def to_bytes(self):
                return self._b
        return _P(self._payload_bytes)


# ======================================================================
# OffloadPublisher tests
# ======================================================================

def test_publisher_counters_encode_enqueue_sent():
    pub_mod = _load("offload_publisher", "speedflow_python/offload_publisher.py")
    session = _FakeSession()

    pub = pub_mod.OffloadPublisher(node_id="edge-01", session=session)
    pub.start()

    # 1) put_plate increments encoded + enqueued
    import numpy as np
    crop = np.zeros((48, 120, 3), dtype=np.uint8)
    pub.put_plate(target_node="edge-02", stid=(0, 42),
                  camera_id="cam_01", frame_no=1, crop_bgr=crop, confidence=0.9)

    assert pub.offload_encoded_count == 1
    assert pub.offload_enqueued_count == 1

    # 2) put_vehicle increments encoded + enqueued
    crop2 = np.zeros((120, 120, 3), dtype=np.uint8)
    pub.put_vehicle(target_node="edge-02", stid=(0, 43),
                    camera_id="cam_01", frame_no=2, crop_bgr=crop2, bbox_world_y=10.0)

    assert pub.offload_encoded_count == 2
    assert pub.offload_enqueued_count == 2

    # Wait for publish loop to drain queue
    import time
    for _ in range(50):
        time.sleep(0.02)
        if pub.offload_sent_count >= 2:
            break

    assert pub.offload_sent_count >= 2, f"sent={pub.offload_sent_count}"
    assert pub.offload_dropped_count == 0
    assert pub.offload_send_errors_count == 0

    pub.stop()
    print("  PASS  test_publisher_counters_encode_enqueue_sent")


def test_publisher_dropped_counter_when_queue_full():
    pub_mod = _load("offload_publisher", "speedflow_python/offload_publisher.py")
    session = _FakeSession()

    # Small queue for testing: monkey-patch before start
    pub = pub_mod.OffloadPublisher(node_id="edge-01", session=session)
    pub._queue = __import__("queue").Queue(maxsize=2)
    pub.start()

    import numpy as np
    crop = np.zeros((48, 120, 3), dtype=np.uint8)

    # Fill queue (3 items -> maxsize 2 -> 1 dropped)
    pub.put_plate(target_node="edge-02", stid=(0, 1), camera_id="cam_01", frame_no=1, crop_bgr=crop)
    pub.put_plate(target_node="edge-02", stid=(0, 2), camera_id="cam_01", frame_no=2, crop_bgr=crop)
    pub.put_plate(target_node="edge-02", stid=(0, 3), camera_id="cam_01", frame_no=3, crop_bgr=crop)

    # Wait for loop
    import time
    for _ in range(50):
        time.sleep(0.02)
        if pub.offload_enqueued_count >= 3:
            break

    assert pub.offload_encoded_count == 3
    assert pub.offload_enqueued_count == 3
    assert pub.offload_dropped_count == 1, f"dropped={pub.offload_dropped_count}"

    pub.stop()
    print("  PASS  test_publisher_dropped_counter_when_queue_full")


def test_publisher_send_errors_counter():
    pub_mod = _load("offload_publisher", "speedflow_python/offload_publisher.py")

    # Session whose declare_publisher returns a broken pub
    class _BadSession:
        def __init__(self):
            self.closed = False
        def declare_publisher(self, key):
            class _Broken:
                def put(self, data):
                    raise RuntimeError("simulated send error")
            return _Broken()
        def close(self):
            self.closed = True

    session = _BadSession()
    pub = pub_mod.OffloadPublisher(node_id="edge-01", session=session)
    pub.start()

    import numpy as np
    crop = np.zeros((48, 120, 3), dtype=np.uint8)
    pub.put_plate(target_node="edge-02", stid=(0, 42),
                  camera_id="cam_01", frame_no=1, crop_bgr=crop, confidence=0.9)

    import time
    for _ in range(50):
        time.sleep(0.02)
        if pub.offload_send_errors_count >= 1:
            break

    assert pub.offload_send_errors_count >= 1, f"send_errors={pub.offload_send_errors_count}"
    assert pub.offload_encoded_count == 1
    assert pub.offload_enqueued_count == 1
    assert pub.offload_sent_count == 0  # send failed

    pub.stop()
    print("  PASS  test_publisher_send_errors_counter")


def test_publisher_backward_compat():
    """Ensure old attributes like _drops are not used anymore."""
    pub_mod = _load("offload_publisher", "speedflow_python/offload_publisher.py")
    session = _FakeSession()
    pub = pub_mod.OffloadPublisher(node_id="edge-01", session=session)
    pub.start()

    # Only new counter accessors exist
    assert hasattr(pub, "offload_encoded_count")
    assert hasattr(pub, "offload_enqueued_count")
    assert hasattr(pub, "offload_sent_count")
    assert hasattr(pub, "offload_dropped_count")
    assert hasattr(pub, "offload_send_errors_count")

    # Old internal names are not used anymore (not a hard requirement, just sanity)
    pub.stop()
    print("  PASS  test_publisher_backward_compat")


# ======================================================================
# OffloadReceiver tests
# ======================================================================

def test_receiver_counters_received_processed():
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    # Engines won't load (files don't exist), but we can still test queue/counters
    rcv.start()

    # Send two plate samples
    payload1 = {
        "type": "plate", "src": "edge-01", "dst": "edge-02",
        "camera_id": "cam_01", "stid": [0, 42], "frame_no": 1,
        "jpeg": b"fake", "confidence": 0.9, "ts": 1234567890.0
    }
    payload2 = {
        "type": "plate", "src": "edge-01", "dst": "edge-02",
        "camera_id": "cam_01", "stid": [0, 43], "frame_no": 2,
        "jpeg": b"fake", "confidence": 0.8, "ts": 1234567891.0
    }

    rcv._on_plate_sample(_FakeSample(payload1))
    rcv._on_plate_sample(_FakeSample(payload2))

    import time
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_received_count >= 2:
            break

    assert rcv.offload_received_count == 2
    # Invalid JPEG is handled as an empty crop, then still produces a result.
    assert rcv.offload_processed_count == 2
    assert rcv.offload_errors_count == 0

    rcv.stop()
    print("  PASS  test_receiver_counters_received_processed")


def test_receiver_queue_dropped_counter():
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    # Reduce work queue
    import queue
    rcv._work_q = queue.Queue(maxsize=2)

    payload = {
        "type": "plate", "src": "edge-01", "dst": "edge-02",
        "camera_id": "cam_01", "stid": [0, 1], "frame_no": 1,
        "jpeg": b"fake", "confidence": 0.9, "ts": 1234567890.0
    }

    # 3 items -> queue size 2 -> 1 dropped
    rcv._on_plate_sample(_FakeSample(payload))
    rcv._on_plate_sample(_FakeSample(payload))
    rcv._on_plate_sample(_FakeSample(payload))

    # received counts ALL successfully decoded wires, even those later dropped
    assert rcv.offload_received_count == 3
    assert rcv.offload_queue_dropped_count == 1
    print("  PASS  test_receiver_queue_dropped_counter")


def test_receiver_results_sent_counter():
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    # Manually call _publish_result (bypass engine)
    rcv._publish_result(
        dst_node="edge-01", camera_id="cam_01", stid=(0, 42),
        frame_no=1, plate_text="ABC123", confidence=0.95
    )
    rcv._publish_result(
        dst_node="edge-01", camera_id="cam_01", stid=(0, 43),
        frame_no=2, plate_text="XYZ789", confidence=0.85
    )

    assert rcv.offload_results_sent_count == 2

    rcv.stop()
    print("  PASS  test_receiver_results_sent_counter")


def test_receiver_all_counter_accessors_exist():
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    assert hasattr(rcv, "offload_processed_count")
    assert hasattr(rcv, "offload_received_count")
    assert hasattr(rcv, "offload_queue_dropped_count")
    assert hasattr(rcv, "offload_errors_count")
    assert hasattr(rcv, "offload_results_sent_count")
    assert hasattr(rcv, "offload_inference_errors_count")

    # backward compat
    assert hasattr(rcv, "offload_processed_count")

    rcv.stop()
    print("  PASS  test_receiver_all_counter_accessors_exist")


# ======================================================================
# Inference outcome tests (contract: inference exception ≠ valid empty)
# ======================================================================

def _make_failing_lpr_engine():
    """Mock LPR engine that always raises on infer()."""
    class _FailEngine:
        def infer(self, inp):
            raise RuntimeError("simulated LPR engine crash")
    return _FailEngine()


def _enable_jpeg_decode():
    """Patch cv2.imdecode stub to return a real crop so inference runs."""
    import cv2
    cv2.imdecode = lambda arr, flag: np.zeros((120, 160, 3), dtype=np.uint8)


def _make_failing_lpd_engine():
    """Mock LPD engine that always raises on infer()."""
    class _FailEngine:
        def infer(self, inp):
            raise RuntimeError("simulated LPD engine crash")
    return _FailEngine()


def _make_successful_lpr_engine(plate_text="ABC123", confidence=0.95):
    """Mock LPR engine that returns a successful decode."""
    class _OkEngine:
        def infer(self, inp):
            # outputs format expected by _decode_lpr_output
            return [
                np.array([1, 2, 3], dtype=np.int32),  # argmax seq
                np.array([0.9, 0.9, 0.9], dtype=np.float32),  # max probs
            ]
    return _OkEngine()


def _make_successful_lpd_engine(has_plate=True):
    """Mock LPD engine that returns a plate bbox or None."""
    class _OkEngine:
        def infer(self, inp):
            if has_plate:
                # [x1, y1, x2, y2, conf, class]
                return [np.array([[0.1, 0.1, 0.5, 0.5, 0.9, 0]], dtype=np.float32)]
            else:
                return [np.array([[0.1, 0.1, 0.5, 0.5, 0.1, 0]], dtype=np.float32)]  # below thresh
    return _OkEngine()


def test_receiver_lpr_inference_failure_suppresses_publish():
    """LPR infer exception → no publish, inference_errors counter increments."""
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    _enable_jpeg_decode()
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    # Prevent _load_engines_once from overwriting our mock
    rcv._engines_loaded = True

    # Replace LPR engine with a failing one
    rcv._lpr_engine = _make_failing_lpr_engine()

    # Send a plate sample
    payload = {
        "type": "plate", "src": "edge-01", "dst": "edge-02",
        "camera_id": "cam_01", "stid": [0, 42], "frame_no": 1,
        "jpeg": b"fake", "confidence": 0.9, "ts": 1234567890.0
    }
    rcv._on_plate_sample(_FakeSample(payload))

    # Wait for worker to process
    import time
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 1:
            break

    # Assert: received=1, processed=1, results_sent=0, inference_errors=1
    assert rcv.offload_received_count == 1
    assert rcv.offload_processed_count == 1
    assert rcv.offload_results_sent_count == 0
    assert rcv.offload_inference_errors_count == 1

    rcv.stop()
    print("  PASS  test_receiver_lpr_inference_failure_suppresses_publish")


def test_receiver_lpd_inference_failure_suppresses_publish():
    """LPD infer exception → no publish, inference_errors counter increments."""
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    _enable_jpeg_decode()
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    # Prevent _load_engines_once from overwriting our mock
    rcv._engines_loaded = True

    # Replace LPD engine with a failing one
    rcv._lpd_engine = _make_failing_lpd_engine()

    # Send a vehicle sample
    payload = {
        "type": "vehicle", "src": "edge-01", "dst": "edge-02",
        "camera_id": "cam_01", "stid": [0, 43], "frame_no": 2,
        "jpeg": b"fake", "confidence": 0.9, "ts": 1234567891.0
    }
    rcv._on_vehicle_sample(_FakeSample(payload))

    # Wait for worker to process
    import time
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 1:
            break

    # Assert: received=1, processed=1, results_sent=0, inference_errors=1
    assert rcv.offload_received_count == 1
    assert rcv.offload_processed_count == 1
    assert rcv.offload_results_sent_count == 0
    assert rcv.offload_inference_errors_count == 1

    rcv.stop()
    print("  PASS  test_receiver_lpd_inference_failure_suppresses_publish")


def test_receiver_lpd_no_bbox_valid_empty_publishes():
    """LPD succeeds but finds no plate bbox → valid empty publish, inference_ok=True."""
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    _enable_jpeg_decode()
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    # Prevent _load_engines_once from overwriting our mock
    rcv._engines_loaded = True

    # Replace LPD engine with one that returns no plate (low confidence)
    rcv._lpd_engine = _make_successful_lpd_engine(has_plate=False)

    # Send a vehicle sample
    payload = {
        "type": "vehicle", "src": "edge-01", "dst": "edge-02",
        "camera_id": "cam_01", "stid": [0, 44], "frame_no": 3,
        "jpeg": b"fake", "confidence": 0.9, "ts": 1234567892.0
    }
    rcv._on_vehicle_sample(_FakeSample(payload))

    # Wait for worker to process
    import time
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 1:
            break

    # Assert: published with empty plate_text, inference_ok=True
    assert rcv.offload_received_count == 1
    assert rcv.offload_processed_count == 1
    assert rcv.offload_results_sent_count == 1
    assert rcv.offload_inference_errors_count == 0

    # Check published payload
    pub = session.pubs["offload/results/edge-02/edge-01"]
    assert len(pub.put_calls) == 1
    import msgpack
    result = msgpack.unpackb(pub.put_calls[0], raw=False)
    assert result["plate_text"] == ""
    assert result["confidence"] == 0.0
    assert result["inference_ok"] is True

    rcv.stop()
    print("  PASS  test_receiver_lpd_no_bbox_valid_empty_publishes")


def test_receiver_lpr_empty_plate_text_valid_publishes():
    """LPR succeeds but decodes empty string → valid empty publish, inference_ok=True."""
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    _enable_jpeg_decode()
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    # Prevent _load_engines_once from overwriting our mock
    rcv._engines_loaded = True

    # Replace LPR engine with one that returns empty decode
    class _EmptyLprEngine:
        def infer(self, inp):
            # Returns empty CTC decode
            return [
                np.array([], dtype=np.int32),
                np.array([], dtype=np.float32),
            ]
    rcv._lpr_engine = _EmptyLprEngine()

    # Send a plate sample
    payload = {
        "type": "plate", "src": "edge-01", "dst": "edge-02",
        "camera_id": "cam_01", "stid": [0, 45], "frame_no": 4,
        "jpeg": b"fake", "confidence": 0.9, "ts": 1234567893.0
    }
    rcv._on_plate_sample(_FakeSample(payload))

    # Wait for worker to process
    import time
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 1:
            break

    # Assert: published with empty plate_text, inference_ok=True
    assert rcv.offload_received_count == 1
    assert rcv.offload_processed_count == 1
    assert rcv.offload_results_sent_count == 1
    assert rcv.offload_inference_errors_count == 0

    pub = session.pubs["offload/results/edge-02/edge-01"]
    assert len(pub.put_calls) == 1
    import msgpack
    result = msgpack.unpackb(pub.put_calls[0], raw=False)
    assert result["plate_text"] == ""
    assert result["confidence"] == 0.0
    assert result["inference_ok"] is True

    rcv.stop()
    print("  PASS  test_receiver_lpr_empty_plate_text_valid_publishes")


def test_receiver_inference_error_counter_accuracy():
    """inference_errors increments exactly on infer exceptions (not valid empty)."""
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    _enable_jpeg_decode()
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    # Prevent _load_engines_once from overwriting our mock
    rcv._engines_loaded = True

    # 1. LPR inference failure
    rcv._lpr_engine = _make_failing_lpr_engine()
    payload = {"type": "plate", "src": "edge-01", "dst": "edge-02",
               "camera_id": "cam_01", "stid": [0, 1], "frame_no": 1,
               "jpeg": b"fake", "confidence": 0.9, "ts": 1.0}
    rcv._on_plate_sample(_FakeSample(payload))
    import time
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 1:
            break
    assert rcv.offload_inference_errors_count == 1
    assert rcv.offload_results_sent_count == 0

    # 2. LPD inference failure
    rcv._lpd_engine = _make_failing_lpd_engine()
    payload = {"type": "vehicle", "src": "edge-01", "dst": "edge-02",
               "camera_id": "cam_01", "stid": [0, 2], "frame_no": 2,
               "jpeg": b"fake", "confidence": 0.9, "ts": 2.0}
    rcv._on_vehicle_sample(_FakeSample(payload))
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 2:
            break
    assert rcv.offload_inference_errors_count == 2
    assert rcv.offload_results_sent_count == 0

    # 3. LPD no bbox (valid empty) - should NOT increment
    rcv._lpd_engine = _make_successful_lpd_engine(has_plate=False)
    payload = {"type": "vehicle", "src": "edge-01", "dst": "edge-02",
               "camera_id": "cam_01", "stid": [0, 3], "frame_no": 3,
               "jpeg": b"fake", "confidence": 0.9, "ts": 3.0}
    rcv._on_vehicle_sample(_FakeSample(payload))
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 3:
            break
    assert rcv.offload_inference_errors_count == 2  # unchanged
    assert rcv.offload_results_sent_count == 1

    # 4. LPR empty plate text (valid empty) - should NOT increment
    rcv._lpr_engine = _make_successful_lpr_engine(plate_text="", confidence=0.0)
    payload = {"type": "plate", "src": "edge-01", "dst": "edge-02",
               "camera_id": "cam_01", "stid": [0, 4], "frame_no": 4,
               "jpeg": b"fake", "confidence": 0.9, "ts": 4.0}
    rcv._on_plate_sample(_FakeSample(payload))
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 4:
            break
    assert rcv.offload_inference_errors_count == 2  # unchanged
    assert rcv.offload_results_sent_count == 2

    rcv.stop()
    print("  PASS  test_receiver_inference_error_counter_accuracy")


# ======================================================================
# Engine unavailable tests (LPR/LPD None with engines marked loaded)
# ======================================================================

def test_receiver_lpr_engine_unavailable_suppresses_publish():
    """LPR engine is None (not loaded) → no publish, inference_errors increments,
    warning contains concise reason."""
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    _enable_jpeg_decode()
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    # Mark engines as loaded, but keep LPR engine as None (unavailable)
    rcv._engines_loaded = True
    rcv._lpr_engine = None

    # Send a plate sample
    payload = {
        "type": "plate", "src": "edge-01", "dst": "edge-02",
        "camera_id": "cam_01", "stid": [0, 42], "frame_no": 1,
        "jpeg": b"fake", "confidence": 0.9, "ts": 1234567890.0
    }
    rcv._on_plate_sample(_FakeSample(payload))

    # Wait for worker to process
    import time
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 1:
            break

    # Assert: received=1, processed=1, results_sent=0, inference_errors=1
    assert rcv.offload_received_count == 1
    assert rcv.offload_processed_count == 1
    assert rcv.offload_results_sent_count == 0
    assert rcv.offload_inference_errors_count == 1

    rcv.stop()
    print("  PASS  test_receiver_lpr_engine_unavailable_suppresses_publish")


def test_receiver_lpd_engine_unavailable_suppresses_publish():
    """LPD engine is None (not loaded) → no publish, inference_errors increments,
    warning contains concise reason."""
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    _enable_jpeg_decode()
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    # Mark engines as loaded, but keep LPD engine as None (unavailable)
    rcv._engines_loaded = True
    rcv._lpd_engine = None

    # Send a vehicle sample
    payload = {
        "type": "vehicle", "src": "edge-01", "dst": "edge-02",
        "camera_id": "cam_01", "stid": [0, 43], "frame_no": 2,
        "jpeg": b"fake", "confidence": 0.9, "ts": 1234567891.0
    }
    rcv._on_vehicle_sample(_FakeSample(payload))

    # Wait for worker to process
    import time
    for _ in range(50):
        time.sleep(0.02)
        if rcv.offload_processed_count >= 1:
            break

    # Assert: received=1, processed=1, results_sent=0, inference_errors=1
    assert rcv.offload_received_count == 1
    assert rcv.offload_processed_count == 1
    assert rcv.offload_results_sent_count == 0
    assert rcv.offload_inference_errors_count == 1

    rcv.stop()
    print("  PASS  test_receiver_lpd_engine_unavailable_suppresses_publish")


def test_receiver_inference_error_warning_rate_limited():
    """WARNING log for inference errors is rate-limited (one per 5s).
    Uses mocked time.monotonic and captured logger."""
    rcv_mod = _load("offload_receiver", "speedflow_python/offload_receiver.py")
    _enable_jpeg_decode()
    session = _FakeSession()

    rcv = rcv_mod.OffloadReceiver(
        node_id="edge-02",
        session=session,
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
        labels_path="/nonexistent/labels.txt",
    )
    rcv.start()

    # Prevent _load_engines_once from overwriting our mock
    rcv._engines_loaded = True

    # Mock time.monotonic to control rate limiting
    import time
    mock_time = [0.0]
    orig_monotonic = time.monotonic
    time.monotonic = lambda: mock_time[0]

    # Capture warning logs
    import logging
    warn_msgs = []
    recv_logger = logging.getLogger("speedflow_python.offload_receiver")
    orig_warning = recv_logger.warning
    try:
        recv_logger.warning = lambda msg, *a, **kw: warn_msgs.append(
            msg % a if a else msg
        )

        # 1. First failure at t=0 → should warn
        rcv._lpr_engine = None
        payload = {"type": "plate", "src": "edge-01", "dst": "edge-02",
                   "camera_id": "cam_01", "stid": [0, 1], "frame_no": 1,
                   "jpeg": b"fake", "confidence": 0.9, "ts": 1.0}
        rcv._on_plate_sample(_FakeSample(payload))
        for _ in range(50):
            time.sleep(0.001)
            if rcv.offload_processed_count >= 1:
                break
        assert rcv.offload_inference_errors_count == 1
        assert len(warn_msgs) == 1
        assert "LPR inference error #1" in warn_msgs[0]
        assert "engine not loaded" in warn_msgs[0].lower()

        # 2. Second failure at t=1 (within 5s interval) → NO warning
        mock_time[0] = 1.0
        payload = {"type": "plate", "src": "edge-01", "dst": "edge-02",
                   "camera_id": "cam_01", "stid": [0, 2], "frame_no": 2,
                   "jpeg": b"fake", "confidence": 0.9, "ts": 2.0}
        rcv._on_plate_sample(_FakeSample(payload))
        for _ in range(50):
            time.sleep(0.001)
            if rcv.offload_processed_count >= 2:
                break
        assert rcv.offload_inference_errors_count == 2
        assert len(warn_msgs) == 1  # still only 1 warning

        # 3. Third failure at t=6 (beyond 5s interval) → warning again
        mock_time[0] = 6.0
        payload = {"type": "plate", "src": "edge-01", "dst": "edge-02",
                   "camera_id": "cam_01", "stid": [0, 3], "frame_no": 3,
                   "jpeg": b"fake", "confidence": 0.9, "ts": 3.0}
        rcv._on_plate_sample(_FakeSample(payload))
        for _ in range(50):
            time.sleep(0.001)
            if rcv.offload_processed_count >= 3:
                break
        assert rcv.offload_inference_errors_count == 3
        assert len(warn_msgs) == 2
        assert "LPR inference error #3" in warn_msgs[1]

        # 4. LPD engine unavailable with exception reason also produces concise warning
        mock_time[0] = 12.0  # beyond interval again
        rcv._lpd_engine = _make_failing_lpd_engine()
        payload = {"type": "vehicle", "src": "edge-01", "dst": "edge-02",
                   "camera_id": "cam_01", "stid": [0, 4], "frame_no": 4,
                   "jpeg": b"fake", "confidence": 0.9, "ts": 4.0}
        rcv._on_vehicle_sample(_FakeSample(payload))
        for _ in range(50):
            time.sleep(0.001)
            if rcv.offload_processed_count >= 4:
                break
        assert rcv.offload_inference_errors_count == 4
        assert len(warn_msgs) == 3
        assert "LPD inference error #4" in warn_msgs[2]
        assert "simulated lpd engine crash" in warn_msgs[2].lower()

    finally:
        time.monotonic = orig_monotonic
        recv_logger.warning = orig_warning

    rcv.stop()
    print("  PASS  test_receiver_inference_error_warning_rate_limited")


# ======================================================================
# SpeedProbe.inject_offload_result tests
# ======================================================================

def test_probe_results_received_counter():
    probe_mod = _load("probes", "speedflow_python/probes.py")
    import queue

    # Need a minimal CameraManager stub
    class _StubCamCfg:
        camera_id = "cam_01"
        roi_polygon = None
        source_points = None
        fps = 30.0
        homo_matrix = np.eye(3, dtype=np.float32)
        min_track_age_frames = 15
        speed_limit_kmh = 50

    class _StubCameraManager:
        def get_config(self, source_id):
            return _StubCamCfg()

    probe = probe_mod.SpeedProbe(camera_manager=_StubCameraManager(), cooldown_s=0.1)
    # Wire the stub input counter
    class _InputCounter:
        def drain(self):
            return {"cam_01": 30}
    probe.set_input_counter(_InputCounter())

    # Initially 0
    assert probe._results_received == 0

    # Inject 3 results
    probe.inject_offload_result({
        "stid": [0, 1], "camera_id": "cam_01", "frame_no": 1,
        "plate_text": "ABC123", "confidence": 0.9, "ts": 1.0
    })
    probe.inject_offload_result({
        "stid": [0, 2], "camera_id": "cam_01", "frame_no": 2,
        "plate_text": "XYZ789", "confidence": 0.8, "ts": 2.0
    })
    probe.inject_offload_result({
        "stid": [0, 3], "camera_id": "cam_01", "frame_no": 3,
        "plate_text": "QWE456", "confidence": 0.7, "ts": 3.0
    })

    assert probe._results_received == 3

    # Inject when full -> silently discarded (no error)
    probe._offload_result_q = queue.Queue(maxsize=1)
    probe._offload_result_q.put_nowait({"existing": True})
    probe.inject_offload_result({
        "stid": [0, 4], "camera_id": "cam_01", "frame_no": 4,
        "plate_text": "DISCARD", "confidence": 0.5, "ts": 4.0
    })
    assert probe._results_received == 3  # not incremented

    probe.stop_fps_writer()
    print("  PASS  test_probe_results_received_counter")


def test_writer_surfaces_all_counters():
    """Verify _fps_writer_loop builds _offload_crops with all lifetime counters."""
    probe_mod = _load("probes", "speedflow_python/probes.py")

    class _StubCamCfg:
        camera_id = "cam_01"
        roi_polygon = None
        source_points = None
        fps = 30.0
        homo_matrix = np.eye(3, dtype=np.float32)
        min_track_age_frames = 15
        speed_limit_kmh = 50

    class _StubCameraManager:
        def get_config(self, source_id):
            return _StubCamCfg()

    probe = probe_mod.SpeedProbe(camera_manager=_StubCameraManager(), cooldown_s=0.1)
    class _InputCounter:
        def drain(self):
            return {"cam_01": 30}
    probe.set_input_counter(_InputCounter())

    # Fake publisher + receiver with known counter values
    class _FakePub:
        offload_encoded_count = 100
        offload_enqueued_count = 95
        offload_sent_count = 90
        offload_dropped_count = 5
        offload_send_errors_count = 2

    class _FakeRcv:
        offload_processed_count = 80
        offload_received_count = 100
        offload_queue_dropped_count = 5
        offload_errors_count = 3
        offload_results_sent_count = 78

    probe.set_offload_publisher(_FakePub())
    probe.set_offload_receiver(_FakeRcv())
    probe._results_received = 77  # probe-side count

    offload_crops, _, _ = probe._snapshot_offload_crops(0, 0.0)

    # Verify all keys present with expected values
    assert offload_crops["processed_count"] == 80
    assert offload_crops["offload_received_count"] == 100
    assert offload_crops["offload_queue_dropped_count"] == 5
    assert offload_crops["offload_errors_count"] == 3
    assert offload_crops["offload_results_sent_count"] == 78
    assert offload_crops["offload_encoded_count"] == 100
    assert offload_crops["offload_enqueued_count"] == 95
    assert offload_crops["offload_sent_count"] == 90
    assert offload_crops["offload_dropped_count"] == 5
    assert offload_crops["offload_send_errors_count"] == 2
    assert offload_crops["results_received"] == 77
    # processed_count and received_per_s backward compat
    assert "processed_count" in offload_crops
    assert "received_per_s" in offload_crops

    probe.stop_fps_writer()
    print("  PASS  test_writer_surfaces_all_counters")


# ======================================================================
# Producer-gate counter fakes for invoking osd_sink_pad_buffer_probe
# ======================================================================

class _Rect:
    def __init__(self, left=10, top=10, width=50, height=40):
        self.left, self.top, self.width, self.height = left, top, width, height

class _FontParams:
    font_size = 10
    font_name = "Serif"

class _TextParams:
    display_text = ""
    font_params = _FontParams()

class _ObjMeta:
    def __init__(self, class_id, object_id, rect=None, conf=0.9):
        self.class_id = class_id
        self.object_id = object_id
        self.rect_params = rect or _Rect()
        self.confidence = conf
        self.text_params = _TextParams()
        # classifier_meta_list is checked only when _extract_lpr_text runs
        # (L2 local accumulation path). For L3 and vehicle-only frames it is
        # never touched.
        self.classifier_meta_list = None

class _LL:
    """Fake linked-list node: .data + .next, for pyds frame/object lists."""
    def __init__(self, items):
        self._items = list(items)
        self._i = 0

    def __bool__(self):
        return self._i < len(self._items)

    @property
    def data(self):
        return self._items[self._i]

    @property
    def next(self):
        self._i += 1
        if self._i < len(self._items):
            return self
        return None

class _FrameMeta:
    """Pyds frame_meta fake. 'obj_meta_list', 'next' wired via _LL."""
    def __init__(self, frame_num=1, source_id=0, objs=()):
        self.frame_num = frame_num
        self.source_id = source_id
        self.ntp_timestamp = 0    # → falls back to time.time()
        self.batch_id = 0
        self.obj_meta_list = _LL(objs)

class _BatchMeta:
    """Minimal pyds batch_meta fake."""
    def __init__(self, frame_meta):
        self.frame_meta_list = _LL([frame_meta])

class _FakeBuffer:
    """Minimal Gst.Buffer fake so hash() is stable."""
    pass

class _FakeInfo:
    def __init__(self, buf):
        self._buf = buf
    def get_buffer(self):
        return self._buf

class _FakeOrch:
    """Fake PeerOrchestrator returning a fixed offload level + target."""
    def __init__(self, level, target):
        self._level  = level
        self._target = target
    def get_offload_level(self, camera_id):
        return self._level
    def get_offload_target(self, camera_id):
        return self._target

class _RecordingPub:
    """Records put_plate / put_vehicle calls for test assertions."""
    def __init__(self):
        self.plate_calls = []
        self.vehicle_calls = []
    def put_plate(self, **kw):
        self.plate_calls.append(kw)
    def put_vehicle(self, **kw):
        self.vehicle_calls.append(kw)


def _probe_with_fakes(probe_mod, orch_level, orch_target="edge-02"):
    """Construct a SpeedProbe wired with cam_mgr, peer_orch + publisher."""
    import numpy as np
    class _StubCamCfg:
        camera_id = "cam_01"
        roi_polygon = None
        source_points = None
        fps = 30.0
        homo_matrix = np.eye(3, dtype=np.float32)
        speed_limit_kmh = 50
        min_track_age_frames = 15
    class _StubCamMgr:
        def get_config(self, source_id):
            return _StubCamCfg()

    probe = probe_mod.SpeedProbe(
        camera_manager=_StubCamMgr(), cooldown_s=0.1,
        peer_orch=_FakeOrch(orch_level, orch_target),
    )
    pub = _RecordingPub()
    probe.set_offload_publisher(pub)
    return probe, pub


def _call_probe_frame(probe, objs, surface):
    """Drive osd_sink_pad_buffer_probe once with fakes.

    surface: numpy ndarray (→ valid crop), None (→ surface unavailable),
             or 'raise' (→ exception path).
    """
    import pyds
    frame_meta = _FrameMeta(objs=objs)
    batch  = _BatchMeta(frame_meta)
    buffer = _FakeBuffer()
    info   = _FakeInfo(buffer)

    pyds.gst_buffer_get_nvds_batch_meta = lambda h: batch

    if isinstance(surface, str) and surface == "raise":
        def _boom(*a, **k):
            raise RuntimeError("surface fetch failed")
        probe._get_frame_bgr_cached = _boom
    else:
        probe._get_frame_bgr_cached = lambda *a, **k: surface

    probe.osd_sink_pad_buffer_probe(None, info, None)


# ======================================================================
# Gate counter tests (L2 / L3 producer-side)
# ======================================================================

def test_gate_counters_l2_active_no_objects():
    """L2 active frame with zero vehicles → active=1, vehicle_objects=0."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe, pub = _probe_with_fakes(probe_mod, orch_level=2)

    # Empty frame: no L2 harvest, but counted as active.
    _call_probe_frame(probe, objs=[], surface=np.ones((120, 160, 3), dtype=np.uint8))

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert crops.get("l2_active_frames", 0) == 1
    assert crops.get("l2_vehicle_objects", 0) == 0
    assert crops.get("l2_valid_crops", 0) == 0
    assert crops.get("l2_surface_unavailable", 0) == 0
    assert crops.get("l2_crop_errors", 0) == 0
    assert len(pub.vehicle_calls) == 0

    probe.stop_fps_writer()
    print("  PASS  test_gate_counters_l2_active_no_objects")


def test_gate_counters_l2_surface_unavailable():
    """L2 with a vehicle but surface returns None."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe, pub = _probe_with_fakes(probe_mod, orch_level=2)

    veh = _ObjMeta(class_id=2, object_id=42, rect=_Rect(10, 10, 50, 40))
    _call_probe_frame(probe, objs=[veh], surface=None)

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert crops.get("l2_active_frames", 0) == 1
    assert crops.get("l2_vehicle_objects", 0) == 1
    assert crops.get("l2_surface_unavailable", 0) == 1
    assert crops.get("l2_valid_crops", 0) == 0
    assert crops.get("l2_crop_errors", 0) == 0
    assert len(pub.vehicle_calls) == 0

    probe.stop_fps_writer()
    print("  PASS  test_gate_counters_l2_surface_unavailable")


def test_gate_counters_l2_valid_crop_put_path():
    """L2 vehicle + valid surface → valid_crops=1, put_vehicle called."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe, pub = _probe_with_fakes(probe_mod, orch_level=2)

    veh = _ObjMeta(class_id=2, object_id=42, rect=_Rect(10, 10, 50, 40))
    _call_probe_frame(probe, objs=[veh],
                       surface=np.ones((120, 160, 3), dtype=np.uint8))

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert crops.get("l2_active_frames", 0) == 1
    assert crops.get("l2_vehicle_objects", 0) == 1
    assert crops.get("l2_valid_crops", 0) == 1
    assert crops.get("l2_surface_unavailable", 0) == 0
    assert crops.get("l2_crop_errors", 0) == 0
    assert len(pub.vehicle_calls) == 1

    probe.stop_fps_writer()
    print("  PASS  test_gate_counters_l2_valid_crop_put_path")


def test_gate_counters_l2_crop_errors():
    """L2 surface raises → crop_errors incremented."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe, pub = _probe_with_fakes(probe_mod, orch_level=2)

    veh = _ObjMeta(class_id=2, object_id=42, rect=_Rect(10, 10, 50, 40))
    _call_probe_frame(probe, objs=[veh], surface="raise")

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert crops.get("l2_active_frames", 0) == 1
    assert crops.get("l2_vehicle_objects", 0) == 1
    assert crops.get("l2_crop_errors", 0) == 1
    assert crops.get("l2_valid_crops", 0) == 0
    assert len(pub.vehicle_calls) == 0

    probe.stop_fps_writer()
    print("  PASS  test_gate_counters_l2_crop_errors")


def test_gate_counters_l3_active_no_plates():
    """L3 active frame with zero plates → active=1, plate_objects=0."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe, pub = _probe_with_fakes(probe_mod, orch_level=3)

    # Vehicle present for association, but no plate objects → 0 plate_objects.
    veh = _ObjMeta(class_id=2, object_id=42, rect=_Rect(10, 10, 50, 40))
    _call_probe_frame(probe, objs=[veh], surface=np.ones((120, 160, 3), dtype=np.uint8))

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert crops.get("l3_active_frames", 0) == 1
    assert crops.get("l3_plate_objects", 0) == 0
    assert crops.get("l3_valid_crops", 0) == 0
    assert crops.get("l3_surface_unavailable", 0) == 0
    assert crops.get("l3_crop_errors", 0) == 0
    assert len(pub.plate_calls) == 0

    probe.stop_fps_writer()
    print("  PASS  test_gate_counters_l3_active_no_plates")


def test_gate_counters_l3_surface_unavailable():
    """L3 plate + vehicle, surface None → surface_unavailable=1."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe, pub = _probe_with_fakes(probe_mod, orch_level=3)

    # Plate bbox intersects the vehicle for association.
    plate = _ObjMeta(class_id=0, object_id=99,
                     rect=_Rect(20, 15, 30, 10), conf=0.8)
    veh   = _ObjMeta(class_id=2, object_id=42,
                     rect=_Rect(10, 10, 50, 40))
    _call_probe_frame(probe, objs=[plate, veh], surface=None)

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert crops.get("l3_active_frames", 0) == 1
    assert crops.get("l3_plate_objects", 0) == 1
    assert crops.get("l3_surface_unavailable", 0) == 1
    assert crops.get("l3_valid_crops", 0) == 0
    assert crops.get("l3_crop_errors", 0) == 0
    assert len(pub.plate_calls) == 0

    probe.stop_fps_writer()
    print("  PASS  test_gate_counters_l3_surface_unavailable")


def test_gate_counters_l3_valid_crop_put_path():
    """L3 plate + vehicle + valid surface → valid_crops=1, put_plate called."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe, pub = _probe_with_fakes(probe_mod, orch_level=3)

    plate = _ObjMeta(class_id=0, object_id=99,
                     rect=_Rect(20, 15, 30, 10), conf=0.8)
    veh   = _ObjMeta(class_id=2, object_id=42,
                     rect=_Rect(10, 10, 50, 40))
    _call_probe_frame(probe, objs=[plate, veh],
                       surface=np.ones((120, 160, 3), dtype=np.uint8))

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert crops.get("l3_active_frames", 0) == 1
    assert crops.get("l3_plate_objects", 0) == 1
    assert crops.get("l3_valid_crops", 0) == 1
    assert crops.get("l3_surface_unavailable", 0) == 0
    assert crops.get("l3_crop_errors", 0) == 0
    assert len(pub.plate_calls) == 1

    probe.stop_fps_writer()
    print("  PASS  test_gate_counters_l3_valid_crop_put_path")


def test_gate_counters_l3_crop_errors():
    """L3 surface raises → crop_errors incremented."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe, pub = _probe_with_fakes(probe_mod, orch_level=3)

    plate = _ObjMeta(class_id=0, object_id=99,
                     rect=_Rect(20, 15, 30, 10), conf=0.8)
    veh   = _ObjMeta(class_id=2, object_id=42,
                     rect=_Rect(10, 10, 50, 40))
    _call_probe_frame(probe, objs=[plate, veh], surface="raise")

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert crops.get("l3_active_frames", 0) == 1
    assert crops.get("l3_plate_objects", 0) == 1
    assert crops.get("l3_crop_errors", 0) == 1
    assert crops.get("l3_valid_crops", 0) == 0
    assert len(pub.plate_calls) == 0

    probe.stop_fps_writer()
    print("  PASS  test_gate_counters_l3_crop_errors")


def test_gate_counters_snapshot_exposure():
    """_snapshot_offload_crops exposes exactly 10 gate-counter keys
    with correct values after increments."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe = _probe_with_fakes(probe_mod, orch_level=2)[0]

    # Drive increments via _gate_inc → the writer can see them.
    probe._gate_inc("l2_active_frames", 5)
    probe._gate_inc("l2_vehicle_objects", 2)
    probe._gate_inc("l3_valid_crops", 7)

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert crops.get("l2_active_frames", 0) == 5
    assert crops.get("l2_vehicle_objects", 0) == 2
    assert crops.get("l3_active_frames", 0) == 0
    assert crops.get("l3_valid_crops", 0) == 7

    # All 10 gate keys present (some may be 0)
    expected_keys = {
        "l2_active_frames", "l2_vehicle_objects", "l2_surface_unavailable",
        "l2_valid_crops", "l2_crop_errors",
        "l3_active_frames", "l3_plate_objects", "l3_surface_unavailable",
        "l3_valid_crops", "l3_crop_errors",
    }
    for k in expected_keys:
        assert k in crops, f"missing gate key '{k}' in snapshot"
        assert isinstance(crops[k], int), f"gate {k} should be int, got {type(crops[k])}"

    probe.stop_fps_writer()
    print("  PASS  test_gate_counters_snapshot_exposure")


# ======================================================================
# Crop error type telemetry tests
# ======================================================================

def test_crop_error_types_aggregation():
    """_record_crop_error_type correctly counts per-exception-type."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe = _probe_with_fakes(probe_mod, orch_level=2)[0]

    class _ErrA(Exception): pass
    class _ErrB(Exception): pass

    probe._record_crop_error_type("l2", _ErrA("a1"))
    probe._record_crop_error_type("l2", _ErrA("a2"))
    probe._record_crop_error_type("l2", _ErrB("b"))

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    types = crops.get("l2_crop_error_types", {})
    assert types.get("_ErrA") == 2, f"expected _ErrA=2, got {types}"
    assert types.get("_ErrB") == 1, f"expected _ErrB=1, got {types}"

    # l3 independent: zero when never touched
    assert crops.get("l3_crop_error_types", {}) == {}

    probe.stop_fps_writer()
    print("  PASS  test_crop_error_types_aggregation")


def test_crop_error_types_cap():
    """Crop error type dict is capped at 16 distinct class names."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe = _probe_with_fakes(probe_mod, orch_level=2)[0]

    for i in range(22):
        name = f"ErrType{i:03d}"
        cls = type(name, (Exception,), {})
        probe._record_crop_error_type("l2", cls(f"msg{i}"))

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    types = crops.get("l2_crop_error_types", {})
    assert len(types) == 16, f"expected 16, got {len(types)}: {list(types)}"
    assert "ErrType000" in types
    assert "ErrType015" in types
    # 17th onward not added
    assert "ErrType016" not in types
    assert "ErrType021" not in types

    probe.stop_fps_writer()
    print("  PASS  test_crop_error_types_cap")


def test_crop_error_types_snapshot_exposure():
    """_snapshot_offload_crops exposes l2/l3_crop_error_types as dicts."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe = _probe_with_fakes(probe_mod, orch_level=2)[0]

    probe._record_crop_error_type("l2", ValueError("bad value"))
    probe._record_crop_error_type("l3", TypeError("bad type"))

    crops, _, _ = probe._snapshot_offload_crops(0, 0.0)
    assert "l2_crop_error_types" in crops, "missing l2_crop_error_types in snapshot"
    assert "l3_crop_error_types" in crops, "missing l3_crop_error_types in snapshot"
    l2t = crops["l2_crop_error_types"]
    l3t = crops["l3_crop_error_types"]
    assert isinstance(l2t, dict)
    assert isinstance(l3t, dict)
    assert l2t.get("ValueError") == 1, f"got {l2t}"
    assert l3t.get("TypeError") == 1, f"got {l3t}"

    probe.stop_fps_writer()
    print("  PASS  test_crop_error_types_snapshot_exposure")


def test_crop_error_warning_rate_limit():
    """Warning fires on 1st error and every 100th; no traceback spam."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    probe = _probe_with_fakes(probe_mod, orch_level=2)[0]

    import logging
    warn_msgs = []
    probe_logger = logging.getLogger("speedflow_python.probes")
    orig_warning = probe_logger.warning
    try:
        probe_logger.warning = lambda msg, *a, **kw: warn_msgs.append(
            msg % a if a else msg
        )

        # 250 L2 errors → warnings at 1, 100, 200 (first + every 100th)
        for i in range(250):
            probe._record_crop_error_type("l2", RuntimeError(f"err{i}"))

        assert len(warn_msgs) == 3, f"expected 3 warns, got {len(warn_msgs)}"
        assert "error #1" in warn_msgs[0], warn_msgs[0]
        assert "error #100" in warn_msgs[1], warn_msgs[1]
        assert "error #200" in warn_msgs[2], warn_msgs[2]
    finally:
        probe_logger.warning = orig_warning

    probe.stop_fps_writer()
    print("  PASS  test_crop_error_warning_rate_limit")


# ======================================================================
# Main
# ======================================================================

if __name__ == "__main__":
    tests = [
        test_publisher_counters_encode_enqueue_sent,
        test_publisher_dropped_counter_when_queue_full,
        test_publisher_send_errors_counter,
        test_publisher_backward_compat,
        test_receiver_counters_received_processed,
        test_receiver_queue_dropped_counter,
        test_receiver_results_sent_counter,
        test_receiver_all_counter_accessors_exist,
        test_receiver_lpr_inference_failure_suppresses_publish,
        test_receiver_lpd_inference_failure_suppresses_publish,
        test_receiver_lpd_no_bbox_valid_empty_publishes,
        test_receiver_lpr_empty_plate_text_valid_publishes,
        test_receiver_inference_error_counter_accuracy,
        test_receiver_lpr_engine_unavailable_suppresses_publish,
        test_receiver_lpd_engine_unavailable_suppresses_publish,
        test_receiver_inference_error_warning_rate_limited,
        test_probe_results_received_counter,
        test_writer_surfaces_all_counters,
        test_gate_counters_l2_active_no_objects,
        test_gate_counters_l2_surface_unavailable,
        test_gate_counters_l2_valid_crop_put_path,
        test_gate_counters_l2_crop_errors,
        test_gate_counters_l3_active_no_plates,
        test_gate_counters_l3_surface_unavailable,
        test_gate_counters_l3_valid_crop_put_path,
        test_gate_counters_l3_crop_errors,
        test_gate_counters_snapshot_exposure,
        test_crop_error_types_aggregation,
        test_crop_error_types_cap,
        test_crop_error_types_snapshot_exposure,
        test_crop_error_warning_rate_limit,
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
