# speedflow/pipeline_file.py
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
from .settings import INFER_CONFIG, TRACKER_CFG, ANALYTICS_CFG, MUX_WIDTH, MUX_HEIGHT

def make_e(name, factory):
    e = Gst.ElementFactory.make(factory, name)
    if not e: raise RuntimeError(f"Failed to create: {factory}")
    return e

def build_file_pipeline(rtsp_uri: str = "unused"):
    """Build pipeline for file input with file output"""
    Gst.init(None)
    pipeline = Gst.Pipeline()

    # File source
    source = make_e("file-source", "filesrc")
    
    # Decoder
    decoder = make_e("decoder", "decodebin")
    
    # Stream muxer
    streammux = make_e("stream-muxer", "nvstreammux")
    streammux.set_property('batch-size', 1)
    streammux.set_property('width', MUX_WIDTH)
    streammux.set_property('height', MUX_HEIGHT)
    streammux.set_property('batched-push-timeout', 40000)
    streammux.set_property('live-source', 0)

    # Primary inference (YOLO)
    pgie = make_e("primary-infer", "nvinfer")
    pgie.set_property('config-file-path', str(INFER_CONFIG))

    # Tracker
    tracker = make_e("tracker", "nvtracker")
    tracker.set_property('ll-lib-file', "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
    tracker.set_property('ll-config-file', str(TRACKER_CFG))
    tracker.set_property('tracker-width', 640)
    tracker.set_property('tracker-height', 384)
    tracker.set_property('gpu_id', 0)

    # Analytics
    analytics = make_e("analytics", "nvdsanalytics")
    analytics.set_property('config-file', str(ANALYTICS_CFG))

    # OSD
    nvdsosd = make_e("onscreendisplay", "nvdsosd")
    nvdsosd.set_property("display-text", 1)
    nvdsosd.set_property("display-bbox", 1)

    # Video conversion and encoding for file output
    conv = make_e("conv", "nvvideoconvert")
    
    capsfilter = make_e("capsfilter", "capsfilter")
    caps = Gst.Caps.from_string("video/x-raw(memory:NVMM), format=I420")
    capsfilter.set_property("caps", caps)
    
    encoder = make_e("encoder", "nvv4l2h264enc")
    encoder.set_property("bitrate", 4000000)
    encoder.set_property("insert-sps-pps", True)
    
    parser = make_e("parser", "h264parse")
    
    muxer = make_e("muxer", "qtmux")
    
    sink = make_e("filesink", "filesink")
    sink.set_property("location", "/app/output/output.mp4")
    sink.set_property("sync", False)
    sink.set_property("async", False)

    # Add all elements to pipeline
    for e in [source, decoder, streammux, pgie, tracker, analytics, nvdsosd, 
              conv, capsfilter, encoder, parser, muxer, sink]:
        pipeline.add(e)

    # Link source to decoder
    source.link(decoder)

    # Connect decoder pad-added signal
    def on_pad_added(decodebin, pad):
        caps = pad.get_current_caps()
        if not caps: return
        if caps.to_string().startswith("video/"):
            sinkpad = streammux.get_request_pad("sink_0")
            if sinkpad and not sinkpad.is_linked():
                pad.link(sinkpad)
    
    decoder.connect("pad-added", on_pad_added)

    # Link the rest of the pipeline
    streammux.link(pgie)
    pgie.link(tracker)
    tracker.link(analytics)
    analytics.link(nvdsosd)
    nvdsosd.link(conv)
    conv.link(capsfilter)
    capsfilter.link(encoder)
    encoder.link(parser)
    parser.link(muxer)
    muxer.link(sink)

    return pipeline, nvdsosd