#!/usr/bin/env python3
"""
Python backend runner.
Uses speedflow_python pipeline + GStreamer pad probes for processing.
Hỗ trợ Multi-Stream Dynamic.
"""
import sys
import os
import logging
import time
import threading

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from .core_pipeline import build_pipeline, dynamic_add_stream, dynamic_remove_stream
from .camera_config import CameraManager
from .probes import SpeedProbe, ROIFilterProbe
from .plate_preprocessor import PlatePreprocessorProbe
from . import settings as S
from .settings import (
    CAMERAS_YML,
    NODE_ID,
    ADVERTISE_IP,
    MUX_WIDTH,
    MUX_HEIGHT,
    MONITOR_URL,
    FPS_STATS_FILE,
    TARGET_FPS,
    HEALTH_INTERVAL,
)

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared probe setup
# ---------------------------------------------------------------------------

def _setup_probes(pipeline: Gst.Pipeline, nvdsosd: Gst.Element,
                  camera_manager: CameraManager,
                  peer_orch=None,
                  offload_pub=None) -> SpeedProbe:
    """
    Attach ROI filter, plate preprocessor, and speed probe to *pipeline*.
    Returns the SpeedProbe instance.

    peer_orch:   PeerOrchestrator — lets the probe query offload levels.
    offload_pub: OffloadPublisher — lets the probe send crops to peers.
    """
    # 1. ROI filter
    analytics = pipeline.get_by_name("analytics")
    if analytics:
        roi_filter = ROIFilterProbe(camera_manager)
        analytics_srcpad = analytics.get_static_pad("src")
        if analytics_srcpad:
            analytics_srcpad.add_probe(
                Gst.PadProbeType.BUFFER,
                roi_filter.analytics_src_pad_buffer_probe,
                None,
            )
            print("[ROI Filter] Enabled (Multi-Stream Python Mode)")

    # 2. Plate preprocessor
    tracker = pipeline.get_by_name("tracker")
    if tracker:
        plate_preprocessor = PlatePreprocessorProbe(
            enable_sharpening=True,
            enable_contrast=True,
            enable_denoise=True,
            adaptive_mode=True,
        )
        tracker_srcpad = tracker.get_static_pad("src")
        if tracker_srcpad:
            tracker_srcpad.add_probe(
                Gst.PadProbeType.BUFFER,
                plate_preprocessor.buffer_probe,
                None,
            )
            print("[Plate Preprocessor] Enabled")

    # 3. Speed + LPR probe (pass peer_orch so it can query offload levels)
    probe = SpeedProbe(camera_manager, peer_orch=peer_orch)
    if offload_pub is not None:
        probe.set_offload_publisher(offload_pub)

    tiler = pipeline.get_by_name("tiler")
    if tiler:
        pad = tiler.get_static_pad("sink")
        pad_name = "tiler sink"
    else:
        pad = nvdsosd.get_static_pad("sink")
        pad_name = "nvdsosd sink"

    if not pad:
        print(f"ERROR: Unable to get {pad_name} pad", file=sys.stderr)
        sys.exit(1)
    pad.add_probe(Gst.PadProbeType.BUFFER, probe.osd_sink_pad_buffer_probe, None)

    return probe


# ---------------------------------------------------------------------------
# Dynamic Hooks Setup
# ---------------------------------------------------------------------------

def _attach_camera_manager(
    camera_manager: CameraManager,
    pipeline: Gst.Pipeline,
    streammux: Gst.Element,
    source_bins: dict,
    tiler: Gst.Element = None
):
    """
    Hooks up the CameraManager to safely add/remove streams dynamically.

    Thread-safety note:
        source_id_to_cam_id is ONLY written inside on_add/on_remove,
        which are always called via GLib.idle_add → run on GLib Main Loop
        thread → no concurrent mutation possible.
    """
    # Mapping ngược: source_id (int) → camera_id (str)
    # Khởi tạo từ các camera đã được enabled khi pipeline bắt đầu.
    # Chỉ được đọc/ghi từ GLib Main Loop thread (thông qua idle_add).
    source_id_to_cam_id: dict[int, str] = {
        cfg.source_id: cfg.camera_id
        for cfg in camera_manager.get_enabled_configs()
    }

    def on_add(cam_cfg):
        current_n = streammux.get_property("batch-size")
        print(f"[Dynamic] Adding camera '{cam_cfg.camera_id}' (source_id={cam_cfg.source_id})")
        dynamic_add_stream(pipeline, streammux, cam_cfg, tiler, source_bins, current_n)
        # Đăng ký ánh xạ ngay sau khi thêm thành công.
        # Hàm này chạy trong GLib Main Loop → an toàn, không cần lock.
        source_id_to_cam_id[cam_cfg.source_id] = cam_cfg.camera_id

    def on_remove(source_id):
        # Tra cứu camera_id từ dict ánh xạ —
        # không dùng GStreamer pad scan vì phức tạp và không thread-safe.
        cam_id = source_id_to_cam_id.get(source_id)
        if cam_id is None:
            print(
                f"[Dynamic] WARN: No camera mapped to source_id={source_id}. "
                "Possibly already removed or never registered.",
                file=sys.stderr
            )
            return

        current_n = streammux.get_property("batch-size")
        print(f"[Dynamic] Removing camera '{cam_id}' (source_id={source_id})")
        dynamic_remove_stream(pipeline, streammux, cam_id, source_id, tiler, source_bins, current_n)

        # Dọn dẹp key sau khi xóa thành công.
        # Phòng tránh memory leak khi hệ thống chạy liên tục nhiều tháng
        # và xung đột source_id nếu cùng ID được tái sử dụng sau này.
        removed = source_id_to_cam_id.pop(source_id, None)
        if removed:
            print(f"[Dynamic] Cleaned up mapping: source_id={source_id} → '{removed}'")

    camera_manager.start(on_add, on_remove, GLib.idle_add)


# ---------------------------------------------------------------------------
# GLib bus helpers
# ---------------------------------------------------------------------------

def _run_loop_until_eos_or_error(
    pipeline: Gst.Pipeline,
    camera_manager: CameraManager
) -> None:
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"ERROR from {message.src.get_name()}: {err}", file=sys.stderr)
            if debug:
                print(f"DEBUG INFO: {debug}", file=sys.stderr)
            loop.quit()
        elif t == Gst.MessageType.EOS:
            print("EOS received — processing complete")
            loop.quit()

    bus.connect("message", on_message)

    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        camera_manager.stop()
        pipeline.set_state(Gst.State.NULL)
        print("Pipeline stopped")


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_display_mode(args, camera_manager: CameraManager, peer_orch=None, offload_pub=None) -> None:
    Gst.init(None)
    configs = camera_manager.get_enabled_configs()

    ret_build = build_pipeline(
        camera_configs=configs,
        sink_type="display",
        mux_width=args.width,
        mux_height=args.height,
    )
    pipeline, nvdsosd, streammux, source_bins = ret_build
    tiler = pipeline.get_by_name("tiler")

    probe = _setup_probes(pipeline, nvdsosd, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub)
    _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, tiler)

    t0_playing = time.monotonic()
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("ERROR: Unable to set pipeline to PLAYING state", file=sys.stderr)
        sys.exit(1)
    warmup_ms = (time.monotonic() - t0_playing) * 1000.0
    probe.record_warmup_ms(warmup_ms)
    logger.info("[Display] Pipeline PLAYING after %.0f ms (warmup)", warmup_ms)

    _run_loop_until_eos_or_error(pipeline, camera_manager)


def run_file_mode(args, camera_manager: CameraManager, peer_orch=None, offload_pub=None) -> None:
    Gst.init(None)
    configs = camera_manager.get_enabled_configs()

    ret_build = build_pipeline(
        camera_configs=configs,
        sink_type="file",
        mux_width=args.width,
        mux_height=args.height,
    )
    pipeline, nvdsosd, streammux, source_bins = ret_build

    _setup_probes(pipeline, nvdsosd, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub)
    _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, None)

    print(f"[Python File Mode] Processing multi-streams to output files...")
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("ERROR: Unable to set pipeline to PLAYING state", file=sys.stderr)
        sys.exit(1)

    _run_loop_until_eos_or_error(pipeline, camera_manager)


# ---------------------------------------------------------------------------
# Health push — periodic metrics to Monitor Server via MonitorClient
# ---------------------------------------------------------------------------

def _health_push_loop(peer_orch=None) -> None:
    import json as _json
    import time as _time
    from pathlib import Path as _Path
    import msgpack as _msgpack
    import yaml as _yaml

    # BUG-4 / BUG-9 fix: delegate metric collection and load-score computation
    # to health_agent's functions so that:
    #   - Adaptive omega weights (thermal / bandwidth / normal) are applied.
    #   - The CPU formula is identical to health_agent.py (100 - idle%), not
    #     the incorrect per-core average that was here before.
    # This ensures peers/status heartbeats always carry a consistent load score
    # regardless of which process (main.py or health_agent.py) publishes them.
    from health_agent import (
        _collect_jetson_metrics as _collect_metrics_fn,
        _compute_load_score     as _compute_load_fn,
        _read_fps_stats         as _read_fps_fn,
    )

    _fps_file = _Path(FPS_STATS_FILE)

    # Load camera configs once so peers know the original URIs for failover
    _cam_configs = {}
    try:
        with open(CAMERAS_YML, "r", encoding="utf-8") as f:
            raw = _yaml.safe_load(f)
        for cam_id, cfg in raw.get("cameras", {}).items():
            if cfg and cfg.get("enabled", True):
                _cam_configs[cam_id] = {
                    "camera_id":       cam_id,
                    "source_id":       int(cfg.get("source_id", 0)),
                    "uri":             cfg.get("uri", ""),
                    "name":            cfg.get("name", cam_id),
                    "fps":             float(cfg.get("fps", 25.0)),
                    "speed_limit_kmh": float(cfg.get("speed_limit_kmh", 80.0)),
                    "homography":      cfg.get("homography", {}),
                    "roi_polygon":     cfg.get("roi_polygon", []),
                    "output":          cfg.get("output", {}),
                }
    except Exception:
        pass

    # Open persistent jtop session for Jetson GPU/CPU/RAM metrics
    _jtop = None
    try:
        from jtop import jtop as _JTop
        _jtop = _JTop()
        _jtop.start()
    except Exception:
        pass

    # Monkey-patch the module-level jtop reference so _collect_metrics_fn
    # picks up our persistent session instead of opening a new one.
    import health_agent as _ha
    _ha_orig_jtop = _ha.HealthAgent  # keep reference; we don't modify the class

    # Create a minimal stub so _collect_metrics_fn can use _jtop
    class _JtopWrapper:
        def __init__(self, j):
            self._j = j
        @property
        def stats(self):    return self._j.stats if self._j else None
        @property
        def memory(self):   return self._j.memory if self._j else None
        @property
        def temperature(self): return self._j.temperature if self._j else {}
        @property
        def power(self):    return self._j.power if self._j else {}
        @property
        def cpu(self):      return self._j.cpu if self._j else {}

    _jtop_wrapper = _JtopWrapper(_jtop) if _jtop else None

    while True:
        try:
            # Use health_agent's collection + scoring functions
            metrics    = _collect_metrics_fn() if _jtop_wrapper is None else _collect_via_jtop(_jtop_wrapper)
            fps_stats  = _read_fps_fn()
            load_score, omega_preset = _compute_load_fn(metrics, fps_stats)

            active_fps_vals = [v for v in fps_stats.values() if v > 0.0]
            avg_fps = round(sum(active_fps_vals) / len(active_fps_vals), 1) if active_fps_vals else None

            payload = {
                "type":         "health",
                "node_id":      NODE_ID,
                "timestamp":    _time.time(),
                "load_score":   load_score,
                "omega_preset": omega_preset,
                "gpu_percent":  metrics["gpu_percent"],
                "cpu_percent":  metrics["cpu_percent"],
                "ram_percent":  metrics["ram_percent"],
                "gpu_temp_c":   metrics["gpu_temp_c"],
                "power_mw":     metrics.get("power_mw", 0.0),
                "pipeline": {
                    "fps_per_camera":  fps_stats,
                    "avg_fps":         avg_fps,
                    # BUG-15 fix: only report cameras that are actively producing frames
                    "active_cameras":  [k for k, v in fps_stats.items() if v > 0.0],
                    "camera_configs":  _cam_configs,
                },
            }

            # Push to Dashboard via WebSocket
            from speedflow_python.monitor_client import send_to_monitor
            send_to_monitor(payload)

            # Publish to Zenoh via PeerOrchestrator's session so peers
            # see active_cameras (uses single shared Zenoh session).
            if peer_orch is not None:
                peer_orch.publish_status(_msgpack.packb(payload, use_bin_type=True))

        except Exception as _exc:
            import logging as _logging
            _logging.getLogger("health_push").warning(
                "[HealthPush] Exception in health loop (will retry): %s", _exc, exc_info=True
            )
        _time.sleep(HEALTH_INTERVAL)


def _collect_via_jtop(jtop_wrapper) -> dict:
    """Collect Jetson metrics from a persistent jtop wrapper object."""
    try:
        stats = jtop_wrapper.stats
        mem   = jtop_wrapper.memory
        if stats is None or mem is None:
            raise ValueError("jtop not ready yet")

        gpu_pct = float(stats.get("GPU", 0))

        cpu_total = jtop_wrapper.cpu.get("total", {})
        cpu_idle  = cpu_total.get("idle", 100.0)
        cpu_pct   = 100.0 - cpu_idle

        ram_pct = float(mem["RAM"]["used"] / mem["RAM"]["tot"] * 100)

        temp = jtop_wrapper.temperature
        gpu_temp_info = temp.get("gpu", {})
        if gpu_temp_info.get("online", False) and gpu_temp_info.get("temp", -256) > -100:
            temp_c = float(gpu_temp_info["temp"])
        else:
            tj_info = temp.get("tj", {})
            temp_c = float(tj_info.get("temp", 0))

        power_mw = float(jtop_wrapper.power.get("tot", {}).get("power", 0))

        return {
            "gpu_percent": round(gpu_pct, 1),
            "cpu_percent": round(cpu_pct, 1),
            "ram_percent": round(ram_pct, 1),
            "gpu_temp_c":  round(temp_c, 1),
            "power_mw":    round(power_mw, 0),
            "source":      "jtop",
        }
    except Exception:
        import psutil
        try:
            return {
                "gpu_percent": 0.0,
                "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
                "ram_percent": round(psutil.virtual_memory().percent, 1),
                "gpu_temp_c":  0.0,
                "power_mw":    0.0,
                "source":      "psutil",
            }
        except Exception:
            return {"gpu_percent": 0.0, "cpu_percent": 0.0, "ram_percent": 0.0,
                    "gpu_temp_c": 0.0, "power_mw": 0.0, "source": "error"}


def run_rtsp_push_mode(args, camera_manager: CameraManager, peer_orch=None, offload_pub=None) -> None:
    Gst.init(None)
    configs = camera_manager.get_enabled_configs()

    rtsp_url = args.rtsp_push_url or S.RTSP_PUSH_URL
    if not rtsp_url:
        print("ERROR: --rtsp-push-url or RTSP_PUSH_URL required", file=sys.stderr)
        sys.exit(1)

    # Guard against two instances of this process publishing to the same path.
    # A second instance would cause MediaMTX to kick the first (overridePublisher)
    # leading to an endless reconnect loop and GLib-GIO-CRITICAL socket errors.
    _PID_FILE = S.ROOT / "run_python.pid"
    _my_pid = os.getpid()
    if _PID_FILE.exists():
        try:
            _old_pid = int(_PID_FILE.read_text().strip())
            if _old_pid != _my_pid:
                # Check if that process is actually alive
                try:
                    os.kill(_old_pid, 0)  # signal 0 = existence check
                    print(
                        f"WARNING: Another pipeline process (PID {_old_pid}) is already running. "
                        f"Two publishers to the same RTSP path will conflict. "
                        f"Kill the old process first: kill {_old_pid}",
                        file=sys.stderr,
                    )
                except OSError:
                    pass  # old process is dead — stale PID file, safe to continue
        except ValueError:
            pass
    _PID_FILE.write_text(str(_my_pid))

    import atexit
    atexit.register(lambda: _PID_FILE.unlink(missing_ok=True))

    _RESTART_DELAYS = [5, 10, 20, 30]
    restart_idx = 0

    while True:
        ret_build = build_pipeline(
            camera_configs=camera_manager.get_enabled_configs(),
            sink_type="rtsp_push",
            mux_width=args.width,
            mux_height=args.height,
            rtsp_push_url=rtsp_url,
            bitrate=S.RTSP_PUSH_BITRATE,
        )
        pipeline, nvdsosd, streammux, source_bins = ret_build
        tiler = pipeline.get_by_name("tiler")

        _setup_probes(pipeline, nvdsosd, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub)
        _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, tiler)

        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("ERROR: Unable to set pipeline to PLAYING state", file=sys.stderr)
            delay = _RESTART_DELAYS[min(restart_idx, len(_RESTART_DELAYS) - 1)]
            print(f"[RTSP Push] Retrying in {delay}s...")
            restart_idx += 1
            import time as _time; _time.sleep(delay)
            continue

        restart_idx = 0  # reset backoff on successful start
        print(f"[RTSP Push] Streaming to {rtsp_url}")

        loop = GLib.MainLoop()
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        _error_flag = [False]
        _removing = set()  # guard against double-remove from multiple error msgs

        def on_message(bus, message):
            t = message.type
            if t == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                src_name = message.src.get_name()
                print(f"ERROR from {src_name}: {err}", file=sys.stderr)
                if debug:
                    print(f"DEBUG INFO: {debug}", file=sys.stderr)

                # If the error comes from a source bin, remove just that
                # stream instead of killing the whole pipeline.
                cam_id = None
                elem = message.src
                while elem is not None:
                    n = elem.get_name()
                    if n.startswith("src-"):
                        cam_id = n[4:]
                        break
                    elem = elem.get_parent()

                if cam_id and cam_id in source_bins and cam_id not in _removing:
                    _removing.add(cam_id)
                    print(f"[Pipeline] Removing failed source '{cam_id}'", file=sys.stderr)
                    sid = None
                    for cfg in camera_manager.get_enabled_configs():
                        if cfg.camera_id == cam_id:
                            sid = cfg.source_id
                            break
                    if sid is not None:
                        current_n = streammux.get_property("batch-size")
                        dynamic_remove_stream(pipeline, streammux, cam_id, sid, tiler, source_bins, current_n)
                    return  # don't quit the pipeline

                _error_flag[0] = True
                loop.quit()
            elif t == Gst.MessageType.EOS:
                print("EOS received — processing complete")
                loop.quit()

        bus.connect("message", on_message)

        try:
            loop.run()
        except KeyboardInterrupt:
            print("\nInterrupted by user")
            # BUG-11 fix: stop the manager on intentional exit only (see below)
            camera_manager.stop()
            pipeline.set_state(Gst.State.NULL)
            print("Pipeline stopped")
            return
        finally:
            # BUG-11 fix: do NOT call camera_manager.stop() here on every
            # iteration — that kills the watchdog observer and processor thread
            # permanently.  On a restart those threads must stay alive so that
            # hot-reload and dynamic ADD/REMOVE keep working.
            # We only null the pipeline state; _attach_camera_manager() at the
            # top of the next iteration re-registers on_add/on_remove callbacks
            # and calls camera_manager.start() again (which is idempotent for
            # the watchdog if it is still running).
            pipeline.set_state(Gst.State.NULL)
            print("Pipeline stopped")

        if _error_flag[0]:
            delay = _RESTART_DELAYS[min(restart_idx, len(_RESTART_DELAYS) - 1)]
            print(f"[RTSP Push] RTSP error — reconnecting in {delay}s...", file=sys.stderr)
            restart_idx += 1
            import time as _time; _time.sleep(delay)
        else:
            # Clean EOS or intentional stop — do not restart
            break

    # BUG-11 fix: stop the camera manager once, after the restart loop exits.
    camera_manager.stop()


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def run_python_mode(args) -> None:
    """Entry point called by main.py for the Python backend."""
    camera_manager = CameraManager(CAMERAS_YML)

    # --- P2P config (edge_node.yml) ---
    edge_cfg = {}
    edge_node_yml = S.ROOT / "configs" / "edge_node.yml"
    try:
        if edge_node_yml.exists():
            with open(edge_node_yml, "r") as f:
                edge_cfg = yaml.safe_load(f) or {}
            print("[P2P] edge_node.yml loaded.")
    except Exception as exc:
        print(f"[P2P] Failed to load edge_node.yml: {exc}", file=sys.stderr)

    # --- P2P Peer Orchestrator (single Zenoh session for ALL P2P traffic) ---
    peer_orch = None
    try:
        from .peer_orchestrator import PeerOrchestrator
        p2p_cfg = edge_cfg.get("p2p", {})
        peer_orch = PeerOrchestrator(
            node_id=NODE_ID,
            cfg=p2p_cfg,
            camera_manager=camera_manager,
        )
        orch_thread = threading.Thread(target=peer_orch.start, daemon=True)
        orch_thread.start()
        peer_orch._ready_event.wait(timeout=5)
        print(f"[PeerOrch] Started. Node='{NODE_ID}', Overload threshold={p2p_cfg.get('overload_threshold', 75.0)}%")
    except Exception as exc:
        print(f"[PeerOrch] Failed to start: {exc}", file=sys.stderr)
        peer_orch = None

    # --- Zenoh Command & Control (shares PeerOrchestrator's Zenoh session) ---
    zenoh_sub = None
    try:
        from .zenoh_subscriber import ZenohCommandSubscriber
        shared_session = peer_orch._session if peer_orch else None
        zenoh_sub = ZenohCommandSubscriber(
            camera_manager=camera_manager,
            node_id=NODE_ID,
            session=shared_session,
        )
        zenoh_sub.start()
        print(f"[Zenoh C2] Subscriber active. Node='{NODE_ID}', Key=peers/control/{NODE_ID}")
    except ImportError:
        print(
            "[Zenoh C2] zenoh/msgpack not installed — control disabled. "
            "Run: pip install zenoh msgpack",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"[Zenoh C2] Failed to start subscriber: {exc}", file=sys.stderr)

    # --- Offload Publisher + Receiver (Level 2/3 crop offload) ---
    offload_pub = None
    offload_rcv = None
    p2p_cfg     = edge_cfg.get("p2p", {})
    if p2p_cfg.get("offload_level", 0) > 0 and peer_orch is not None:
        try:
            from .offload_publisher import OffloadPublisher
            from .offload_receiver  import OffloadReceiver

            offload_pub = OffloadPublisher(
                node_id=NODE_ID,
                session=peer_orch._session,
            )
            offload_pub.start()
            print("[OffloadPub] Started.")

            lpr_path    = S.ROOT / p2p_cfg.get("lpr_engine_path", "models/lpr.engine")
            lpd_path    = S.ROOT / p2p_cfg.get("lpd_engine_path", "models/lpd.engine")
            labels_path = S.ROOT / "configs" / "labels_lpr.txt"

            offload_rcv = OffloadReceiver(
                node_id=NODE_ID,
                session=peer_orch._session,
                lpr_engine_path=str(lpr_path),
                lpd_engine_path=str(lpd_path),
                labels_path=str(labels_path),
            )
            offload_rcv.start()
            print("[OffloadRcv] Started.")

        except Exception as exc:
            print(f"[OffloadPub/Rcv] Failed to start: {exc}", file=sys.stderr)

    # --- MonitorClient ---
    # Do NOT open a MonitorClient here.  health_agent.py is the sole owner
    # of the WebSocket connection to the Central Monitor Server for this
    # node_id.  Opening a second client from main.py causes the server to
    # close the health_agent's connection (code 1000 normal closure) every
    # time main.py starts, triggering an endless close/reconnect loop between
    # the two competing clients for the same node_id.
    #
    # The _health_push_loop below calls send_to_monitor() which is a no-op
    # when no MonitorClient has been registered — so health metrics from this
    # process are forwarded via Zenoh → health_agent → server instead.
    #
    # If you run main.py WITHOUT health_agent.py (standalone / dev mode),
    # set the env var PIPELINE_OWN_WS=1 to re-enable the client here.
    if MONITOR_URL and os.environ.get("PIPELINE_OWN_WS") == "1":
        from speedflow_python.monitor_client import MonitorClient, set_default_client
        _client = MonitorClient(MONITOR_URL, NODE_ID, ADVERTISE_IP)
        _client.start()
        set_default_client(_client)
        print(f"[MonitorClient] Started → {MONITOR_URL} (PIPELINE_OWN_WS mode)")

    # --- Health Push (periodic metrics → Dashboard + Zenoh) ---
    _health_thread = threading.Thread(
        target=_health_push_loop, args=(peer_orch,), daemon=True,
    )
    _health_thread.start()
    print("[HealthPush] Started (metrics → Dashboard + Zenoh)")

    # Run pipeline — offload_pub reference passed so SpeedProbe can use it
    if args.mode == "display":
        probe = run_display_mode(args, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub)
    elif args.mode == "file":
        probe = run_file_mode(args, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub)
    elif args.mode == "rtsp_push":
        probe = run_rtsp_push_mode(args, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub)
    else:
        raise ValueError(f"Unknown mode: '{args.mode}'")
