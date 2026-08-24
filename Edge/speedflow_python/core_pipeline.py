# speedflow/core_pipeline.py  (Multi-Stream Edition)
"""
Builds a DeepStream pipeline with multi-stream support.

Architecture:
  N × uridecodebin ──→ nvstreammux ──→ PGIE ──→ Tracker ──→ SGIE1 ──→ SGIE2
                                                                        │
                                                                  nvdsanalytics
                                                                        │
                               ┌──────────────────────────────────────────┘
                               │
                    sink_type == "display":     nvmultistreamtiler → OSD → EGL sink
                    sink_type == "file":        OSD → nvstreamdemux → N × encoder → filesink
                    sink_type == "rtsp_push":   nvmultistreamtiler → OSD → H264 enc → rtspclientsink
"""
import logging
import os
import threading
from typing import Optional

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from .common import make_element, gst_link
from .settings import (
    INFER_CONFIG, TRACKER_CFG, ANALYTICS_CFG,
    SGIE_CONFIG, TRACKER_LIB, LPR_CONFIG,
)
from .camera_config import CameraConfig, compute_tiler_layout

logger = logging.getLogger(__name__)

Gst.init(None)


# ---------------------------------------------------------------------------
# URI helpers
# ---------------------------------------------------------------------------

def normalize_uri(uri: str) -> str:
    """Ensure the URI has a valid scheme."""
    if uri.startswith(("file://", "rtsp://", "rtmp://", "http://")):
        return uri
    if os.path.exists(uri):
        return "file://" + os.path.abspath(uri)
    return uri


def is_file_uri(uri: str) -> bool:
    return uri.startswith("file://") or (
        os.path.isabs(uri) and os.path.isfile(uri)
    )


# ---------------------------------------------------------------------------
# Source bin factory
# ---------------------------------------------------------------------------

def _make_source_bin(
    pipeline: Gst.Pipeline,
    streammux: Gst.Element,
    cam_cfg: CameraConfig,
    ready_event: Optional[threading.Event] = None,
) -> Gst.Element:
    """
    Create a source bin for one camera and connect it to streammux.
    Returns the source element (uridecodebin) so it can be removed later.

    Element naming convention: "src-{camera_id}"
    """
    uri = normalize_uri(cam_cfg.uri)
    is_file = is_file_uri(uri)
    source_id = cam_cfg.source_id
    elem_name = f"src-{cam_cfg.camera_id}"

    source = make_element(elem_name, "uridecodebin")
    source.set_property("uri", uri)

    def on_source_setup(decodebin, src):
        if not is_file:
            for prop, val in [
                ("latency", 200),
                ("drop-on-latency", True),
                ("protocols", 0x4),  # rtspsrc TCP transport (GST_RTSP_LOWER_TRANS_TCP)
                ("retry", 5),
                ("timeout", 5_000_000),  # 5s in microseconds
            ]:
                try:
                    src.set_property(prop, val)
                except (TypeError, Exception):
                    pass

    source.connect("source-setup", on_source_setup)
    pipeline.add(source)

    def on_pad_added(decodebin, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or not caps.to_string().startswith("video/"):
            return
        pad_name = f"sink_{source_id}"
        sinkpad = streammux.get_request_pad(pad_name)
        if sinkpad and not sinkpad.is_linked():
            q = make_element(f"q_{cam_cfg.camera_id}", "queue")
            q.set_property("max-size-buffers", 4)
            q.set_property("leaky", 2)          # leaky downstream
            conv = make_element(f"conv_{cam_cfg.camera_id}", "nvvideoconvert")
            pipeline.add(q)
            pipeline.add(conv)
            q.sync_state_with_parent()
            conv.sync_state_with_parent()

            # ponytail: no BUFFER probe here anymore.  Input FPS is counted
            # from the same OSD sink-pad counter as output FPS (see
            # SpeedProbe._fps_frame_count), so both always share the same
            # writer telemetry window — no independent source probe to burst.
            pad.link(q.get_static_pad("sink"))
            gst_link(q, conv)
            conv_src_pad = conv.get_static_pad("src")
            if ready_event is not None:
                def _first_buffer(pad, info, event=ready_event):
                    event.set()
                    return Gst.PadProbeReturn.REMOVE
                conv_src_pad.add_probe(Gst.PadProbeType.BUFFER, _first_buffer)
            conv_src_pad.link(sinkpad)

            logger.info(
                "[Pipeline] Camera '%s' (source_id=%d) linked → sink_%d",
                cam_cfg.camera_id, source_id, source_id,
            )

    source.connect("pad-added", on_pad_added)
    return source


def _add_file_recording_branch(
    pipeline: Gst.Pipeline,
    demux: Gst.Element,
    cam_cfg: CameraConfig,
    sync: bool = False,
) -> None:
    """Create one nvstreamdemux → encoder → filesink branch for cam_cfg."""
    if not cam_cfg.record:
        return

    sid = cam_cfg.source_id
    if pipeline.get_by_name(f"queue_file_{sid}"):
        return

    queue = make_element(f"queue_file_{sid}", "queue")
    postosd = make_element(f"postosd_{sid}", "nvvideoconvert")
    enc = make_element(f"enc_{sid}", "nvv4l2h264enc")
    enc.set_property("bitrate", 10_000_000)
    enc.set_property("preset-level", 1)
    enc.set_property("insert-sps-pps", True)

    parse = make_element(f"parse_{sid}", "h264parse")
    muxer = make_element(f"mux_{sid}", "qtmux")
    # faststart allows MP4 file to be viewable even if it crashes midway
    muxer.set_property("faststart", True)

    fsink = make_element(f"fsink_{sid}", "filesink")
    fsink.set_property("sync", False)
    os.makedirs(os.path.dirname(os.path.abspath(cam_cfg.record_path)), exist_ok=True)
    fsink.set_property("location", os.path.abspath(cam_cfg.record_path))

    elements = [queue, postosd, enc, parse, muxer, fsink]
    for el in elements:
        pipeline.add(el)

    gst_link(queue, postosd, enc, parse, muxer, fsink)

    srcpad = demux.get_request_pad(f"src_{sid}")
    sinkpad = queue.get_static_pad("sink")
    if srcpad and sinkpad and not sinkpad.is_linked():
        srcpad.link(sinkpad)

    if sync:
        for el in elements:
            el.sync_state_with_parent()


def _remove_file_recording_branch(pipeline: Gst.Pipeline, source_id: int) -> None:
    demux = pipeline.get_by_name("demux")
    elements = [
        pipeline.get_by_name(f"queue_file_{source_id}"),
        pipeline.get_by_name(f"postosd_{source_id}"),
        pipeline.get_by_name(f"enc_{source_id}"),
        pipeline.get_by_name(f"parse_{source_id}"),
        pipeline.get_by_name(f"mux_{source_id}"),
        pipeline.get_by_name(f"fsink_{source_id}"),
    ]

    for el in elements:
        if el:
            el.set_state(Gst.State.NULL)

    if demux:
        srcpad = demux.get_static_pad(f"src_{source_id}")
        queue = elements[0]
        sinkpad = queue.get_static_pad("sink") if queue else None
        if srcpad and sinkpad and srcpad.is_linked():
            srcpad.unlink(sinkpad)
        if srcpad:
            demux.release_request_pad(srcpad)

    for el in elements:
        if el:
            pipeline.remove(el)


# ---------------------------------------------------------------------------
# Main pipeline builder (Multi-Stream)
# ---------------------------------------------------------------------------

def build_pipeline(
    camera_configs: list[CameraConfig],
    sink_type: str = "display",
    mux_width: int = 1920,
    mux_height: int = 1080,
    analytics_config: str = None,
    **kwargs,
):
    """
    Build a multi-stream DeepStream pipeline.
    """
    if not camera_configs:
        raise ValueError("camera_configs must not be empty.")

    n_cameras = len(camera_configs)

    if analytics_config is None:
        analytics_config = str(ANALYTICS_CFG)

    pipeline = Gst.Pipeline.new(f"ds-multi-pipeline-{sink_type}")

    # ── Muxer ────────────────────────────────────────────────────────────────
    streammux = make_element("stream-muxer", "nvstreammux")
    streammux.set_property("batch-size", n_cameras)
    streammux.set_property("width", mux_width)
    streammux.set_property("height", mux_height)
    streammux.set_property("batched-push-timeout", 33_000)
    # live-source=1 (arrival-rate push) is correct for live RTSP sources,
    # but for pure file playback it lets the muxer run at decode speed, so
    # output FPS can exceed the source file's native FPS.  live-source=0
    # paces the muxer to the sources' PTS → realtime playback (output FPS
    # ≤ source FPS).  Mixed live+file pipelines must stay 1 (a live source
    # would stall under PTS pacing); probes.py telemetry exposes the
    # decision as muxer_live_source so downstream can interpret FPS.
    live_source = (
        0 if all(is_file_uri(normalize_uri(c.uri)) for c in camera_configs)
        else 1
    )
    streammux.set_property("live-source", live_source)
    streammux.set_property("attach-sys-ts", True)

    # ── Core AI processing ───────────────────────────────────────────────────
    pgie = make_element("primary-infer", "nvinfer")
    pgie.set_property("config-file-path", str(INFER_CONFIG))

    tracker = make_element("tracker", "nvtracker")
    tracker.set_property("ll-lib-file", str(TRACKER_LIB))
    tracker.set_property("ll-config-file", str(TRACKER_CFG))
    tracker.set_property("tracker-width", 224)
    tracker.set_property("tracker-height", 224)
    tracker.set_property("gpu_id", 0)

    sgie = make_element("secondary-infer", "nvinfer")
    sgie.set_property("config-file-path", str(SGIE_CONFIG))

    sgie2 = make_element("lpr-classifier", "nvinfer")
    sgie2.set_property("config-file-path", str(LPR_CONFIG))

    analytics = make_element("analytics", "nvdsanalytics")
    analytics.set_property("config-file", analytics_config)

    # ── Determine display / file-write strategy ──────────────────────────────
    is_tiled = (sink_type in ["display", "rtsp_push"])

    # ── Tiler (only create when a tiled grid is needed) ───────────────────────
    if is_tiled:
        tiler = make_element("tiler", "nvmultistreamtiler")
        # Grid is computed from the INITIAL camera count so it looks square.
        # It must NOT change while the pipeline is running — resizing rows/cols
        # on a live tiler causes a VIC scaling crash on Jetson.  Dynamic add/remove
        # reuses the existing slots without touching the grid dimensions.
        rows, cols = compute_tiler_layout(n_cameras)
        tiler.set_property("rows", int(rows))
        tiler.set_property("columns", int(cols))
        tiler.set_property("width", mux_width)
        tiler.set_property("height", mux_height)
        tiler.set_property("gpu-id", 0)

        logger.info("[Pipeline] Tiler layout: %d×%d for %d streams", rows, cols, n_cameras)
    else:
        tiler = None

    # ── Pre-OSD convert ──────────────────────────────────────────────────────
    preosd_convert = make_element("preosd_convert", "nvvideoconvert")
    preosd_caps = make_element("preosd_caps", "capsfilter")
    preosd_caps.set_property(
        "caps", Gst.Caps.from_string("video/x-raw(memory:NVMM), format=RGBA")
    )

    nvdsosd = make_element("onscreendisplay", "nvdsosd")
    nvdsosd.set_property("display-text", 1)
    nvdsosd.set_property("display-bbox", 1)
    nvdsosd.set_property("process-mode", 2)
    nvdsosd.set_property("gpu-id", 0)

    # ── Sink-specific elements & Routing ─────────────────────────────────────
    sink_elements: list = []

    if sink_type == "display":
        conv = make_element("conv", "nvvideoconvert")
        conv_caps = make_element("conv_caps", "capsfilter")
        conv_caps.set_property(
            "caps", Gst.Caps.from_string("video/x-raw(memory:NVMM), format=NV12")
        )
        eglT = make_element("eglT", "nvegltransform")
        sink = make_element("display", "nveglglessink")
        sink.set_property("sync", False)
        sink.set_property("qos", False)
        sink.set_property("async", False)
        sink.set_property("max-lateness", -1)
        sink_elements = [conv, conv_caps, eglT, sink]

    elif sink_type == "rtsp_push":
        conv = make_element("conv", "nvvideoconvert")
        scale_caps = make_element("scale_caps", "capsfilter")
        scale_caps.set_property(
            "caps", Gst.Caps.from_string(
                "video/x-raw(memory:NVMM), format=NV12, width=1280, height=720"
            )
        )
        enc = make_element("enc", "nvv4l2h264enc")
        enc.set_property("insert-sps-pps", True)
        enc.set_property("iframeinterval", 30)
        enc.set_property("bitrate", kwargs.get("bitrate", 4_000_000))
        try:
            enc.set_property("maxperf-enable", True)
        except (TypeError, Exception):
            pass
        parse = make_element("parse", "h264parse")
        sink = make_element("rtsp_push_sink", "rtspclientsink")
        sink.set_property("location", kwargs["rtsp_push_url"])
        sink.set_property("protocols", "tcp")
        sink.set_property("latency", 0)
        sink_elements = [conv, scale_caps, enc, parse, sink]

    elif sink_type == "file":
        # ── Demuxer ──
        demux = make_element("demux", "nvstreamdemux")
        pipeline.add(demux)

        for cam_cfg in camera_configs:
            _add_file_recording_branch(pipeline, demux, cam_cfg)

    else:
        raise ValueError(f"Unknown sink_type: '{sink_type}'")

    # ── Add core elements to pipeline ─────────────────────────────────────────
    core_elements = [
        streammux, pgie, tracker, sgie, sgie2,
        analytics, preosd_convert, preosd_caps, nvdsosd,
    ]
    if is_tiled:
        core_elements.insert(-1, tiler)  # Add tiler before nvdsosd, after RGBA caps

    for el in core_elements + sink_elements:
        pipeline.add(el)

    # ── Link core chain ───────────────────────────────────────────────────────
    if is_tiled:
        gst_link(
            streammux, pgie, tracker, sgie, sgie2,
            analytics, preosd_convert, preosd_caps, tiler, nvdsosd,
        )
    else:
        gst_link(
            streammux, pgie, tracker, sgie, sgie2,
            analytics, preosd_convert, preosd_caps, nvdsosd,
        )

    # ── Link sink chain ───────────────────────────────────────────────────────
    if sink_type == "display":
        conv, conv_caps, eglT, sink = sink_elements
        gst_link(nvdsosd, conv, conv_caps, eglT, sink)

    elif sink_type == "rtsp_push":
        conv, scale_caps, enc, parse, sink = sink_elements
        gst_link(nvdsosd, conv, scale_caps, enc, parse, sink)

    elif sink_type == "file":
        # Connect OSD to Demux
        nvdsosd.get_static_pad("src").link(demux.get_static_pad("sink"))

    # ── Add source bins (N cameras) ───────────────────────────────────────────
    source_bins: dict[str, Gst.Element] = {}
    for cam_cfg in camera_configs:
        src = _make_source_bin(pipeline, streammux, cam_cfg)
        source_bins[cam_cfg.camera_id] = src

    logger.info(
        "[Pipeline] Built multi-stream pipeline: %d cameras, sink=%s",
        n_cameras, sink_type,
    )

    return pipeline, nvdsosd, streammux, source_bins


# ---------------------------------------------------------------------------
# Dynamic stream add/remove helpers (Phase 3)
# ---------------------------------------------------------------------------

def _remove_fake_black_source(pipeline: Gst.Pipeline, streammux: Gst.Element, source_id: int) -> None:
    """Remove videotestsrc pattern=2 black placeholder for source_id if present."""
    fake_src = pipeline.get_by_name(f"fake_src_{source_id}")
    if not fake_src:
        return
    fake_conv = pipeline.get_by_name(f"fake_conv_{source_id}")
    fake_elements = [el for el in [fake_src, fake_conv] if el is not None]
    for el in fake_elements:
        el.set_state(Gst.State.NULL)
    if fake_conv:
        conv_pad = fake_conv.get_static_pad("src")
        mux_pad = conv_pad.get_peer() if conv_pad and conv_pad.is_linked() else None
        if conv_pad and mux_pad and conv_pad.is_linked():
            conv_pad.unlink(mux_pad)
        if mux_pad:
            streammux.release_request_pad(mux_pad)
    for el in fake_elements:
        pipeline.remove(el)
    logger.info("[Pipeline] Removed fake black source for slot source_id=%d", source_id)


def _add_fake_black_source(pipeline: Gst.Pipeline, streammux: Gst.Element, source_id: int) -> None:
    """Add videotestsrc pattern=2 (black) to keep tiler slot black."""
    try:
        sinkpad = streammux.get_request_pad(f"sink_{source_id}")
        if not sinkpad:
            return
        fake_src = make_element(f"fake_src_{source_id}", "videotestsrc")
        fake_src.set_property("pattern", 2)  # 2 = black
        fake_conv = make_element(f"fake_conv_{source_id}", "nvvideoconvert")
        pipeline.add(fake_src)
        pipeline.add(fake_conv)
        gst_link(fake_src, fake_conv)
        conv_pad = fake_conv.get_static_pad("src")
        if conv_pad and sinkpad:
            conv_pad.link(sinkpad)
        fake_src.sync_state_with_parent()
        fake_conv.sync_state_with_parent()
        logger.info("[Pipeline] Added fake black source for freed slot source_id=%d", source_id)
    except Exception as exc:
        logger.warning("[Pipeline] Could not add fake black source for slot source_id=%d: %s", source_id, exc)


def dynamic_add_stream(
    pipeline: Gst.Pipeline,
    streammux: Gst.Element,
    cam_cfg: CameraConfig,
    tiler: Gst.Element,
    source_bins: dict,
    ready_event: Optional[threading.Event] = None,
) -> Gst.Element:
    # Remove fake black source if it was occupying this source_id slot
    _remove_fake_black_source(pipeline, streammux, cam_cfg.source_id)

    # Cleanup stale source_bin from a previous failed ADD attempt.
    # If the prior _send_ack timed out without calling dynamic_remove_stream,
    # the old uridecodebin may still occupy sink_N (sinkpad.is_linked()==True),
    # causing on_pad_added to skip linking → ready_event never fires → retry TIMEOUT.
    # ponytail: tear down stale bin here so retry gets a clean pad.
    stale_src = source_bins.get(cam_cfg.camera_id)
    if stale_src is not None and stale_src.get_parent() is not None:
        # Unlink the queue/conv chain from streammux sink pad
        stale_conv = pipeline.get_by_name(f"conv_{cam_cfg.camera_id}")
        if stale_conv is not None:
            conv_src = stale_conv.get_static_pad("src")
            if conv_src and conv_src.is_linked():
                mux_sinkpad = conv_src.get_peer()
                conv_src.unlink(mux_sinkpad)
                if mux_sinkpad:
                    streammux.release_request_pad(mux_sinkpad)
            stale_conv.set_state(Gst.State.NULL)
            pipeline.remove(stale_conv)
        stale_q = pipeline.get_by_name(f"q_{cam_cfg.camera_id}")
        if stale_q is not None:
            stale_q.set_state(Gst.State.NULL)
            pipeline.remove(stale_q)
        stale_src.set_state(Gst.State.NULL)
        pipeline.remove(stale_src)
        source_bins.pop(cam_cfg.camera_id, None)
        # Restore batch-size incremented by the prior failed ADD
        _bs = streammux.get_property("batch-size")
        if _bs > 1:
            streammux.set_property("batch-size", _bs - 1)
        logger.info(
            "[Pipeline] Removed stale source_bin for '%s' (source_id=%d) before retry ADD.",
            cam_cfg.camera_id, cam_cfg.source_id,
        )

    # 1. Read current batch-size from the live streammux rather than trusting
    #    a caller-supplied value that may have been stale before GLib idle_add
    #    dispatched this callback.
    old_batch_size = streammux.get_property("batch-size")

    # 2. Increase muxer batch-size BEFORE creating any sources/recording
    #    branches so that if creation fails we can roll the batch-size back
    #    to its previous value and leave the pipeline unchanged.
    streammux.set_property("batch-size", old_batch_size + 1)

    # File mode uses nvstreamdemux branches. Dynamic ADD must create the
    # matching branch before frames for the new source_id start flowing.
    demux = pipeline.get_by_name("demux")
    recording_added = False
    try:
        if demux is not None:
            _add_file_recording_branch(pipeline, demux, cam_cfg, sync=True)
            recording_added = True

        # 3. Add and start the new source
        src = _make_source_bin(pipeline, streammux, cam_cfg, ready_event=ready_event)
        src.sync_state_with_parent()
    except Exception:
        # Rollback: restore batch-size and tear down any partially-created
        # recording branch so the pipeline returns to the state it was in
        # before this call.
        streammux.set_property("batch-size", old_batch_size)
        if recording_added:
            _remove_file_recording_branch(pipeline, cam_cfg.source_id)
        raise

    source_bins[cam_cfg.camera_id] = src
    logger.info(
        "[Pipeline] Added stream '%s' (source_id=%d), batch-size %d → %d",
        cam_cfg.camera_id, cam_cfg.source_id, old_batch_size, old_batch_size + 1,
    )
    return src




def dynamic_remove_stream(
    pipeline: Gst.Pipeline,
    streammux: Gst.Element,
    camera_id: str,
    source_id: int,
    tiler: Gst.Element,
    source_bins: dict,
    done_event: Optional[threading.Event] = None,
) -> None:
    src = source_bins.get(camera_id)
    if not src:
        if done_event is not None:
            done_event.set()
        return

    # Jetson removal risk (known limitation):
    # The BLOCK_DOWNSTREAM + idle_add pattern is the documented GStreamer-safe
    # way to remove a src pad from a running nvstreammux.  However, DeepStream
    # 6.x on Jetson may still log harmless pad-spurious warnings or, in rare
    # cases, trigger a short-lived mux hiccup on the remaining streams because
    # nvstreammux doesn't entirely isolate per-sink-pad internal state.  No
    # async retry or pad-blocking refinements are added here — those belong
    # to a separate runtime/native/probes change by other writers.

    conv = pipeline.get_by_name(f"conv_{camera_id}")
    conv_src_pad = conv.get_static_pad("src") if conv else None
    cleanup_lock = threading.Lock()
    cleanup_scheduled = False
    cleanup_started = False

    def _cleanup_bin(pad, probe_id):
        nonlocal cleanup_started
        with cleanup_lock:
            if cleanup_started:
                return False
            cleanup_started = True

        probe_removed = False
        try:
            # Capture elements and pads before state changes / unlink
            source_elem = src
            q_elem = pipeline.get_by_name(f"q_{camera_id}")
            conv_elem = pipeline.get_by_name(f"conv_{camera_id}")
            conv_pad = conv_src_pad or (conv_elem.get_static_pad("src") if conv_elem else None)
            # sink_N is a request pad — get_static_pad returns None for it.
            # Use the conv src pad's peer (the mux sink it's linked to).
            mux_sinkpad = conv_pad.get_peer() if conv_pad and conv_pad.is_linked() else None

            # Transition source branch elements sequentially PLAYING -> PAUSED -> READY -> NULL
            branch_elements = [el for el in [source_elem, q_elem, conv_elem] if el is not None]
            for target_state in (Gst.State.PAUSED, Gst.State.READY, Gst.State.NULL):
                for el in branch_elements:
                    el.set_state(target_state)
                for el in branch_elements:
                    state_ret, current_state, _ = el.get_state(5 * Gst.SECOND)
                    if state_ret == Gst.StateChangeReturn.FAILURE:
                        raise RuntimeError(
                            f"Element {el.get_name()} failed to reach "
                            f"{target_state.value_nick}: state={current_state.value_nick}"
                        )

            # Unlink conv src from mux sink
            if conv_pad and mux_sinkpad and conv_pad.is_linked():
                conv_pad.unlink(mux_sinkpad)

            # Release mux request pad only after source is safely in NULL state
            if mux_sinkpad:
                streammux.release_request_pad(mux_sinkpad)

            # Remove blocking probe
            if pad and probe_id:
                try:
                    pad.remove_probe(probe_id)
                    probe_removed = True
                except Exception:
                    pass

            # Remove elements from pipeline
            for el in branch_elements:
                pipeline.remove(el)

            # Clean up recording branch
            _remove_file_recording_branch(pipeline, source_id)

            # Bookkeeping
            if camera_id in source_bins:
                del source_bins[camera_id]

            # Decrease batch size
            old_n = streammux.get_property("batch-size")
            new_n = max(1, old_n - 1)
            streammux.set_property("batch-size", new_n)

            # Display fix: add fake black source to keep slot black in tiler if tiler is present
            if tiler is not None:
                _add_fake_black_source(pipeline, streammux, source_id)

            logger.info(f"[Pipeline] Cleaned up resources for camera {camera_id}")
        except Exception as exc:
            logger.error(f"[Pipeline] Error during cleanup of camera {camera_id}: {exc}")
            return False
        finally:
            if pad and probe_id and not probe_removed:
                try:
                    pad.remove_probe(probe_id)
                except Exception:
                    pass
            if done_event is not None:
                done_event.set()

        return False

    def _blocking_probe(pad, info, _user_data):
        nonlocal cleanup_scheduled
        with cleanup_lock:
            if cleanup_scheduled:
                return Gst.PadProbeReturn.OK
            cleanup_scheduled = True

        # Keep probe active to maintain blocking state, schedule cleanup on GLib idle.
        GLib.idle_add(_cleanup_bin, pad, info.id)
        # Keep the pad blocked until cleanup removes this probe.
        return Gst.PadProbeReturn.OK

    if conv_src_pad:
        conv_src_pad.add_probe(
            Gst.PadProbeType.BLOCK_DOWNSTREAM, _blocking_probe, None
        )
    else:
        # If no pad exists, clean up immediately
        GLib.idle_add(_cleanup_bin, None, None)
