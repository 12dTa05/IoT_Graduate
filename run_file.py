#!/usr/bin/env python3
# run_file.py
import argparse
import os
import sys
import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from speedflow.pipeline_file import build_file_pipeline  
from speedflow.homography import load_points, ViewTransformer
from speedflow.settings import HOMO_YML
from speedflow.probes import SpeedProbe


def main():
    parser = argparse.ArgumentParser(
        description="Run DeepStream file pipeline (filesrc→decodebin) with speed overlay & CSV log"
    )
    parser.add_argument(
        "video",
        help="Đường dẫn tới file video đầu vào (mp4/avi/...)"
    )
    parser.add_argument(
        "--homo",
        default=str(HOMO_YML),
        help="YAML chứa SOURCE/TARGET cho homography (mặc định: homography/points_source_target.yml)"
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Đường dẫn file mp4 đầu ra (mặc định giữ nguyên giá trị trong pipeline: /home/mta/output_highway.mp4)"
    )
    args = parser.parse_args()

    Gst.init(None)

    pipeline, nvdsosd = build_file_pipeline(rtsp_uri="unused-for-filesrc")

    src = pipeline.get_by_name("file-source")
    if not src:
        print("ERROR: Không tìm thấy element 'file-source' trong pipeline.", file=sys.stderr)
        sys.exit(1)

    in_path = os.path.abspath(args.video)
    if not os.path.exists(in_path):
        print(f"ERROR: Không tìm thấy file đầu vào: {in_path}", file=sys.stderr)
        sys.exit(1)

    src.set_property("location", in_path)

    if args.out:
        sink = pipeline.get_by_name("filesink")
        if not sink:
            print("WARNING: Không tìm thấy element 'filesink' để đặt --out. Bỏ qua.", file=sys.stderr)
        else:
            out_path = os.path.abspath(args.out)
  
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            sink.set_property("location", out_path)


    source_pts, target_pts = load_points(args.homo)
    vt = ViewTransformer(source_pts, target_pts)
    probe = SpeedProbe(vt, roi_source_points=source_pts)
    osd_sink_pad = nvdsosd.get_static_pad("sink")
    if not osd_sink_pad:
        print("ERROR: Unable to get sink pad of nvdsosd", file=sys.stderr)
        sys.exit(1)
    osd_sink_pad.add_probe(Gst.PadProbeType.BUFFER, probe.osd_sink_pad_buffer_probe, None)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("ERROR: Không thể set pipeline sang PLAYING.", file=sys.stderr)
        sys.exit(1)

    print("Pipeline is running with file:", in_path)
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
            print("EOS received.")
            loop.quit()

    bus.connect("message", on_message)

    try:
        loop.run()
    except KeyboardInterrupt:
        print("Interrupted by user.")
    finally:
        pipeline.set_state(Gst.State.NULL)
        try:
            probe.logger.close()
        except Exception:
            pass
        print("Pipeline stopped.")

if __name__ == "__main__":
    main()
