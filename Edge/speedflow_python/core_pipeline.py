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
from gi.repository import Gst

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
        # Mux sink pads are pre-created once at build time and are NEVER
        # requested/released post-init (#596 crash class). A pad still linked
        # here can only be owned by our own black filler left by a previous
        # REMOVE — detach it, then link the real branch into the same pad.
        sinkpad = streammux.get_static_pad(pad_name)
        if sinkpad is None:
            logger.error(
                "[Pipeline] No permanent mux pad '%s' for camera '%s' "
                "(source_id=%d beyond slot capacity); ADD aborted.",
                pad_name, cam_cfg.camera_id, source_id,
            )
            return
        _detach_filler_from_pad(pipeline, streammux, source_id)
        if not sinkpad.is_linked():
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
    slot_capacity: int = None,
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

    # ── Permanent mux sink pads (crash-class fix) ─────────────────────────────
    # Every possible slot's request pad is created ONCE here, before the
    # pipeline ever runs, and is never released while the process lives.
    # Dynamic ADD/REMOVE only swaps the upstream branch linked into an
    # existing pad. Pad request/release on a PLAYING nvstreammux can corrupt
    # NvBufSurfacePool and wedge the Tegra kernel (#596), so post-init pad
    # churn is eliminated by construction. batch-size still tracks the number
    # of active branches exactly as before (unchanged GPU economics).
    if slot_capacity is None:
        # Deployment-wide source_id universe, NOT this node's own camera
        # count: any camera may arrive here via migration/failover carrying
        # its original source_id (e.g. sids 4-5 landing on a 2-camera node).
        # Idle request pads are inert (no branch linked, batch-size excludes
        # them), so a generous bound costs nothing at runtime.
        slot_capacity = int(os.environ.get("SPEEDFLOW_SLOT_CAPACITY", "16"))
    slot_capacity = max(int(slot_capacity), n_cameras)
    for sid in range(slot_capacity):
        streammux.get_request_pad(f"sink_{sid}")
    logger.info(
        "[Pipeline] Pre-created %d permanent mux sink pads (slot_capacity=%d)",
        slot_capacity, slot_capacity,
    )

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

# Orin NVDEC hardware decode-session ceiling is ~16-32 (#598); exceeding it
# yields unrecoverable OutputBufferUnavailable (reboot required). Gate ADDs at
# a conservative default well below the documented floor until device data
# justifies raising it. ponytail: single env knob instead of settings plumbing;
# raise SPEEDFLOW_NVDEC_SESSION_LIMIT once workload-swap data arrives.
NVDEC_SESSION_LIMIT = int(os.environ.get("SPEEDFLOW_NVDEC_SESSION_LIMIT", "14"))


def _iter_elements_deep(root: Gst.Element):
    """Yield every GstElement under root, recursing into bins."""
    if not isinstance(root, Gst.Bin):
        return
    it = root.iterate_recurse()
    while True:
        ret, el = it.next()
        if ret != Gst.IteratorResult.OK:
            return
        yield el


def _count_nvdec_decoders(pipeline: Gst.Pipeline) -> int:
    """Count live nvv4l2decoder elements ≈ active NVDEC hardware sessions."""
    n = 0
    for el in _iter_elements_deep(pipeline):
        factory = el.get_factory()
        if factory and factory.get_name() == "nvv4l2decoder":
            n += 1
    return n


def _proc_rss_mb() -> int:
    """Process RSS in MB (-1 if unreadable) for teardown-leak auditing."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return -1


def _detach_filler_from_pad(pipeline: Gst.Pipeline, streammux: Gst.Element, source_id: int) -> None:
    """Stop and remove a black filler occupying a permanent mux sink pad.

    The mux pad itself is NEVER released (#596 crash class): only the
    upstream filler branch is torn down so the permanent pad becomes free
    for the real source branch.
    """
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
    for el in fake_elements:
        pipeline.remove(el)
    logger.info("[Pipeline] Detached black filler from permanent pad sink_%d", source_id)


def _add_fake_black_source(pipeline: Gst.Pipeline, streammux: Gst.Element, source_id: int) -> None:
    """Add videotestsrc pattern=2 (black) to keep tiler slot black."""
    try:
        # ponytail: never get_request_pad post-init (#596 crash class) — the
        # permanent pad was pre-created at build time via slot_capacity.
        sinkpad = streammux.get_static_pad(f"sink_{source_id}")
        if sinkpad is None:
            logger.warning(
                "[Pipeline] No permanent mux pad sink_%d; cannot attach black filler.",
                source_id,
            )
            return
        if sinkpad.is_linked():
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
    # Cleanup stale source_bin from a previous failed ADD attempt.
    # If the prior _send_ack timed out without a REMOVE completing, the old
    # uridecodebin may still occupy this slot (its mux pad stays linked).
    # Route through the SAME sequential teardown as dynamic_remove_stream:
    # direct-to-NULL slams leave Tegra nvv4l2decoder registers undefined
    # (#597 kernel v4l2 deadlock), and pad release is forbidden post-init (#596).
    stale_src = source_bins.get(cam_cfg.camera_id)
    if stale_src is not None and stale_src.get_parent() is not None:
        logger.info(
            "[Pipeline] Removing stale source_bin for '%s' (source_id=%d) before retry ADD.",
            cam_cfg.camera_id, cam_cfg.source_id,
        )
        _teardown_source_branch(
            pipeline, streammux, cam_cfg.camera_id, cam_cfg.source_id,
            tiler, source_bins,
        )

    # NVDEC session gate (#598): exceeding the Orin decode-session ceiling is
    # unrecoverable (reboot required). Refuse BEFORE mutating anything; the
    # caller's exception path disables the config and a later REMOVE becomes
    # a clean no-op.
    nvdec_count = _count_nvdec_decoders(pipeline)
    if nvdec_count >= NVDEC_SESSION_LIMIT:
        raise RuntimeError(
            f"NVDEC session limit reached ({nvdec_count} >= "
            f"{NVDEC_SESSION_LIMIT}); refusing ADD '{cam_cfg.camera_id}' "
            f"(source_id={cam_cfg.source_id})"
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




def _teardown_source_branch(
    pipeline: Gst.Pipeline,
    streammux: Gst.Element,
    camera_id: str,
    source_id: int,
    tiler: Optional[Gst.Element],
    source_bins: dict,
) -> None:
    """Single sequential teardown path for a live source branch.

    Used by dynamic_remove_stream AND the stale-bin cleanup in
    dynamic_add_stream so there is exactly one correct teardown
    implementation (#597): PLAYING→PAUSED→READY→NULL with get_state waits.
    The mux sink pad is NEVER released (#596 crash class); after the branch
    is gone the permanent pad is re-armed with a black filler (tiled sinks).
    """
    src = source_bins.get(camera_id)
    if not src:
        return

    pre_nvdec = _count_nvdec_decoders(pipeline)
    pre_rss = _proc_rss_mb()

    try:
        q_elem = pipeline.get_by_name(f"q_{camera_id}")
        conv_elem = pipeline.get_by_name(f"conv_{camera_id}")
        conv_pad = conv_elem.get_static_pad("src") if conv_elem else None
        mux_sinkpad = conv_pad.get_peer() if conv_pad and conv_pad.is_linked() else None

        # Transition source branch elements sequentially PLAYING -> PAUSED -> READY -> NULL
        branch_elements = [el for el in [src, q_elem, conv_elem] if el is not None]
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

        # Verify hardware decoder(s) really reached NULL — a bin can report
        # NULL while an inner nvv4l2decoder is wedged mid-teardown, silently
        # leaking its NVDEC session toward the #598 accumulation ceiling.
        for dec_el in _iter_elements_deep(src):
            factory = dec_el.get_factory()
            fname = factory.get_name() if factory else ""
            if fname.startswith("nvv4l2"):
                _, dec_state, _ = dec_el.get_state(0)
                if dec_state != Gst.State.NULL:
                    logger.critical(
                        "[Pipeline] '%s' (%s) stuck at %s after bin NULL for "
                        "camera %s — NVDEC session leak risk!",
                        dec_el.get_name(), fname, dec_state.value_nick, camera_id,
                    )

        # Unlink conv src from the mux sink pad.
        # ponytail: release_request_pad deliberately omitted — pads are permanent (#596).
        if conv_pad and mux_sinkpad and conv_pad.is_linked():
            conv_pad.unlink(mux_sinkpad)

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

        # Display fix: re-arm the permanent pad with black filler for tiled sinks
        if tiler is not None:
            _add_fake_black_source(pipeline, streammux, source_id)

        # Leak audit (on-device evidence): nvdec must drop by exactly the
        # number of removed branches (-1 here); rss creep across many cycles
        # flags Python/GObject ref leaks or unfreed NvBufSurface memory.
        logger.info(
            "[Pipeline] Teardown audit '%s': nvdec %d→%d, rss %dMB→%dMB",
            camera_id,
            pre_nvdec, _count_nvdec_decoders(pipeline),
            pre_rss, _proc_rss_mb(),
        )
        logger.info("[Pipeline] Cleaned up resources for camera %s", camera_id)
    except Exception as exc:
        logger.error("[Pipeline] Error during cleanup of camera %s: %s", camera_id, exc)


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

    logger.info(
        "[Pipeline] Removing stream '%s' (source_id=%d) synchronously",
        camera_id,
        source_id,
    )
    # Synchronous teardown on the GLib main thread: avoids BLOCK_DOWNSTREAM pad
    # probe deadlocks when RTSP streams are stalled and buffers stop flowing.
    _teardown_source_branch(pipeline, streammux, camera_id, source_id, tiler, source_bins)
    if done_event is not None:
        done_event.set()
