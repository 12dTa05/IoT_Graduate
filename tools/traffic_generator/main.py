#!/usr/bin/env python3
"""
Pure Python Multi‑Camera Traffic Simulation Data Engine
Generates synchronized 8‑camera H.264 MP4 files with ground‑truth CSV.
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.traffic_generator.coordinator import IntersectionCoordinator


def main():
    parser = argparse.ArgumentParser(
        description="Traffic video generator for Edge testing (8 cameras, turning model)"
    )
    parser.add_argument(
        "--duration", type=float, default=30.0,
        help="Video duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--fps", type=int, default=60,
        help="Output frame rate (default: 60)",
    )
    parser.add_argument(
        "--output-dir", type=str, default="Camera/videos",
        help="Output directory (default: Camera/videos)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    parser.add_argument(
        "--traffic-scale", type=float, default=1.0,
        help="Traffic volume multiplier (1.0 = baseline). "
             "Example: 1.5 = 50%% more vehicles, intervals divided by 1.5.",
    )
    parser.add_argument(
        "--video-encoder", type=str, default="auto",
        choices=["auto", "nvenc", "x264"],
        help="Video encoder: auto (NVENC if available, else libx264), "
             "nvenc (force GPU), or x264 (force CPU). Default: auto",
    )

    args = parser.parse_args()

    output_path = Path(args.output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print(f"[+] Starting simulation: {args.duration}s @ {args.fps}fps")
    print(f"[+] Output directory: {output_path}")
    print(f"[+] 8 cameras (N/S/E/W × inbound_rear + outbound_rear)")
    print(f"[+] Traffic scale: {args.traffic_scale}x")
    print(f"[+] Video encoder: {args.video_encoder}")

    coordinator = IntersectionCoordinator(
        duration=args.duration,
        fps=args.fps,
        seed=args.seed,
        video_encoder=args.video_encoder,
        traffic_scale=args.traffic_scale,
    )

    coordinator.execute_generation(str(output_path))

    print("[+] Generation complete!")
    print(f"[+] Next steps:")
    print(f"    1. Copy {output_path / 'sim_cameras.yml'} to Edge/configs/cameras.yml")
    print(f"    2. Start MediaMTX: docker compose -f Camera/docker-compose.yml up -d")
    print(f"    3. Run Edge pipeline: python3 Edge/main.py --mode rtsp_push")


if __name__ == "__main__":
    main()