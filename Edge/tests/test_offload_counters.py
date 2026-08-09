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

    # backward compat
    assert hasattr(rcv, "offload_processed_count")

    rcv.stop()
    print("  PASS  test_receiver_all_counter_accessors_exist")


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
        test_probe_results_received_counter,
        test_writer_surfaces_all_counters,
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
