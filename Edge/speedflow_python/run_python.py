#!/usr/bin/env python3
"""
Python backend runner.
Uses speedflow_python pipeline + GStreamer pad probes for processing.
Supports Multi-Stream Dynamic.
"""
import sys
import os
import logging
import time
import threading
from typing import Optional

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

# Holder for the live SpeedProbe.  Set by the mode runners when a probe is
# created (rtsp_push restarts create a fresh probe per iteration); read by
# _health_push_loop each tick so it always feeds the current probe.
ACTIVE_SPEED_PROBE: list = []

# ---------------------------------------------------------------------------
# Shared probe setup
# ---------------------------------------------------------------------------

def _setup_probes(pipeline: Gst.Pipeline, nvdsosd: Gst.Element,
                  camera_manager: CameraManager,
                  peer_orch=None,
                  offload_pub=None,
                  offload_rcv=None,
                  zenoh_pub=None) -> SpeedProbe:
    """
    Attach ROI filter, plate preprocessor, and speed probe to *pipeline*.
    Returns the SpeedProbe instance.

    peer_orch:   PeerOrchestrator — lets the probe query offload levels.
    offload_pub: OffloadPublisher — lets the probe send crops to peers.
    offload_rcv: OffloadReceiver — lets peer-returned crop results reach the probe.
    zenoh_pub:   ZenohPublisher — lets the probe publish overspeed events.
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
    # Wire camera -> source_type ("live" | "file") so probes.py can
    # publish source_modes in _telemetry and downstream consumers can
    # distinguish decoder-throughput file playback from live source FPS.
    # ponytail: local helper, cheap ini
    def _is_file(s: str) -> bool:
        if not s:
            return False
        s = s.strip().lower()
        return s.startswith("file://") or s.startswith("/")
    _source_types = {}
    for _c in camera_manager.get_enabled_configs():
        _source_types[_c.camera_id] = "file" if _is_file(_c.uri or "") else "live"
    probe.set_source_types(_source_types)
    if offload_pub is not None:
        probe.set_offload_publisher(offload_pub)
    if offload_rcv is not None:
        offload_rcv.set_result_handler(probe.inject_offload_result)
        probe.set_offload_receiver(offload_rcv)
    if zenoh_pub is not None:
        probe.set_publisher(zenoh_pub)

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
    tiler: Gst.Element = None,
):
    """
    Hooks up the CameraManager to safely add/remove streams dynamically.

    Thread-safety note:
        source_id_to_cam_id is ONLY written inside on_add/on_remove,
        which are always called via GLib.idle_add → run on GLib Main Loop
        thread → no concurrent mutation possible.
    """
    # Reverse mapping: source_id (int) → camera_id (str)
    # Initialized from enabled cameras when pipeline starts.
    # Only read/written from GLib Main Loop thread (via idle_add).
    source_id_to_cam_id: dict[int, str] = {
        cfg.source_id: cfg.camera_id
        for cfg in camera_manager.get_enabled_configs()
    }

    def on_add(cam_cfg):
        print(f"[Dynamic] Adding camera '{cam_cfg.camera_id}' (source_id={cam_cfg.source_id})")
        dynamic_add_stream(pipeline, streammux, cam_cfg, tiler, source_bins)
        # Register mapping immediately after successful add.
        # This function runs in GLib Main Loop → safe, no lock needed.
        source_id_to_cam_id[cam_cfg.source_id] = cam_cfg.camera_id

    def on_remove(source_id):
        # Look up camera_id from the mapping dict.
        # Not using GStreamer pad scan because it's complex and not thread-safe.
        cam_id = source_id_to_cam_id.get(source_id)
        if cam_id is None:
            print(
                f"[Dynamic] WARN: No camera mapped to source_id={source_id}. "
                "Possibly already removed or never registered.",
                file=sys.stderr
            )
            return

        print(f"[Dynamic] Removing camera '{cam_id}' (source_id={source_id})")
        dynamic_remove_stream(pipeline, streammux, cam_id, source_id, tiler, source_bins)

        # Clean up key after successful removal.
        # Prevents memory leak during continuous operation over months
        # and avoids source_id conflicts if the same ID is reused later.
        removed = source_id_to_cam_id.pop(source_id, None)
        if removed:
            print(f"[Dynamic] Cleaned up mapping: source_id={source_id} → '{removed}'")

    camera_manager.start(on_add, on_remove, GLib.idle_add)


# ---------------------------------------------------------------------------
# GLib bus helpers
# ---------------------------------------------------------------------------

def _is_transient_nvmm_buffer_error(err, debug: Optional[str]) -> bool:
    """Narrow check for transient decoder buffer exhaustion."""
    text = f"{err} {debug or ''}"
    return "OutputBufferUnavailable" in text or "cbAllocPictureBuffer" in text


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
            src_name = message.src.get_name() if message.src else "unknown"
            if _is_transient_nvmm_buffer_error(err, debug):
                print(f"WARNING (recoverable): Transient decoder buffer exhaustion from {src_name}: {err}", file=sys.stderr)
                if debug:
                    print(f"DEBUG INFO: {debug}", file=sys.stderr)
                return

            print(f"ERROR from {src_name}: {err}", file=sys.stderr)
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

def run_display_mode(args, camera_manager: CameraManager, peer_orch=None, offload_pub=None, offload_rcv=None, zenoh_pub=None) -> SpeedProbe:
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

    probe = _setup_probes(pipeline, nvdsosd, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
    ACTIVE_SPEED_PROBE.clear()
    ACTIVE_SPEED_PROBE.append(probe)
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
    return probe


def run_file_mode(args, camera_manager: CameraManager, peer_orch=None, offload_pub=None, offload_rcv=None, zenoh_pub=None) -> SpeedProbe:
    Gst.init(None)
    configs = camera_manager.get_enabled_configs()

    ret_build = build_pipeline(
        camera_configs=configs,
        sink_type="file",
        mux_width=args.width,
        mux_height=args.height,
    )
    pipeline, nvdsosd, streammux, source_bins = ret_build

    probe = _setup_probes(pipeline, nvdsosd, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
    ACTIVE_SPEED_PROBE.clear()
    ACTIVE_SPEED_PROBE.append(probe)
    _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, None)

    print(f"[Python File Mode] Processing multi-streams to output files...")
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("ERROR: Unable to set pipeline to PLAYING state", file=sys.stderr)
        sys.exit(1)

    _run_loop_until_eos_or_error(pipeline, camera_manager)
    return probe


# ---------------------------------------------------------------------------
# Health push — periodic metrics to local PeerOrchestrator self-state, and a
# heartbeat on peers/status/<NODE_ID> via PeerOrchestrator's shared Zenoh
# session at the same 1 s cadence.  In the default run_edge.sh deployment a
# standalone health_agent.py process also publishes on the same key from its
# own session; in explicit PIPELINE_OWN_WS=1 standalone/dev mode this loop
# additionally owns WebSocket forwarding to the Monitor Server so main.py can
# run without health_agent.py.
# ---------------------------------------------------------------------------

def _health_push_loop(peer_orch=None) -> None:
    import json as _json
    import time as _time
    from pathlib import Path as _Path
    import yaml as _yaml

    # Delegate metric collection and load-score computation to health_agent's
    # functions so adaptive omega weights and the correct CPU formula are used.
    from health_agent import (
        _compute_load_score           as _compute_load_fn,
        _compute_load_score_breakdown as _compute_lb_fn,
        _detect_source_starved,
        _derive_camera_workload,
        _read_pipeline_snapshot       as _read_pipeline,
        _maybe_reload_edge_cfg        as _reload_edge_cfg,
        get_edge_cfg                  as _get_edge_cfg,
        open_jtop_session,
        collect_metrics,
    )
    _reload_edge_cfg()
    from speedflow_python.load_model import ProactiveModel as _ProactiveModel
    from speedflow_python.settings import LOAD_MODEL as _LOAD_MODEL, LOAD_POLICY as _LOAD_POLICY
    _proactive_model = _ProactiveModel(
        _get_edge_cfg().get("proactive", {}),
        policy=_LOAD_POLICY,
        model_type=_LOAD_MODEL,
    )

    _fps_file = _Path(FPS_STATS_FILE)

    # Load camera configs once so peers know the original URIs for failover.
    _cam_configs: dict = {}
    # ponytail: uses dict, sufficient for periodic camera config reload

    def _reload_cam_configs() -> dict:
        try:
            with open(CAMERAS_YML, "r", encoding="utf-8") as f:
                raw = _yaml.safe_load(f)
            result = {}
            for cam_id, cfg in raw.get("cameras", {}).items():
                if cfg and cfg.get("enabled", True):
                    result[cam_id] = {
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
            return result
        except Exception:
            return {}

    _cam_configs = _reload_cam_configs()
    _cam_configs_reload_interval = 30.0   # re-read cameras.yml every 30 s
    _last_cam_reload = _time.monotonic()

    # Open a persistent jtop session using standalone health_agent functions.
    # This avoids the HealthAgent.__new__ stub hack and keeps the pipeline
    # health loop independent.  This loop no longer publishes to Zenoh or
    # WebSocket; health_agent.py is the single external telemetry owner.
    _jtop_session = open_jtop_session()

    # ponytail: monotonic deadline sleep so work duration doesn't extend period.
    _next_deadline = _time.monotonic()

    # Flat unavailable breakdown matching health_agent._compute_load_score_breakdown
    # when fps_stats is invalid (non-dict or no active cams).
    _UNAVAILABLE_BREAKDOWN = {
        "fps_score": 100.0,
        "workload_bonus": 0.0,
        "thermal_bonus": 0.0,
        "recv_bonus": 0.0,
        "trend_bonus": 0.0,
        "composite_score": 100.0,
        "load_score": 100.0,
    }

    while True:
        try:
            # Periodically refresh camera configs to pick up dynamic changes
            if _time.monotonic() - _last_cam_reload >= _cam_configs_reload_interval:
                _cam_configs = _reload_cam_configs()
                _last_cam_reload = _time.monotonic()
                # Reload edge_node.yml so ProactiveModel sees fresh coefficients
                _reload_edge_cfg()
                _proactive_model.reload_cfg(_get_edge_cfg().get("proactive", {}))

            # Use health_agent's metric collection via standalone function
            metrics       = collect_metrics(_jtop_session)
            snapshot_valid, fps_stats, feature_stats, offload_crops, _input_fps, _source_modes = _read_pipeline()

            # ── Pipeline unavailable guard ──────────────────────────────
            # Match health_agent's contract: invalid snapshot →
            # load_score 100 (unavailable), skip proactive computation.
            if snapshot_valid:
                source_starved_cameras = _detect_source_starved(
                    fps_stats, _input_fps, _get_edge_cfg(),
                    source_type_map=_source_modes,
                )
                camera_workload = _derive_camera_workload(
                    feature_stats, fps_stats, source_starved_cameras
                )
                load_score, omega_preset = _compute_load_fn(
                    metrics, fps_stats, source_starved_cameras,
                    feature_stats=feature_stats,
                )
                # Compute breakdown with same inputs for auditability
                lb = _compute_lb_fn(
                    metrics, fps_stats, source_starved_cameras,
                    feature_stats=feature_stats,
                )
                offload_crops_received_per_s = float(offload_crops.get("received_per_s", 0.0))
                active_fps_vals = [v for v in fps_stats.values() if v > 0.0]
                avg_fps = round(sum(active_fps_vals) / len(active_fps_vals), 1) if active_fps_vals else None
                active_cameras = [k for k, v in fps_stats.items() if v > 0.0]
            else:
                load_score, omega_preset = 100.0, "fps_dominant"
                lb = _UNAVAILABLE_BREAKDOWN
                offload_crops_received_per_s = 0.0
                active_fps_vals = []
                avg_fps = None
                active_cameras = []
                fps_stats = {}
                feature_stats = {}
                offload_crops = {"received_per_s": 0.0}
                source_starved_cameras = set()
                camera_workload = {}

            payload = {
                "type":         "health",
                "node_id":      NODE_ID,
                "timestamp":    _time.time(),
                "load_score":   load_score,
                "load_score_breakdown": lb,
                "omega_preset": omega_preset,
                "gpu_percent":  metrics["gpu_percent"],
                "cpu_percent":  metrics["cpu_percent"],
                "ram_percent":  metrics["ram_percent"],
                "gpu_temp_c":   metrics["gpu_temp_c"],
                "power_mw":     metrics.get("power_mw", 0.0),
                "source":       metrics.get("source", "jtop"),
                "pipeline": {
                    # pipeline_available: False = snapshot not yet valid (pipeline
                    # starting/stale); load_score=100 is "unavailable", not overload.
                    "pipeline_available":    snapshot_valid,
                    # output_fps_per_camera = pipeline throughput (frames processed).
                    # fps_per_camera kept for backward compatibility.
                    "fps_per_camera":        fps_stats,
                    "output_fps_per_camera": fps_stats,
                    # input_fps_per_camera = PTS-derived native source rate
                    # (SpeedProbe.buf_pts deltas), falling back to bounded
                    # OSD output rate when PTS is unavailable.
                    "input_fps_per_camera":  _input_fps if snapshot_valid else {},
                    "avg_fps":         avg_fps,
                    "active_cameras":  active_cameras,
                    "camera_configs":  _cam_configs,
                    "camera_workload": camera_workload,
                    "camera_features": feature_stats if snapshot_valid else {},
                    "source_starved_cameras": sorted(source_starved_cameras),
                },
            }

            # Push the breakdown to the live SpeedProbe so the FPS writer
            # includes it in the next telemetry snapshot.  No blocking, no file I/O.
            if ACTIVE_SPEED_PROBE:
                ACTIVE_SPEED_PROBE[0].set_load_score_breakdown(lb)

            # ── Proactive model ──────────────────────────────────────
            # Skip when snapshot is invalid — no features to compute on.
            if snapshot_valid:
                _active_ids = {k for k, v in fps_stats.items() if v > 0.0}
                proactive_result = _proactive_model.compute(
                    metrics,
                    {k: v for k, v in feature_stats.items() if k in _active_ids},
                    offload_crops_received_per_s=offload_crops_received_per_s,
                    fps_stats={k: v for k, v in fps_stats.items() if k in _active_ids},
                )
                payload.update(proactive_result)

            # Keep local PeerOrchestrator state fresh and publish a fresh
            # heartbeat on peers/status/<NODE_ID> via the shared session.
            # health_agent.py may also publish from its own session in the
            # default run_edge.sh deployment; that second source publishes
            # under the SAME node_id so the Zenoh pub/sub contract is
            # satisfied regardless of which process is running.
            # send_to_monitor is deliberately inside the PIPELINE_OWN_WS=1
            # block below (MonitorClient ownership = standalone / dev mode).
            if peer_orch is not None:
                peer_orch.update_self_state(payload)
                import msgpack as _msgpack
                peer_orch.publish_status(
                    _msgpack.packb(payload, use_bin_type=True)
                )

            if os.environ.get("PIPELINE_OWN_WS") == "1":
                from speedflow_python.monitor_client import send_to_monitor
                send_to_monitor(payload)

        except Exception as _exc:
            import logging as _logging
            _logging.getLogger("health_push").warning(
                "[HealthPush] Exception in health loop (will retry): %s", _exc, exc_info=True
            )
        # ponytail: deadline sleep — work duration does not extend the period.
        _next_deadline += float(HEALTH_INTERVAL)
        _remaining = _next_deadline - _time.monotonic()
        if _remaining > 0:
            _time.sleep(_remaining)
        else:
            _next_deadline = _time.monotonic()



def run_rtsp_push_mode(args, camera_manager: CameraManager, peer_orch=None, offload_pub=None, offload_rcv=None, zenoh_pub=None) -> Optional["SpeedProbe"]:
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
    _last_probe = None

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

        _last_probe = _setup_probes(pipeline, nvdsosd, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
        ACTIVE_SPEED_PROBE.clear()
        ACTIVE_SPEED_PROBE.append(_last_probe)
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
                src_name = message.src.get_name() if message.src else "unknown"

                if _is_transient_nvmm_buffer_error(err, debug):
                    print(f"WARNING (recoverable): Transient decoder buffer exhaustion from {src_name}: {err}", file=sys.stderr)
                    if debug:
                        print(f"DEBUG INFO: {debug}", file=sys.stderr)
                    return

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
                    print(f"ERROR (source) from {src_name} ({cam_id}): {err}", file=sys.stderr)
                    if debug:
                        print(f"DEBUG INFO: {debug}", file=sys.stderr)
                    print(f"[Pipeline] Removing failed source '{cam_id}'", file=sys.stderr)
                    sid = None
                    for cfg in camera_manager.get_enabled_configs():
                        if cfg.camera_id == cam_id:
                            sid = cfg.source_id
                            break
                    if sid is not None:
                        dynamic_remove_stream(pipeline, streammux, cam_id, sid, tiler, source_bins)
                    return  # don't quit the pipeline

                # Distinguish sink vs other pipeline errors
                is_rtsp_sink = (
                    elem_name := (src_name or "")
                ) and ("rtsp_push_sink" in elem_name or "sink" in elem_name or "rtsp" in elem_name)
                err_category = "sink" if is_rtsp_sink else "pipeline"
                print(f"ERROR ({err_category}) from {src_name}: {err}", file=sys.stderr)
                if debug:
                    print(f"DEBUG INFO: {debug}", file=sys.stderr)

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
            return _last_probe
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
    return _last_probe


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

    # --- Zenoh event publisher (overspeed violations → traffic/events/...) ---
    # SpeedProbe enqueues overspeed payloads here; a daemon thread publishes
    # them via Zenoh. health_agent.py subscribes to traffic/events/{NODE_ID}/**
    # and forwards them to the Central Monitor Server. Without this, overspeed
    # events are computed but never leave the pipeline.
    zenoh_pub = None
    try:
        from .zenoh_publisher import ZenohPublisher
        zenoh_pub = ZenohPublisher(node_id=NODE_ID)
        zenoh_pub.start()
        print(f"[ZenohPub] Started. Key=traffic/events/{NODE_ID}/<camera_id>")
    except ImportError:
        print(
            "[ZenohPub] zenoh/msgpack not installed — overspeed events disabled. "
            "Run: pip install zenoh msgpack",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"[ZenohPub] Failed to start: {exc}", file=sys.stderr)

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
    # Do NOT open a MonitorClient here by default.  health_agent.py is the
    # sole owner of the WebSocket connection and Zenoh health heartbeat for
    # this node_id.  Opening a second client from main.py causes the server to
    # close the health_agent's connection (code 1000 normal closure) every
    # time main.py starts, triggering an endless close/reconnect loop between
    # the two competing clients for the same node_id.
    #
    # The _health_push_loop below updates PeerOrchestrator self-state only;
    # health metrics are published externally by health_agent.py.
    #
    # If you run main.py WITHOUT health_agent.py (standalone / dev mode),
    # set the env var PIPELINE_OWN_WS=1 to re-enable the client here.
    if MONITOR_URL and os.environ.get("PIPELINE_OWN_WS") == "1":
        from speedflow_python.monitor_client import MonitorClient, set_default_client
        _client = MonitorClient(MONITOR_URL, NODE_ID, ADVERTISE_IP)
        _client.start()
        set_default_client(_client)
        print(f"[MonitorClient] Started → {MONITOR_URL} (PIPELINE_OWN_WS mode)")

    # --- Health Push (periodic metrics → local PeerOrchestrator state) ---
    _health_thread = threading.Thread(
        target=_health_push_loop, args=(peer_orch,), daemon=True,
    )
    _health_thread.start()
    if os.environ.get("PIPELINE_OWN_WS") == "1":
        print("[HealthPush] Started (PIPELINE_OWN_WS: Zenoh heartbeat + WebSocket)")
    else:
        print("[HealthPush] Started (Zenoh heartbeat; WebSocket via health_agent)")

    # Run pipeline — offload_pub + zenoh_pub references passed so SpeedProbe can use them
    if args.mode == "display":
        probe = run_display_mode(args, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
    elif args.mode == "file":
        probe = run_file_mode(args, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
    elif args.mode == "rtsp_push":
        probe = run_rtsp_push_mode(args, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
    else:
        raise ValueError(f"Unknown mode: '{args.mode}'")

    # Fix #1 / #7: stop the FPS writer thread cleanly now that the pipeline
    # has exited, regardless of which mode was used.
    if probe is not None:
        probe.stop_fps_writer()

    # Clear the active probe reference so health loop doesn't push to stale probe
    ACTIVE_SPEED_PROBE.clear()

    # Stop the overspeed event publisher and flush its queue on exit.
    if zenoh_pub is not None:
        try:
            zenoh_pub.stop()
        except Exception as exc:
            print(f"[ZenohPub] Stop error: {exc}", file=sys.stderr)
