#!/usr/bin/env python3
"""
Orchestrator for 3D render pipeline.

Steps:
  1. Run physics simulation (scene_export) to produce manifest + cameras.json + ground truth + sim_cameras.yml
  2. Launch Blender (headless) to render the 3D videos from the manifest

CLI args:
  --duration, --fps 25 (default 30s, 25fps), --seed, --traffic-scale, --video-encoder, --engine, --transparent-ground, --samples, --output-dir
"""

import argparse
import subprocess
import sys
import shutil
from pathlib import Path

# Add repo root to sys.path so we can import scene_exporter
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.traffic_generator.scene_export import SceneExporter


def find_blender():
    # Try common locations
    for candidate in ["blender", "/snap/bin/blender"]:
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError("Blender not found in PATH. Install Blender or set BLENDER_PATH env var.")


def main():
    parser = argparse.ArgumentParser(description="3D traffic video generation via Blender")
    parser.add_argument("--duration", type=float, default=30.0, help="Video duration in seconds")
    parser.add_argument("--fps", type=int, default=25, help="Output frame rate")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--traffic-scale", type=float, default=1.0, help="Traffic volume multiplier")
    parser.add_argument("--video-encoder", default="auto", choices=["auto", "nvenc", "x264"],
                        help="ffmpeg video encoder")
    parser.add_argument("--engine", default="auto", choices=["auto", "eevee", "cycles", "workbench"],
                        help="Blender render engine")
    parser.add_argument("--transparent-ground", action="store_true", help="Make ground plane transparent")
    parser.add_argument("--samples", type=int, default=16, help="Render samples (EEVEE/Cycles)")
    parser.add_argument("--output-dir", type=str, default="output/3d_render",
                        help="Output directory")
    parser.add_argument("--blender-path", type=str, default=None,
                        help="Path to blender executable (overrides detection)")

    args = parser.parse_args()

    output_path = Path(args.output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print(f"[+] Starting 3D pipeline: {args.duration}s @ {args.fps}fps, traffic_scale={args.traffic_scale}")
    print(f"[+] Output directory: {output_path}")

    # Phase 1: Export simulation manifest + ground truth + cameras config
    print("[+] Phase 1: Running simulation export...")
    exporter = SceneExporter(
        duration=args.duration,
        fps=args.fps,
        seed=args.seed,
        traffic_scale=args.traffic_scale,
    )
    exporter.execute_export(str(output_path))

    # Phase 2: Launch Blender
    print("[+] Phase 2: Launching Blender render...")
    blender_exe = args.blender_path or find_blender()
    blend_file = _REPO_ROOT / "tools" / "models" / "car_blender" / "LowPolyCars.blend"
    if not blend_file.exists():
        raise FileNotFoundError(f"Blend file not found: {blend_file}")

    blender_script = _REPO_ROOT / "tools" / "traffic_generator" / "blender_renderer.py"
    manifest = output_path / "manifest.jsonl"
    cameras = output_path / "cameras.json"

    blender_cmd = [
        blender_exe,
        "--background",
        str(blend_file),
        "--python", str(blender_script),
        "--",
        str(manifest),
        str(cameras),
        str(output_path),
        "--engine", args.engine,
        "--samples", str(args.samples),
        "--video-encoder", args.video_encoder,
    ]
    if args.transparent_ground:
        blender_cmd.append("--transparent-ground")

    print("[+] Running:", " ".join(blender_cmd))
    proc = subprocess.Popen(blender_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # Stream Blender output live
    for line in proc.stdout:
        print(line, end="")
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"Blender render failed with exit code {proc.returncode}")

    print("[+] 3D pipeline complete!")
    print(f"[+] Videos: {output_path / 'videos'}")
    print(f"[+] Ground truth: {output_path / 'ground_truth.csv'}")
    print(f"[+] Config: {output_path / 'sim_cameras.yml'}")


if __name__ == "__main__":
    main()
