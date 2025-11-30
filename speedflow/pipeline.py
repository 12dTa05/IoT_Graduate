# # speedflow/pipeline.py
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
from .settings import INFER_CONFIG, TRACKER_CFG, ANALYTICS_CFG

def make_e(name, factory):
    e = Gst.ElementFactory.make(factory, name)
    if not e: raise RuntimeError(f"Failed to create: {factory}")
    return e

def build_rtsp_pipeline(rtsp_uri: str):
    Gst.init(None)
    pipeline = Gst.Pipeline()

    source = make_e("source-bin","uridecodebin")
    def on_source_setup(decodebin, src):
        # uridecodebin sẽ tạo rtspsrc khi URI là rtsp://
        # Cứ set, nếu property không tồn tại thì GStreamer sẽ báo warning (an toàn bỏ qua).
        try:
            src.set_property("latency", 100)             # ms
            src.set_property("drop-on-latency", True)    # rơi frame khi trễ
            # (tuỳ chọn, thường giúp giảm lag)
            # src.set_property("udp-buffer-size", 524288)
            # src.set_property("ntp-sync", True)
        except TypeError:
            pass
    source.connect("source-setup", on_source_setup)
    source.set_property("uri", rtsp_uri)

    streammux = make_e("stream-muxer","nvstreammux")
    streammux.set_property('batch-size', 1)
    streammux.set_property('width', 1280)
    streammux.set_property('height', 720)
    streammux.set_property('batched-push-timeout', 40000)
    streammux.set_property('live-source', 0)

    pgie = make_e("primary-infer","nvinfer")
    pgie.set_property('config-file-path', str(INFER_CONFIG))

    tracker = make_e("tracker","nvtracker")
    tracker.set_property('ll-lib-file', "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
    tracker.set_property('ll-config-file', str(TRACKER_CFG))
    tracker.set_property('tracker-width', 640)
    tracker.set_property('tracker-height', 384)
    tracker.set_property('gpu_id', 0)

    analytics = make_e("analytics","nvdsanalytics")
    analytics.set_property('config-file', str(ANALYTICS_CFG))

    nvdsosd = make_e("onscreendisplay","nvdsosd")
    nvdsosd.set_property("display-text", 1)
    nvdsosd.set_property("display-bbox", 1)   # tuỳ, nếu muốn vẫn vẽ bbox

    conv = make_e("conv","nvvideoconvert")
    eglT = make_e("eglT","nvegltransform")
    sink = make_e("display","nveglglessink")
    sink.set_property("sync", False)
    sink.set_property("qos", False)

    for e in [source, streammux, pgie, tracker, analytics, nvdsosd, conv, eglT, sink]:
        pipeline.add(e)

    def on_pad_added(decodebin, pad):
        caps = pad.get_current_caps()
        if not caps: return
        if caps.to_string().startswith("video/"):
            sinkpad = streammux.get_request_pad("sink_0")
            if sinkpad and not sinkpad.is_linked():
                pad.link(sinkpad)
    source.connect("pad-added", on_pad_added)

    streammux.link(pgie)
    pgie.link(tracker)
    tracker.link(analytics)
    analytics.link(nvdsosd)
    nvdsosd.link(conv)
    conv.link(eglT)
    eglT.link(sink)

    return pipeline, nvdsosd

# def build_rtmp_pipeline(rtsp_uri: str, rtmp_url: str, bitrate=4000000):
#     Gst.init(None)
#     pipeline = Gst.Pipeline()

#     source = make_e("source-bin","uridecodebin")
#     def on_source_setup(decodebin, src):
#         try:
#             src.set_property("latency", 100)
#             src.set_property("drop-on-latency", True)
#         except TypeError:
#             pass
#     source.connect("source-setup", on_source_setup)
#     source.set_property("uri", rtsp_uri)

#     streammux = make_e("stream-muxer","nvstreammux")
#     streammux.set_property('batch-size', 1)
#     streammux.set_property('width', 1280)
#     streammux.set_property('height', 720)
#     streammux.set_property('batched-push-timeout', 40000)
#     streammux.set_property('live-source', 1)

#     pgie = make_e("primary-infer","nvinfer")
#     pgie.set_property('config-file-path', str(INFER_CONFIG))

#     tracker = make_e("tracker","nvtracker")
#     tracker.set_property('ll-lib-file', "/opt/nvidia/deepstream/deepstream-6.3/lib/libnvds_nvmultiobjecttracker.so")
#     tracker.set_property('ll-config-file', str(TRACKER_CFG))
#     tracker.set_property('tracker-width', 640)
#     tracker.set_property('tracker-height', 360)
#     tracker.set_property('gpu_id', 0)

#     analytics = make_e("analytics","nvdsanalytics")
#     analytics.set_property('config-file', str(ANALYTICS_CFG))

#     nvdsosd = make_e("onscreendisplay","nvdsosd")
#     nvdsosd.set_property("display-text", 1)
#     nvdsosd.set_property("display-bbox", 1)

#     # --- branch encode → RTMP ---
#     conv = make_e("conv","nvvideoconvert")
#     enc  = make_e("enc","nvv4l2h264enc")
#     enc.set_property("insert-sps-pps", True)
#     enc.set_property("iframeinterval", 30)   # GOP ~1s nếu 30fps
#     enc.set_property("idrinterval", 30)
#     enc.set_property("bitrate", bitrate)     # 4 Mbps mặc định
#     enc.set_property("profile", 1)           # Main
#     enc.set_property("preset-level", 1)      # Low-latency
#     enc.set_property("control-rate", 1)      # CBR

#     parse= make_e("h264parse","h264parse")
#     parse.set_property("config-interval", 1) # lặp SPS/PPS mỗi IDR

#     mux  = make_e("flvmux","flvmux")
#     mux.set_property("streamable", True)

#     sink = make_e("rtmpsink","rtmpsink")
#     sink.set_property("location", rtmp_url)  # ví dụ: rtmp://<LAPTOP_IP>/live/jetson
#     sink.set_property("sync", False)
#     sink.set_property("async", False)

#     for e in [source, streammux, pgie, tracker, analytics, nvdsosd, conv, enc, parse, mux, sink]:
#         pipeline.add(e)

#     def on_pad_added(decodebin, pad):
#         caps = pad.get_current_caps()
#         if not caps: return
#         if caps.to_string().startswith("video/"):
#             sinkpad = streammux.get_request_pad("sink_0")
#             if sinkpad and not sinkpad.is_linked():
#                 pad.link(sinkpad)
#     source.connect("pad-added", on_pad_added)

#     streammux.link(pgie)
#     pgie.link(tracker)
#     tracker.link(analytics)
#     analytics.link(nvdsosd)
#     nvdsosd.link(conv)
#     conv.link(enc)
#     enc.link(parse)
#     parse.link(mux)
#     mux.link(sink)

#     return pipeline, nvdsosd
