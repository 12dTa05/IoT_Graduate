#!/usr/bin/env python3
"""
DeepStream Traffic Monitor — Python Backend Entry Point.
"""
import argparse
import sys
from pathlib import Path

# Ensure Edge/ is importable when running as python3 main.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

from speedflow_python.settings import MUX_WIDTH, MUX_HEIGHT


def main():
    parser = argparse.ArgumentParser(
        description="DeepStream Traffic Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 main.py --source rtsp://192.168.1.155:8554/cam1 --mode display
  python3 main.py --mode display            # uses cameras.yml sources
  python3 main.py --mode webrtc
""",
    )

    parser.add_argument(
        "--source",
        default="",
        help="Input source (RTSP URL or file path). Overrides cameras.yml if given.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["display", "file", "webrtc"],
        help="Output mode: display (screen), file (MP4), or webrtc (browser stream)",
    )
    parser.add_argument("--width",  type=int, default=MUX_WIDTH,  help="Streammux width")
    parser.add_argument("--height", type=int, default=MUX_HEIGHT, help="Streammux height")
    parser.add_argument("--output", help="Output file path (file mode only)")

    args = parser.parse_args()

    if args.mode == "file" and not args.output:
        parser.error("--output is required when --mode is 'file'")

    from speedflow_python.run_python import run_python_mode
    run_python_mode(args)


if __name__ == "__main__":
    main()
