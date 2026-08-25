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
    FPS_STATS_FILE,
    TARGET_FPS,
    HEALTH_INTERVAL,
)

import yaml

logger = logging.getLogger(__name__)

# Holder for the live SpeedProbe.  Set by the mode runners when a probe is
# created (rtsp_push restarts create a fresh probe per iteration).
ACTIVE_SPEED_PROBE: list = []


def _stop_active_speed_probes() -> None:
    """Stop/join FPS writer on all probes in ACTIVE_SPEED_PROBE and clear the list."""
    for p in ACTIVE_SPEED_PROBE:
        try:
            p.stop_fps_writer()
        except Exception:
            pass
    ACTIVE_SPEED_PROBE.clear()

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
    # Keep PGIE interval fixed at 3. Adaptive switching is intentionally disabled.
    pgie_elem = pipeline.get_by_name("primary-infer")
    if pgie_elem is not None:
        pgie_elem.set_property("interval", 3)
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
        raise RuntimeError(f"Unable to get {pad_name} pad")
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
        ready_ev = camera_manager.stream_ready_event(cam_cfg.source_id)
        try:
            dynamic_add_stream(
                pipeline, streammux, cam_cfg, tiler, source_bins,
                ready_event=ready_ev,
            )
            # Register mapping immediately after successful add.
            # This function runs in GLib Main Loop → safe, no lock needed.
            source_id_to_cam_id[cam_cfg.source_id] = cam_cfg.camera_id
        except Exception as exc:
            print(f"[Dynamic] ERROR adding camera '{cam_cfg.camera_id}': {exc}", file=sys.stderr)
            with camera_manager._lock:
                if cam_cfg.camera_id in camera_manager._configs:
                    camera_manager._configs[cam_cfg.camera_id].enabled = False
                    camera_manager._rebuild_lookup()
            camera_manager.cleanup_stream_ready(cam_cfg.source_id)
            raise

    def on_remove(source_id, done_event=None):
        # Look up camera_id from the mapping dict.
        # Not using GStreamer pad scan because it's complex and not thread-safe.
        cam_id = source_id_to_cam_id.get(source_id)
        if cam_id is None:
            print(
                f"[Dynamic] WARN: No camera mapped to source_id={source_id}. "
                "Possibly already removed or never registered.",
                file=sys.stderr
            )
            if done_event is not None:
                done_event.set()
            return

        print(f"[Dynamic] Removing camera '{cam_id}' (source_id={source_id})")
        dynamic_remove_stream(
            pipeline, streammux, cam_id, source_id, tiler, source_bins, done_event=done_event
        )

        # Clean up key after initiating/scheduling removal.
        # Prevents memory leak during continuous operation over months
        # and avoids source_id conflicts if the same ID is reused later.
        removed = source_id_to_cam_id.pop(source_id, None)
        if removed:
            print(f"[Dynamic] Cleaned up mapping: source_id={source_id} → '{removed}'")
        camera_manager.cleanup_stream_ready(source_id)

    camera_manager.start(on_add, on_remove, GLib.idle_add)


# ---------------------------------------------------------------------------
# GLib bus helpers
# ---------------------------------------------------------------------------

# Track NVMM buffer errors to detect persistent decoder starvation
_NVMM_ERROR_TIMESTAMPS: dict = {}  # src_name -> list of timestamps
_NVMM_ERROR_RATE_LIMIT = 10        # errors in 30s triggers critical warning


def _is_transient_nvmm_buffer_error(err, debug: Optional[str], src_name: str = "unknown") -> bool:
    """Narrow check for transient decoder buffer exhaustion.

    Returns True if the error is considered transient (below rate limit).
    Returns False if it is not an NVMM buffer error or if the rate limit is exceeded
    (so callers can trigger recovery/restart/source removal).
    """
    text = f"{err} {debug or ''}"
    if "OutputBufferUnavailable" not in text and "cbAllocPictureBuffer" not in text:
        return False

    now = time.monotonic()
    hist = _NVMM_ERROR_TIMESTAMPS.setdefault(src_name, [])
    hist.append(now)
    _NVMM_ERROR_TIMESTAMPS[src_name] = [t for t in hist if now - t < 30.0]
    if len(_NVMM_ERROR_TIMESTAMPS[src_name]) >= _NVMM_ERROR_RATE_LIMIT:
        logger.critical(
            "[Pipeline] Frequent NVDEC buffer starvation on %s (%d errors in 30s) — stream degraded",
            src_name, len(_NVMM_ERROR_TIMESTAMPS[src_name]),
        )
        _NVMM_ERROR_TIMESTAMPS[src_name].clear()
        return False
    return True


def _graceful_stop_pipeline(pipeline: Gst.Pipeline) -> None:
    """
    Tear down a GStreamer pipeline safely on Jetson (Tegra).
    Transitions sequentially PLAYING -> PAUSED -> READY -> NULL
    with get_state() checks to allow hardware NVDEC/NVENC blocks
    to release registers and kernel dmabuf pools gracefully.
    """
    if pipeline is None:
        return
    try:
        # 1. PAUSED — stops data flow, drains in-flight buffers
        pipeline.set_state(Gst.State.PAUSED)
        pipeline.get_state(1 * Gst.SECOND)
        # 2. READY — tears down element-specific hardware contexts (NVDEC/NVENC)
        pipeline.set_state(Gst.State.READY)
        pipeline.get_state(1 * Gst.SECOND)
        # 3. NULL — frees GStreamer structures and closes file descriptors
        pipeline.set_state(Gst.State.NULL)
        pipeline.get_state(1 * Gst.SECOND)
    except Exception:
        try:
            pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass


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
            if _is_transient_nvmm_buffer_error(err, debug, src_name):
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
        _graceful_stop_pipeline(pipeline)
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

    # Stop any previous active probe's FPS writer before creating a new one
    _stop_active_speed_probes()

    probe = _setup_probes(pipeline, nvdsosd, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
    ACTIVE_SPEED_PROBE.append(probe)
    _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, tiler)

    t0_playing = time.monotonic()
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        _stop_active_speed_probes()
        _graceful_stop_pipeline(pipeline)
        raise RuntimeError("Unable to set display pipeline to PLAYING state")
    warmup_ms = (time.monotonic() - t0_playing) * 1000.0
    probe.record_warmup_ms(warmup_ms)
    logger.info("[Display] Pipeline PLAYING after %.0f ms (warmup)", warmup_ms)

    try:
        _run_loop_until_eos_or_error(pipeline, camera_manager)
    finally:
        _stop_active_speed_probes()
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

    # Stop any previous active probe's FPS writer before creating a new one
    _stop_active_speed_probes()

    probe = _setup_probes(pipeline, nvdsosd, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
    ACTIVE_SPEED_PROBE.append(probe)
    _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, None)

    print(f"[Python File Mode] Processing multi-streams to output files...")
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        _stop_active_speed_probes()
        _graceful_stop_pipeline(pipeline)
        raise RuntimeError("Unable to set file pipeline to PLAYING state")

    try:
        _run_loop_until_eos_or_error(pipeline, camera_manager)
    finally:
        _stop_active_speed_probes()
    return probe


def run_rtsp_push_mode(args, camera_manager: CameraManager, peer_orch=None, offload_pub=None, offload_rcv=None, zenoh_pub=None) -> Optional["SpeedProbe"]:
    Gst.init(None)
    configs = camera_manager.get_enabled_configs()

    rtsp_url = args.rtsp_push_url or S.RTSP_PUSH_URL
    if not rtsp_url:
        raise ValueError("--rtsp-push-url or RTSP_PUSH_URL required")

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
    _last_restart_cause = "initial_start"

    while True:
        # Stop any previous active probe's FPS writer before creating a new one / rebuilding RTSP
        _stop_active_speed_probes()

        print(f"[RTSP Push] Building pipeline (cause: {_last_restart_cause})...")
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
        ACTIVE_SPEED_PROBE.append(_last_probe)
        _attach_camera_manager(camera_manager, pipeline, streammux, source_bins, tiler)

        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("ERROR: Unable to set pipeline to PLAYING state", file=sys.stderr)
            _stop_active_speed_probes()
            _graceful_stop_pipeline(pipeline)
            delay = _RESTART_DELAYS[min(restart_idx, len(_RESTART_DELAYS) - 1)]
            print(f"[RTSP Push] Retrying in {delay}s...")
            restart_idx += 1
            import time as _time; _time.sleep(delay)
            continue
        elif ret == Gst.StateChangeReturn.ASYNC:
            state_ret, current_state, pending_state = pipeline.get_state(5 * Gst.SECOND)
            print(f"[RTSP Push] State change ASYNC resolved: return={state_ret.value_nick}, current={current_state.value_nick}, pending={pending_state.value_nick}")
            if state_ret == Gst.StateChangeReturn.FAILURE:
                print("ERROR: Unable to complete pipeline transition to PLAYING state (ASYNC timeout/failure)", file=sys.stderr)
                _stop_active_speed_probes()
                _graceful_stop_pipeline(pipeline)
                delay = _RESTART_DELAYS[min(restart_idx, len(_RESTART_DELAYS) - 1)]
                print(f"[RTSP Push] Retrying in {delay}s...")
                restart_idx += 1
                import time as _time; _time.sleep(delay)
                continue
        elif ret == Gst.StateChangeReturn.NO_PREROLL:
            print("[RTSP Push] State change NO_PREROLL: live pipeline running (preroll not required)")
        elif ret == Gst.StateChangeReturn.SUCCESS:
            print("[RTSP Push] State change SUCCESS: pipeline is PLAYING")

        restart_idx = 0  # reset backoff on successful start
        print(f"[RTSP Push] Streaming to {rtsp_url}")

        loop = GLib.MainLoop()
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        _error_flag = [False]
        _error_reason = ["unknown"]
        _removing = set()  # guard against double-remove from multiple error msgs
        _sink_reconnect_attempts = [0]
        _MAX_SINK_RETRIES = int(os.environ.get("RTSP_PUSH_MAX_RETRIES", "3"))
        _SINK_RETRY_DELAY_S = float(os.environ.get("RTSP_PUSH_RETRY_DELAY_S", "1.0"))

        def on_message(bus, message):
            t = message.type
            if t == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                src_name = message.src.get_name() if message.src else "unknown"

                if _is_transient_nvmm_buffer_error(err, debug, src_name):
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

                # Distinguish RTSP push sink (rtsp_push_sink / rtspclientsink) vs other pipeline errors
                is_rtsp_sink = False
                elem = message.src
                while elem is not None:
                    ename = elem.get_name()
                    fact = elem.get_factory()
                    fact_name = fact.get_name() if fact else ""
                    if ename == "rtsp_push_sink" or fact_name == "rtspclientsink":
                        is_rtsp_sink = True
                        break
                    elem = elem.get_parent()

                if is_rtsp_sink:
                    err_category = "publisher_failure"
                    print(f"ERROR ({err_category}) from {src_name}: {err}", file=sys.stderr)
                    if debug:
                        print(f"DEBUG INFO: {debug}", file=sys.stderr)

                    # Attempt bounded in-place reconnect on the rtspclientsink element
                    sink_elem = pipeline.get_by_name("rtsp_push_sink")
                    if sink_elem is None and message.src is not None:
                        curr = message.src
                        while curr is not None:
                            cfact = curr.get_factory()
                            if curr.get_name() == "rtsp_push_sink" or (cfact and cfact.get_name() == "rtspclientsink"):
                                sink_elem = curr
                                break
                            curr = curr.get_parent()

                    if sink_elem is not None and _sink_reconnect_attempts[0] < _MAX_SINK_RETRIES:
                        _sink_reconnect_attempts[0] += 1
                        print(
                            f"[RTSP Push] In-place reconnect attempt {_sink_reconnect_attempts[0]}/{_MAX_SINK_RETRIES} for {sink_elem.get_name()}...",
                            file=sys.stderr,
                        )
                        try:
                            sink_elem.set_state(Gst.State.NULL)
                            sink_elem.get_state(1 * Gst.SECOND)
                            if _SINK_RETRY_DELAY_S > 0:
                                time.sleep(_SINK_RETRY_DELAY_S)
                            sink_elem.set_state(Gst.State.READY)
                            sink_elem.get_state(1 * Gst.SECOND)
                            sret = sink_elem.set_state(Gst.State.PLAYING)
                            if sret != Gst.StateChangeReturn.FAILURE:
                                print(f"[RTSP Push] In-place reconnect initiated successfully (state_return={sret.value_nick})")
                                return
                        except Exception as exc:
                            print(f"[RTSP Push] In-place reconnect exception: {exc}", file=sys.stderr)

                    _error_reason[0] = f"{err_category}:{src_name}:{err}"
                    _error_flag[0] = True
                    loop.quit()
                    return

                # Non-RTSP errors (source/decoder/pipeline) trigger full pipeline restart
                err_category = "pipeline"
                _error_reason[0] = f"{err_category}:{src_name}:{err}"
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
            _stop_active_speed_probes()
            _graceful_stop_pipeline(pipeline)
            print("Pipeline stopped")
            return _last_probe
        finally:
            # BUG-11 fix: do NOT call camera_manager.stop() here on every
            # iteration — that kills the watchdog observer and processor thread
            # permanently.  On a restart those threads must stay alive so that
            # hot-reload and dynamic ADD/REMOVE keep working.
            # Stop the probe FPS writer and clear active probe for this iteration
            _stop_active_speed_probes()
            # We gracefully stop the pipeline state; _attach_camera_manager() at the
            # top of the next iteration re-registers on_add/on_remove callbacks
            # and calls camera_manager.start() again (which is idempotent for
            # the watchdog if it is still running).
            _graceful_stop_pipeline(pipeline)
            print("Pipeline stopped")

        if _error_flag[0]:
            delay = _RESTART_DELAYS[min(restart_idx, len(_RESTART_DELAYS) - 1)]
            _last_restart_cause = _error_reason[0]
            print(f"[RTSP Push] {_last_restart_cause} — reconnecting in {delay}s...", file=sys.stderr)
            restart_idx += 1
            import time as _time; _time.sleep(delay)
        else:
            # Clean EOS or intentional stop — do not restart
            _last_restart_cause = "clean_stop"
            break

    # BUG-11 fix: stop the camera manager once, after the restart loop exits.
    camera_manager.stop()
    return _last_probe


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def run_python_mode(args) -> None:
    """Entry point called by main.py for the Python backend."""
    # Dual logging handler: terminal INFO+, file /tmp/edge_debug.log DEBUG+
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    # Clear existing root handlers to avoid duplicate logs
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s — %(message)s", datefmt="%H:%M:%S")

    term_handler = logging.StreamHandler(sys.stderr)
    term_handler.setLevel(logging.INFO)
    term_handler.setFormatter(formatter)
    root_logger.addHandler(term_handler)

    try:
        file_handler = logging.FileHandler("/tmp/edge_debug.log", mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except Exception as exc:
        print(f"[Logging] Failed to attach /tmp/edge_debug.log handler: {exc}", file=sys.stderr)

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
    p2p_cfg = edge_cfg.get("p2p", {})
    peer_orch = None
    try:
        from .peer_orchestrator import PeerOrchestrator
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

    # --- Health Agent (shares PeerOrchestrator's Zenoh session) ---
    health_agent = None
    try:
        from health_agent import HealthAgent
        health_agent = HealthAgent(external_session=peer_orch._session if peer_orch else None)
        ha_thread = threading.Thread(target=health_agent.run, daemon=True, name="HealthAgent")
        ha_thread.start()
        health_agent._ready_event.wait(timeout=5)
        print(f"[HealthAgent] Started. Node='{NODE_ID}', Interval={S.HEALTH_INTERVAL}s")
    except Exception as exc:
        print(f"[HealthAgent] Failed to start: {exc}", file=sys.stderr)
        health_agent = None

    # --- Zenoh Command & Control (shares PeerOrchestrator's Zenoh session) ---
    zenoh_sub = None
    try:
        from .zenoh_subscriber import ZenohCommandSubscriber
        shared_session = peer_orch._session if peer_orch else None
        # Pass configured add_ack_timeout_s from edge_node.yml (falls back to migration_timeout_s - 3.0s margin, or 12.0s default)
        migration_timeout = float(p2p_cfg.get("migration_timeout_s", 15.0))
        if "add_ack_timeout_s" in p2p_cfg and p2p_cfg["add_ack_timeout_s"] is not None:
            ack_timeout = float(p2p_cfg["add_ack_timeout_s"])
        else:
            ack_timeout = max(1.0, migration_timeout - 3.0)
        if ack_timeout >= migration_timeout:
            raise ValueError(
                f"add_ack_timeout_s ({ack_timeout:.1f}s) must be strictly less than "
                f"migration_timeout_s ({migration_timeout:.1f}s) to ensure sender timeout safety"
            )
        zenoh_sub = ZenohCommandSubscriber(
            camera_manager=camera_manager,
            node_id=NODE_ID,
            session=shared_session,
            ack_timeout_s=ack_timeout,
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
        shared_session = peer_orch._session if peer_orch else None
        zenoh_pub = ZenohPublisher(node_id=NODE_ID, session=shared_session)
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

    # Note: Production health_agent.py is the sole metrics/load-score publisher
    # and publishes self-heartbeats to peers/status/<NODE_ID> over Zenoh.

    # Run pipeline — offload_pub + zenoh_pub references passed so SpeedProbe can use them
    # Supervisor loop around pipeline execution to handle recovery / stream parking
    # and keep Edge process alive without exiting when all streams are removed.
    probe = None
    while True:
        try:
            enabled_cams = camera_manager.get_enabled_configs()
            if not enabled_cams:
                recovery_wait = float(edge_cfg.get("p2p", {}).get("recovery_wait_s", 300.0))
                print(f"[Supervisor] Zero active cameras configured/enabled. Entering recovery state for {recovery_wait:.0f}s...")
                time.sleep(recovery_wait)
                # Re-check cameras config file
                camera_manager.reload()
                enabled_cams = camera_manager.get_enabled_configs()
                if not enabled_cams:
                    print("[Supervisor] Still 0 active cameras after recovery wait. Retrying...")
                    continue
                print(f"[Supervisor] Found {len(enabled_cams)} enabled cameras after recovery. Resuming pipeline.")

            if args.mode == "display":
                probe = run_display_mode(args, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
            elif args.mode == "file":
                probe = run_file_mode(args, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
            elif args.mode == "rtsp_push":
                probe = run_rtsp_push_mode(args, camera_manager, peer_orch=peer_orch, offload_pub=offload_pub, offload_rcv=offload_rcv, zenoh_pub=zenoh_pub)
            else:
                raise ValueError(f"Unknown mode: '{args.mode}'")
            # A GStreamer EOS/error returns here after the mode runner has
            # stopped the old pipeline.  Keep the Edge supervisor alive and
            # deliberately park before rebuilding; an empty-FPS interval is
            # WAITING/RECOVERY, never a reason to terminate the node.
            recovery_wait = float(edge_cfg.get("p2p", {}).get("recovery_wait_s", 300.0))
            print(f"[Supervisor] Pipeline stopped. Entering recovery state for {recovery_wait:.0f}s...")
            time.sleep(recovery_wait)
            camera_manager.reload()
            print("[Supervisor] Recovery wait complete. Rebuilding pipeline.")
            continue
        except KeyboardInterrupt:
            print("\n[Supervisor] Interrupted by user. Exiting.")
            break
        except Exception as exc:
            print(f"[Supervisor] Pipeline exception: {exc}. Retrying in 5s...", file=sys.stderr)
            time.sleep(5)
            continue
        break

    # Fix #1 / #7: stop the FPS writer thread cleanly now that the pipeline
    # has exited, regardless of which mode was used.
    if probe is not None:
        try:
            probe.stop_fps_writer()
        except Exception:
            pass

    # Clear the active probe reference so health loop doesn't push to stale probe
    ACTIVE_SPEED_PROBE.clear()

    # Stop the overspeed event publisher and flush its queue on exit.
    if zenoh_pub is not None:
        try:
            zenoh_pub.stop()
        except Exception as exc:
            print(f"[ZenohPub] Stop error: {exc}", file=sys.stderr)
