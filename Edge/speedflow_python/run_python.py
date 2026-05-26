#!/usr/bin/env python3
"""
Python backend runner.
Uses speedflow_python pipeline + GStreamer pad probes for processing.
Hỗ trợ Multi-Stream Dynamic.
"""
import sys
import os
import asyncio
import time
import threading

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from .core_pipeline import build_pipeline, dynamic_add_stream, dynamic_remove_stream
from .camera_config import CameraManager
from .probes import SpeedProbe, ROIFilterProbe
from .plate_preprocessor import PlatePreprocessorProbe
from .config_txt import load_kv_txt
from .common import WebRTCSession
from . import settings as S
from .settings import (
    CAMERAS_YML,
    NODE_ID,
    MQTT_BROKER_HOST,
    MQTT_BROKER_PORT,
    MQTT_USER,
    MQTT_PASS,
    SIGNALING_HOST,
    SIGNALING_PORT,
    MUX_WIDTH,
    MUX_HEIGHT,
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


async def run_webrtc_mode_async(args, camera_manager: CameraManager) -> None:
    configs = list(camera_manager.configs.values())

    # --- Start embedded signaling server trong cùng event loop ---
    try:
        from .signaling import start_embedded
        asyncio.create_task(start_embedded(SIGNALING_HOST, SIGNALING_PORT))
        print(f"[Signaling] Embedded server started on {SIGNALING_HOST}:{SIGNALING_PORT}")
    except Exception as exc:
        print(f"[Signaling] Failed to start: {exc}", file=sys.stderr)

    ret_build = build_pipeline(
        camera_configs=configs,
        sink_type="webrtc",
        mux_width=args.width,
        mux_height=args.height,
    )
    pipeline, nvdsosd, streammux, source_bins, webrtc_elem = ret_build
    tiler = pipeline.get_by_name("tiler")

    probe = _setup_probes(pipeline, nvdsosd, camera_manager)
    _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, tiler)

    ws_uri = f"ws://{args.server}:{args.port}/ws?room={args.room}&role=pub"
    session = WebRTCSession(webrtc_elem, ws_uri)
    probe.set_publisher(session.send_json_threadsafe)

    pipeline.set_state(Gst.State.PLAYING)
    print(f"[Python WebRTC Mode] Pipeline running")
    print(f"[Python WebRTC Mode] Room: {args.room}")
    print(f"[Python WebRTC Mode] View stream at: http://{args.server}:{args.port}/")

    await asyncio.sleep(1.5)
    await session.connect()

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            loop.quit()
        elif t == Gst.MessageType.EOS:
            loop.quit()

    bus.connect("message", on_message)

    try:
        running_loop = asyncio.get_running_loop()
        await running_loop.run_in_executor(None, loop.run)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        session.close()
        camera_manager.stop()
        pipeline.set_state(Gst.State.NULL)
        print("Pipeline stopped")


def run_webrtc_mode(args, camera_manager: CameraManager) -> None:
    Gst.init(None)
    asyncio.run(run_webrtc_mode_async(args, camera_manager))


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def run_python_mode(args) -> None:
    """Entry point called by main.py for the Python backend."""
    camera_manager = CameraManager(CAMERAS_YML)

    # --- MQTT Command & Control ---
    # All values loaded from Edge/.env via speedflow_python.settings
    mqtt_sub = None
    try:
        from .mqtt_subscriber import MQTTCommandSubscriber
        node_id     = NODE_ID
        broker_host = MQTT_BROKER_HOST
        broker_port = MQTT_BROKER_PORT
        mqtt_user   = MQTT_USER
        mqtt_pass   = MQTT_PASS

        mqtt_sub = MQTTCommandSubscriber(
            camera_manager=camera_manager,
            node_id=node_id,
            broker_host=broker_host,
            broker_port=broker_port,
            username=mqtt_user,
            password=mqtt_pass,
        )
        mqtt_sub.start()
        print(
            f"[MQTT C2] Subscriber active. Node='{node_id}', "
            f"Broker={broker_host}:{broker_port}, "
            f"Topic=peers/control/{node_id}"
        )
    except ImportError:
        print(
            "[MQTT C2] paho-mqtt not installed — MQTT control disabled. "
            "Run: pip install paho-mqtt",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"[MQTT C2] Failed to start subscriber: {exc}", file=sys.stderr)

    # --- P2P Peer Discovery ---
    edge_cfg = {}        # safe default — populated if edge_node.yml exists
    edge_node_yml = S.ROOT / "configs" / "edge_node.yml"
    try:
        from .peer_discovery import PeerDiscovery
        if edge_node_yml.exists():
            with open(edge_node_yml, "r") as f:
                edge_cfg = yaml.safe_load(f) or {}
            static_peers = edge_cfg.get("peers", [])
            mdns_cfg = edge_cfg.get("mdns", {})
            discovery = PeerDiscovery(
                static_peers=static_peers,
                mdns_enabled=mdns_cfg.get("enabled", False),
                service_type=mdns_cfg.get("service_type", "_iot_graduate._tcp.local."),
            )
            discovery.start()
            print(f"[PeerDiscovery] Started. Static peers: {[p.node_id for p in discovery.get_peers()]}")
        else:
            discovery = None
            print("[PeerDiscovery] edge_node.yml not found — no peer discovery.")
    except Exception as exc:
        print(f"[PeerDiscovery] Failed: {exc}", file=sys.stderr)
        discovery = None

    # --- Embedded MQTT Broker (optional — runs on exactly one node) ---
    broker_mgr = None
    broker_cfg = edge_cfg.get("broker", {})
    from .settings import BROKER_ENABLED, BROKER_PORT as _BROKER_PORT
    if BROKER_ENABLED:
        try:
            from .broker_manager import BrokerManager
            broker_mgr = BrokerManager(
                port=_BROKER_PORT,
                username=mqtt_user,
                password=mqtt_pass,
            )
            broker_mgr.start()
            print(f"[BrokerManager] Embedded Mosquitto started on port {_BROKER_PORT}.")
        except Exception as exc:
            print(f"[BrokerManager] Failed to start: {exc}", file=sys.stderr)
            broker_mgr = None

    # --- Health Agent (starts early so broker status reaches peers quickly) ---
    health_agent = None
    try:
        import sys as _sys2
        import importlib.util as _ilu
        _ha_path = S.ROOT / "health_agent.py"
        if _ha_path.exists():
            _spec = _ilu.spec_from_file_location("health_agent", _ha_path)
            _ha_mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_ha_mod)
            health_agent = _ha_mod.HealthAgent(broker_manager=broker_mgr)
            health_agent.start()
            print(f"[HealthAgent] Started. Node='{node_id}'")
    except Exception as exc:
        print(f"[HealthAgent] Failed to start: {exc}", file=sys.stderr)

    # --- P2P Peer Orchestrator ---
    peer_orch = None
    try:
        from .peer_orchestrator import PeerOrchestrator
        p2p_cfg = edge_cfg.get("p2p", {})

        # Extra reconnect callbacks — each MQTT client that needs to follow
        # a broker failover registers here.
        reconnect_callbacks = []
        if health_agent is not None and hasattr(health_agent, "reconnect"):
            reconnect_callbacks.append(health_agent.reconnect)
        if mqtt_sub is not None and hasattr(mqtt_sub, "reconnect"):
            reconnect_callbacks.append(mqtt_sub.reconnect)

        peer_orch = PeerOrchestrator(
            node_id=node_id,
            cfg=p2p_cfg,
            camera_manager=camera_manager,
            broker_host=broker_host,
            broker_port=broker_port,
            username=mqtt_user,
            password=mqtt_pass,
            broker_cfg=broker_cfg if broker_cfg else None,
            broker_manager=broker_mgr,
            extra_reconnect_callbacks=reconnect_callbacks,
        )
        # Start orchestrator in a background thread (non-blocking)
        orch_thread = threading.Thread(target=peer_orch.start, daemon=True)
        orch_thread.start()
        print(
            f"[PeerOrch] Started. Node='{node_id}', "
            f"Overload threshold={p2p_cfg.get('overload_threshold', 75.0)}%, "
            f"BrokerWatcher={'ON' if broker_cfg else 'OFF'}"
        )
    except Exception as exc:
        print(f"[PeerOrch] Failed to start: {exc}", file=sys.stderr)

    # --- Embedded Signaling Server (display/file modes — webrtc mode handles its own) ---
    sig_enabled = edge_cfg.get("signaling", {}).get("enabled", True)
    if sig_enabled and args.mode in ("display", "file"):
        sig_host = SIGNALING_HOST
        sig_port = SIGNALING_PORT
        # display/file mode: GLib main loop, cần asyncio event loop riêng
        def _run_signaling():
            import asyncio as _asyncio
            from .signaling import start_embedded
            _loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(_loop)
            _loop.run_until_complete(start_embedded(sig_host, sig_port, logger_obj=None))
            _loop.run_forever()
        threading.Thread(target=_run_signaling, daemon=True).start()
        print(f"[Signaling] Embedded server started on {sig_host}:{sig_port}")

    if args.mode == "display":
        run_display_mode(args, camera_manager)
    elif args.mode == "file":
        run_file_mode(args, camera_manager)
    elif args.mode == "webrtc":
        run_webrtc_mode(args, camera_manager)
    else:
        raise ValueError(f"Unknown mode: '{args.mode}'")
