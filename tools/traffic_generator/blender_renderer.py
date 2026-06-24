#!/usr/bin/env python3
"""
Blender 3D renderer for the traffic simulation.

Usage inside Blender:
  blender --background LowPolyCars.blend --python blender_renderer.py -- <manifest> <cameras> <out_dir> [options]

Arguments (after '--'):
  <manifest>   Path to manifest.jsonl (frame-wise vehicle states)
  <cameras>    Path to cameras.json (camera definitions)
  <out_dir>    Output directory for MP4s

Options (passed as CLI args before the '--' or via environment):
  --engine <eevee|cycles|workbench|auto>   Render engine (default: auto → try eevee then cycles)
  --transparent-ground                     Make ground plane invisible (alpha)
  --samples <int>                          Render samples for EEVEE/Cycles (default: 16)
  --video-encoder <auto|nvenc|x264>        Passed through to ffmpeg_encoder (default: auto)

This script runs entirely in Blender's Python environment.
"""

import sys
import os
import json
import argparse
import tempfile
import subprocess
import shutil
from pathlib import Path
from math import radians

import numpy as np
import bpy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def look_at_matrix(location, target):
    """Return a 4x4 world matrix that looks from location toward target (Y-up, -Z forward in Blender)."""
    forward = np.subtract(target, location)
    forward = forward / np.linalg.norm(forward)
    up = np.array([0.0, 0.0, 1.0])  # Z-up world
    right = np.cross(forward, up)
    right = right / np.linalg.norm(right)
    up = np.cross(right, forward)
    m = np.eye(4)
    m[:3, 0] = right
    m[:3, 1] = forward
    m[:3, 2] = up
    m[:3, 3] = location
    return m

def euler_from_look_at(location, target):
    """Extract Blender Euler (XYZ) from a look-at matrix."""
    m = look_at_matrix(location, target)
    return np.array((
        np.arctan2(-m[0, 2], m[2, 2]),
        np.arctan2(m[1, 2], np.sqrt(m[0, 2]**2 + m[2, 2]**2)),
        np.arctan2(-m[1, 0], m[1, 1])
    ))

def car_model_bounds(model_name: str):
    """Return (min_z, max_z) of the model's world bounds (for ground offset)."""
    obj = bpy.data.objects.get(model_name)
    if obj is None:
        raise ValueError(f"Model {model_name} not found in blend file")
    # Use bound_box (local coordinates) then transform to world
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    zs = [c[2] for c in corners]
    return min(zs), max(zs)

def group_model(model_name: str):
    """Find all objects that belong to the given model (body + its wheels)."""
    objs = []
    body = bpy.data.objects.get(model_name)
    if body is None:
        return []
    objs.append(body)
    # Wheels have names like 'whell', 'whell.001', etc. They belong to car2 if they are in its collection or nearby
    # Simple heuristic: any wheel whose bounding box center is close to body and has small Z range
    body_min_z, body_max_z = car_model_bounds(model_name)
    for o in bpy.data.objects:
        if o.name.startswith("whell"):
            # Check proximity: wheel center near body XY and Z within typical wheel height
            ow = o.matrix_world.to_translation()
            body_center = body.matrix_world.to_translation()
            if abs(ow[0] - body_center[0]) < 2.0 and abs(ow[1] - body_center[1]) < 2.0:
                objs.append(o)
    return objs

def make_plate_image(plate_text: str, out_path: Path):
    """Render a Vietnamese plate PNG using OpenCV-like drawing via Blender's bgl (or pillow if available)."""
    # Try using Pillow if available in Blender's Python (unlikely). Fallback: use a simple solid color placeholder.
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGBA", (256, 128), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 64)
        except Exception:
            font = ImageFont.load_default()
        w, h = draw.textsize(plate_text, font=font)
        draw.text(((256 - w) // 2, (128 - h) // 2), plate_text, fill=(0, 0, 0, 255), font=font)
        img.save(out_path)
        return
    except Exception:
        pass
    # Minimal fallback: 1x1 transparent PNG (will be invisible). Not ideal but prevents crash.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Parse args after '--'
    argv = sys.argv
    if "--" in argv:
        idx = argv.index("--")
        args = argv[idx + 1:]
    else:
        args = argv[1:]

    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("cameras", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--engine", default="auto", choices=["auto", "eevee", "cycles", "workbench"])
    parser.add_argument("--transparent-ground", action="store_true")
    parser.add_argument("--samples", type=int, default=16)
    parser.add_argument("--video-encoder", default="auto", choices=["auto", "nvenc", "x264"])
    opt = parser.parse_args(args)

    opt.out_dir.mkdir(parents=True, exist_ok=True)

    # Load manifest
    frames = []
    with open(opt.manifest, "r") as f:
        for line in f:
            if line.strip():
                frames.append(json.loads(line))
    total_frames = len(frames)

    # Load cameras
    with open(opt.cameras, "r") as f:
        cameras_data = json.load(f)["cameras"]  # dict by camera_id

    # Determine render engine
    engine = opt.engine.upper()
    if engine == "AUTO":
        # Prefer EEVEE if available (headless OK on this machine), else Cycles
        if "BLENDER_EEVEE" in bpy.context.preferences.addons.get('blender', {}):
            engine = "BLENDER_EEVEE"
        else:
            engine = "CYCLES"
    bpy.context.scene.render.engine = engine

    # Configure engine
    if engine == "CYCLES":
        cprefs = bpy.context.preferences.addons['cycles'].preferences
        cprefs.compute_device_type = 'OPTIX'
        cprefs.get_devices()
        for d in cprefs.devices:
            d.use = (d.type == 'OPTIX')
        bpy.context.scene.cycles.device = 'GPU'
        bpy.context.scene.cycles.samples = opt.samples
        bpy.context.scene.cycles.use_denoising = False
    else:  # EEVEE or WORKBENCH
        bpy.context.scene.render.use_compositing = False

    # Output settings: 1920x1080, 25fps (or from cameras? we'll set from first camera)
    # All cameras have same resolution; use the first
    first_cam_id = next(iter(cameras_data))
    cam_info = cameras_data[first_cam_id]
    width, height = 1920, 1080  # fixed as per requirement
    bpy.context.scene.render.resolution_x = width
    bpy.context.scene.render.resolution_y = height
    bpy.context.scene.render.resolution_percentage = 100
    bpy.context.scene.render.image_settings.file_format = 'PNG'  # temp, then encode to H.264

    # Frame rate
    bpy.context.scene.render.fps = opt.fps if hasattr(opt, 'fps') else 25
    bpy.context.scene.render.fps = 25  # hard per spec

    # Create ground plane (optional)
    ground_obj = None
    if not opt.transparent_ground:
        bpy.ops.mesh.primitive_plane_add(size=500.0, location=(0, 0, 0))
        ground_obj = bpy.context.active_object
        ground_obj.name = "Ground"
        # Asphalt material
        mat = bpy.data.materials.new(name="Asphalt")
        mat.diffuse_color = (0.1, 0.1, 0.1, 1.0)
        ground_obj.data.materials.append(mat)

    # Build car templates
    templates = {}
    plate_tex_dir = opt.out_dir / "plates"
    plate_tex_dir.mkdir(exist_ok=True)

    for model in ["car2", "car9"]:
        group = group_model(model)
        if not group:
            print(f"[WARN] Model {model} not found in blend file — skipping")
            continue
        # Compute ground offset: we want the lowest point of the group (wheels) at z=0
        min_z, max_z = car_model_bounds(model)
        z_offset = -min_z  # shift up so min_z goes to 0
        # Create an empty parent to hold the template; hide original
        template_empty = bpy.data.objects.new(f"Template_{model}", None)
        bpy.context.scene.collection.objects.link(template_empty)
        for obj in group:
            # Parent to template empty, preserving world transform
            matrix_world = obj.matrix_world.copy()
            obj.parent = template_empty
            obj.matrix_world = matrix_world
        # Move template empty so that its children's lowest point sits at z=0
        template_empty.location.z = z_offset
        templates[model] = {
            "empty": template_empty,
            "objects": group,
        }

    # Prepare plate images (generate PNGs for unique plates)
    # We'll scan manifest to collect unique plates
    unique_plates = set()
    for frame in frames:
        for veh in frame["vehicles"]:
            unique_plates.add(veh["plate"])
    plate_images = {}
    for plate in unique_plates:
        png_path = plate_tex_dir / f"{plate}.png"
        if not png_path.exists():
            make_plate_image(plate, png_path)
        plate_images[plate] = str(png_path)

    # Create 8 cameras in Blender
    blender_cams = {}
    for cam_id, info in cameras_data.items():
        cam_data = bpy.data.cameras.new(name=cam_id)
        cam_obj = bpy.data.objects.new(cam_id, cam_data)
        bpy.context.scene.collection.objects.link(cam_obj)
        # Settings
        cam_data.sensor_fit = 'HORIZONTAL'
        cam_data.lens_unit = 'FOV'
        cam_data.angle = radians(float(info.get("fov", 40.0)))
        cam_data.clip_start = 0.1
        cam_data.clip_end = 1000.0
        # Position
        pos = info["position"]  # [x, y, z]
        target = info["look_at"]  # [x, y, z]
        cam_obj.location = (pos[0], pos[1], pos[2])
        # Orientation: look at target
        rot = euler_from_look_at(np.array(pos), np.array(target))
        cam_obj.rotation_euler = (rot[0], rot[1], rot[2])
        blender_cams[cam_id] = cam_obj

    # Lighting: simple sun + ambient for even illumination
    bpy.ops.object.light_add(type='SUN', location=(0, 0, 50))
    sun = bpy.context.active_object
    sun.data.energy = 2.0
    bpy.context.scene.world.use_sky = False
    bpy.context.scene.world.color = (0.05, 0.05, 0.05)

    # Prepare ffmpeg processes per camera
    from tools.traffic_generator.ffmpeg_encoder import build_ffmpeg_cmd
    import tempfile as py_tempfile
    import logging
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

    ffmpeg_procs = {}
    ffmpeg_stderr = {}
    dead_cameras = set()

    video_dir = opt.out_dir / "videos"
    video_dir.mkdir(exist_ok=True)

    for cam_id, cam_obj in blender_cams.items():
        out_file = video_dir / f"{cam_id}.mp4"
        cmd = build_ffmpeg_cmd(
            out_file=str(out_file),
            width=width,
            height=height,
            fps=opt.fps if hasattr(opt, 'fps') else 25,
            video_encoder=opt.video_encoder,
        )
        stderr_temp = py_tempfile.TemporaryFile(mode="w+b")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=stderr_temp)
        except FileNotFoundError as e:
            stderr_temp.close()
            raise RuntimeError(f"Failed to start ffmpeg for {cam_id}: {e}") from e
        ffmpeg_procs[cam_id] = proc
        ffmpeg_stderr[cam_id] = stderr_temp
        if proc.poll() is not None:
            exit_code = proc.returncode
            stderr_temp.seek(0)
            err_output = stderr_temp.read().decode(errors="replace")
            stderr_temp.close()
            raise RuntimeError(f"ffmpeg for {cam_id} exited immediately: {err_output}")

    # Helper to render a frame and return raw BGR bytes
    def render_frame(cam_obj):
        bpy.context.scene.camera = cam_obj
        # Render to temporary PNG then read
        with py_tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        bpy.context.scene.render.filepath = tmp_path
        try:
            bpy.ops.render.render(write_still=True)
            # Read image as RGBA
            import cv2
            img = cv2.imread(tmp_path, cv2.IMREAD_UNCHANGED)
            if img is None:
                return None
            # Convert to BGR (if RGBA, drop alpha)
            if img.shape[2] == 4:
                bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
            else:
                bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            return bgr.tobytes()
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # Main per-frame loop
    for frame_idx, frame_entry in enumerate(frames):
        # Spawn/destroy vehicle instances to match active set
        # We'll maintain a mapping from vehicle id to Blender object group
        active_vehs = {veh["id"]: veh for veh in frame_entry["vehicles"]}
        # Remove missing
        for obj_name in list(bpy.data.objects):
            if obj_name.startswith("VEH_"):
                vid = obj_name[4:]
                if vid not in active_vehs:
                    bpy.data.objects.remove(bpy.data.objects[obj_name], do_unlink=True)
        # Add/update
        for veh in frame_entry["vehicles"]:
            vid = veh["id"]
            obj_name = f"VEH_{vid}"
            if obj_name not in bpy.data.objects:
                # Instance from template
                template = templates.get(veh["model"])
                if template is None:
                    continue
                # Create linked duplicate of the template empty
                new_empty = bpy.data.objects.new(obj_name, None)
                new_empty.matrix_world = template["empty"].matrix_world.copy()
                new_empty.instance_type = 'COLLECTION'
                # Link children by making instances of each object
                for child in template["objects"]:
                    inst = child.copy()
                    inst.data = child.data  # share mesh data
                    inst.parent = new_empty
                    bpy.context.scene.collection.objects.link(inst)
                bpy.context.scene.collection.objects.link(new_empty)
                # Apply plate texture to rear window/plate area (we'll create a plate object)
                # For now, skip plate 3D mesh; later we'll add a small plate quad
                # We'll create a plate object as a child of the empty
                plate_tex_path = plate_images.get(veh["plate"])
                if plate_tex_path:
                    # Create a plane at rear
                    bpy.ops.mesh.primitive_plane_add(size=0.5, location=(0,0,0))
                    plate_ob = bpy.context.active_object
                    plate_ob.name = f"Plate_{vid}"
                    plate_ob.parent = new_empty
                    plate_ob.location = (-0.5, 0, 0.5)  # rear offset (local +X forward, so -X is rear)
                    plate_ob.rotation_euler = (0, 0, 0)
                    # Material with image texture (emission)
                    mat = bpy.data.materials.new(name=f"PlateMat_{vid}")
                    mat.use_nodes = True
                    nodes = mat.node_tree.nodes
                    nodes.clear()
                    output = nodes.new(type='ShaderNodeOutputMaterial')
                    emission = nodes.new(type='ShaderNodeEmission')
                    tex = nodes.new(type='ShaderNodeTexImage')
                    img = bpy.data.images.load(str(plate_tex_path))
                    tex.image = img
                    # Connect: tex.color -> emission.color
                    mat.node_tree.links.new(tex.outputs['Color'], emission.inputs['Color'])
                    mat.node_tree.links.new(emission.outputs['Emission'], output.inputs['Surface'])
                    plate_ob.data.materials.append(mat)
            else:
                new_empty = bpy.data.objects[obj_name]

            # Update transform
            x, y = veh["x"], veh["y"]
            heading = veh["heading"]
            # Model-specific z offset: we'll store it on the template object's custom property
            # (computed earlier as -min_z). For simplicity, we assume all templates have same ground offset.
            z_offset = templates[veh["model"]]["empty"].location.z
            new_empty.location = (x, y, z_offset)
            new_empty.rotation_euler = (0, 0, heading)

        # Render each camera
        for cam_id, cam_obj in blender_cams.items():
            if cam_id in dead_cameras:
                continue
            bgr_bytes = render_frame(cam_obj)
            if bgr_bytes is None:
                logging.error(f"Render returned None for {cam_id} frame {frame_idx}")
                continue
            try:
                ffmpeg_procs[cam_id].stdin.write(bgr_bytes)
            except (BrokenPipeError, ValueError):
                dead_cameras.add(cam_id)
                proc = ffmpeg_procs.get(cam_id)
                exit_code = proc.returncode if proc else "unknown"
                stderr_temp = ffmpeg_stderr.get(cam_id)
                if stderr_temp:
                    stderr_temp.seek(0)
                    err = stderr_temp.read().decode(errors="replace")[-500:]
                else:
                    err = "no stderr"
                logging.error(f"[blender_renderer] ffmpeg {cam_id} died (exit={exit_code}). Stderr: {err}")

    # Cleanup: close ffmpeg, wait
    for cam_id, proc in ffmpeg_procs.items():
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.wait(timeout=30)
        except Exception as e:
            proc.kill()
            proc.wait(timeout=10)
        stderr_temp = ffmpeg_stderr.get(cam_id)
        if stderr_temp:
            stderr_temp.close()

    print(f"[+] Blender render complete: {total_frames} frames, cameras: {len(blender_cams)}")
    print(f"[+] Videos: {video_dir}")

if __name__ == "__main__":
    main()
