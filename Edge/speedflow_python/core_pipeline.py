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
            for prop, val in [("latency", 200), ("drop-on-latency", True)]:
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
            pad.link(q.get_static_pad("sink"))
            gst_link(q, conv)
            conv.get_static_pad("src").link(sinkpad)
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
    streammux.set_property("live-source", 1)   # mostly RTSP live sources
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
        core_elements.insert(-3, tiler)  # Add tiler before preosd_convert

    for el in core_elements + sink_elements:
        pipeline.add(el)

    # ── Link core chain ───────────────────────────────────────────────────────
    if is_tiled:
        gst_link(
            streammux, pgie, tracker, sgie, sgie2,
            analytics, tiler, preosd_convert, preosd_caps, nvdsosd,
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

def dynamic_add_stream(
    pipeline: Gst.Pipeline,
    streammux: Gst.Element,
    cam_cfg: CameraConfig,
    tiler: Gst.Element,
    source_bins: dict,
    current_n: int,
) -> Gst.Element:
    # 1. Increase muxer batch-size
    streammux.set_property("batch-size", current_n + 1)

    # File mode uses nvstreamdemux branches. Dynamic ADD must create the
    # matching branch before frames for the new source_id start flowing.
    demux = pipeline.get_by_name("demux")
    if demux is not None:
        _add_file_recording_branch(pipeline, demux, cam_cfg, sync=True)
    
    # 2. Add and start the new source
    src = _make_source_bin(pipeline, streammux, cam_cfg)
    src.sync_state_with_parent()

    source_bins[cam_cfg.camera_id] = src
    return src




def dynamic_remove_stream(
    pipeline: Gst.Pipeline,
    streammux: Gst.Element,
    camera_id: str,
    source_id: int,
    tiler: Gst.Element,
    source_bins: dict,
    current_n: int,
) -> None:
    src = source_bins.get(camera_id)
    if not src:
        return

    from gi.repository import GLib

    conv = pipeline.get_by_name(f"conv_{camera_id}")
    conv_src_pad = conv.get_static_pad("src") if conv else None

    def _cleanup_bin(pad, probe_id):
        # 1. Remove blocking probe first so teardown cannot deadlock on a blocked pad.
        if pad and probe_id:
            try:
                pad.remove_probe(probe_id)
            except Exception:
                pass

        # 2. Unlink from muxer (streammux) after the blocking probe has been removed.
        mux_sinkpad = streammux.get_static_pad(f"sink_{source_id}")
        if mux_sinkpad:
            if conv_src_pad:
                conv_src_pad.unlink(mux_sinkpad)

        # 3. Set state to NULL to stop data flow from source to sink.
        if src:
            src.set_state(Gst.State.NULL)
        for prefix in [f"q_{camera_id}", f"conv_{camera_id}"]:
            el = pipeline.get_by_name(prefix)
            if el:
                el.set_state(Gst.State.NULL)

        if mux_sinkpad:
            streammux.release_request_pad(mux_sinkpad)

        # 4. Remove element from pipeline
        if src:
            pipeline.remove(src)
        for prefix in [f"q_{camera_id}", f"conv_{camera_id}"]:
            el = pipeline.get_by_name(prefix)
            if el:
                pipeline.remove(el)

        _remove_file_recording_branch(pipeline, source_id)

        if camera_id in source_bins:
            del source_bins[camera_id]

        new_n = max(1, current_n - 1)
        
        # Decrease batch-size
        streammux.set_property("batch-size", new_n)
        
        # Note: Do not change tiler rows/cols to avoid VIC error on Jetson
             
        logger.info(f"[Pipeline] Cleaned up resources for camera {camera_id}")


        return False

    def _blocking_probe(pad, info, _user_data):
        # Do not remove probe here to maintain block state
        GLib.idle_add(_cleanup_bin, pad, info.id)
        # DROP current buffer to prevent it from leaking through on unlink
        return Gst.PadProbeReturn.DROP

    if conv_src_pad:
        conv_src_pad.add_probe(
            Gst.PadProbeType.BLOCK_DOWNSTREAM, _blocking_probe, None
        )
    else:
        # If no pad exists, clean up immediately
        GLib.idle_add(_cleanup_bin, None, None)
