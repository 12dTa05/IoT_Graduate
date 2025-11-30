
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
from .settings import INFER_CONFIG, TRACKER_CFG, ANALYTICS_CFG

def make_e(name, factory):
    e = Gst.ElementFactory.make(factory, name)
    if not e: raise RuntimeError(f"Failed to create: {factory}")
    return e

def build_file_pipeline(rtsp_uri: str):
    # ==== GStreamer pipeline ====
    Gst.init(None)
    pipeline = Gst.Pipeline()
    source = Gst.ElementFactory.make("filesrc", "file-source")
    decoder = Gst.ElementFactory.make("decodebin", "decoder")
    streammux = Gst.ElementFactory.make("nvstreammux", "stream-muxer")
    pgie = Gst.ElementFactory.make("nvinfer", "primary-infer")
    tracker = Gst.ElementFactory.make("nvtracker", "tracker")
    nvdsanalytics = Gst.ElementFactory.make("nvdsanalytics", "analytics")
    nvdsosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    nvdsosd.set_property("display-text", 1)
    nvdsosd.set_property("display-bbox", 1)   # tuỳ, nếu muốn vẫn vẽ bbox

    encoder = Gst.ElementFactory.make("nvv4l2h264enc", "encoder")
    parser = Gst.ElementFactory.make("h264parse", "parser")
    muxer = Gst.ElementFactory.make("qtmux", "muxer")
    sink = Gst.ElementFactory.make("filesink", "filesink")
    sink.set_property("location", "/home/mta/output_highway2.mp4")
    nvdsanalytics.set_property('config-file', str(ANALYTICS_CFG))

    for comp in [source, decoder, streammux, pgie, tracker, nvdsanalytics, nvdsosd, encoder, parser, muxer, sink]:
        if not comp:
            print("Failed to create component")
            sys.exit(1)

    #source.set_property('location', "/home/mta/highway2.mp4")
    source.set_property('location', "/home/mta/vehicles.mp4")
    streammux.set_property('batch-size', 1)
    streammux.set_property('width', 1920)
    streammux.set_property('height', 1080)
    streammux.set_property('batched-push-timeout', 40000)
    pgie.set_property('config-file-path', str(INFER_CONFIG))
    tracker.set_property('ll-lib-file', "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
    tracker.set_property('ll-config-file', str(TRACKER_CFG))
    tracker.set_property('tracker-width', 640)
    tracker.set_property('tracker-height', 360)
    tracker.set_property('gpu_id', 0)

    pipeline.add(source)
    pipeline.add(decoder)
    pipeline.add(streammux)
    pipeline.add(pgie)
    pipeline.add(tracker)
    pipeline.add(nvdsanalytics)
    pipeline.add(nvdsosd)
    pipeline.add(encoder)
    pipeline.add(parser)
    pipeline.add(muxer)
    pipeline.add(sink)

    #ket noi pipeline với video
    source.link(decoder)
    def decode_src_created(decodebin, pad, data):
        caps = pad.get_current_caps()
        gstname = caps.to_string()
        if gstname.find("video") != -1:
            sinkpad = streammux.get_request_pad("sink_0")
            if not sinkpad:
                print("Unable to get the sink pad of streammux")
                return
            pad.link(sinkpad)
    decoder.connect("pad-added", decode_src_created, streammux)

    streammux.link(pgie)
    pgie.link(tracker)
    tracker.link(nvdsanalytics)
    nvdsanalytics.link(nvdsosd)
    nvdsosd.link(encoder)
    encoder.link(parser)
    parser.link(muxer)
    muxer.link(sink)
    return pipeline, nvdsosd
