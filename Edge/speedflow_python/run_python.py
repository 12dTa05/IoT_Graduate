#!/usr/bin/env python3
"""
Python backend runner.
Uses speedflow_python pipeline + GStreamer pad probes for processing.
Hỗ trợ Multi-Stream Dynamic.
"""
import sys
import os
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
)

import yaml


# ---------------------------------------------------------------------------
# Shared probe setup
# ---------------------------------------------------------------------------

def _setup_probes(pipeline: Gst.Pipeline, nvdsosd: Gst.Element, camera_manager: CameraManager) -> SpeedProbe:
    """
    Attach ROI filter, plate preprocessor, and speed probe to *pipeline*.
    Returns the SpeedProbe instance.
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

    # 3. Speed + LPR probe
    probe = SpeedProbe(camera_manager)

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

def run_display_mode(args, camera_manager: CameraManager) -> None:
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

    _setup_probes(pipeline, nvdsosd, camera_manager)
    _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, tiler)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("ERROR: Unable to set pipeline to PLAYING state", file=sys.stderr)
        sys.exit(1)

    _run_loop_until_eos_or_error(pipeline, camera_manager)


def run_file_mode(args, camera_manager: CameraManager) -> None:
    Gst.init(None)
    configs = camera_manager.get_enabled_configs()

    ret_build = build_pipeline(
        camera_configs=configs,
        sink_type="file",
        mux_width=args.width,
        mux_height=args.height,
    )
    pipeline, nvdsosd, streammux, source_bins = ret_build

    _setup_probes(pipeline, nvdsosd, camera_manager)
    _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, None)

    print(f"[Python File Mode] Processing multi-streams to output files...")
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("ERROR: Unable to set pipeline to PLAYING state", file=sys.stderr)
        sys.exit(1)

    _run_loop_until_eos_or_error(pipeline, camera_manager)


def run_rtsp_push_mode(args, camera_manager: CameraManager) -> None:
    Gst.init(None)
    configs = camera_manager.get_enabled_configs()

    rtsp_url = args.rtsp_push_url or S.RTSP_PUSH_URL
    if not rtsp_url:
        print("ERROR: --rtsp-push-url or RTSP_PUSH_URL required", file=sys.stderr)
        sys.exit(1)

    ret_build = build_pipeline(
        camera_configs=configs,
        sink_type="rtsp_push",
        mux_width=args.width,
        mux_height=args.height,
        rtsp_push_url=rtsp_url,
        bitrate=S.RTSP_PUSH_BITRATE,
    )
    pipeline, nvdsosd, streammux, source_bins = ret_build
    tiler = pipeline.get_by_name("tiler")

    _setup_probes(pipeline, nvdsosd, camera_manager)
    _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, tiler)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("ERROR: Unable to set pipeline to PLAYING state", file=sys.stderr)
        sys.exit(1)

    print(f"[RTSP Push] Streaming to {rtsp_url}")
    _run_loop_until_eos_or_error(pipeline, camera_manager)


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def run_python_mode(args) -> None:
    """Entry point called by main.py for the Python backend."""
    camera_manager = CameraManager(CAMERAS_YML)

    # --- Zenoh Command & Control ---
    zenoh_sub = None
    try:
        from .zenoh_subscriber import ZenohCommandSubscriber
        zenoh_sub = ZenohCommandSubscriber(
            camera_manager=camera_manager,
            node_id=NODE_ID,
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

    # --- P2P Peer Orchestrator ---
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
        print(f"[PeerOrch] Started. Node='{NODE_ID}', Overload threshold={p2p_cfg.get('overload_threshold', 75.0)}%")
    except Exception as exc:
        print(f"[PeerOrch] Failed to start: {exc}", file=sys.stderr)
        peer_orch = None

    # --- MonitorClient: persistent WS connection to Central Monitor Server ---
    if MONITOR_URL:
        try:
            from .monitor_client import MonitorClient, set_default_client
            client = MonitorClient(MONITOR_URL, NODE_ID, ADVERTISE_IP)
            client.start()
            set_default_client(client)
            print(f"[MonitorClient] Connected to {MONITOR_URL} (node={NODE_ID})")
        except Exception as exc:
            print(f"[MonitorClient] Failed to start: {exc}", file=sys.stderr)
    else:
        print("[MonitorClient] MONITOR_URL not set — central monitoring disabled")

    if args.mode == "display":
        run_display_mode(args, camera_manager)
    elif args.mode == "file":
        run_file_mode(args, camera_manager)
    elif args.mode == "rtsp_push":
        run_rtsp_push_mode(args, camera_manager)
    else:
        raise ValueError(f"Unknown mode: '{args.mode}'")
