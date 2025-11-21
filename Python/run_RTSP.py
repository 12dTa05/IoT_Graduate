# run_rtsp.py
import gi, sys
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
from speedflow.pipeline import build_rtsp_pipeline
from speedflow.homography import load_points, ViewTransformer
from speedflow.settings import HOMO_YML
from speedflow.probes import SpeedProbe

def main():
    rtsp = sys.argv[1] if len(sys.argv) > 1 else "rtsp://admin:admin@192.168.1.168:554/ch01/1"
    pipeline, nvdsosd = build_rtsp_pipeline(rtsp)

    source, target = load_points(str(HOMO_YML))
    vt = ViewTransformer(source, target)
    probe = SpeedProbe(vt, roi_source_points=source)

    pad = nvdsosd.get_static_pad("sink")
    pad.add_probe(Gst.PadProbeType.BUFFER, probe.osd_sink_pad_buffer_probe, None)

    pipeline.set_state(Gst.State.PLAYING)
    print("Pipeline is running...")
    loop = GLib.MainLoop()
    try: loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

if __name__ == "__main__":
    main()
