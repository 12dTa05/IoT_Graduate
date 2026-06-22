"""Master simulation coordinator — 8‑camera synchronized output with turning.

Produces 8 H.264 MP4 files (1920×1080, yuv420p, 60 fps) via ffmpeg
subprocess pipes, one ground‑truth CSV, and a ``sim_cameras.yml`` ready
for the DeepStream pipeline.
"""

import yaml
import numpy as np
import random
import subprocess
from typing import List, Dict, Set
from pathlib import Path

from .world import (
    Lane, VehicleAgent, VehiclePhase, TrafficLightController,
    ROAD_LENGTH, TURN_ROUTES, TURN_WEIGHTS,
)
from .camera import ProjectionCamera, CameraKind, build_intersection_cameras
from .renderer import SubPixelProceduralRenderer
from .plates import generate_vietnamese_plate


class IntersectionCoordinator:
    """8‑camera intersection simulation with turning + ffmpeg H.264 output."""

    def __init__(self, duration=30.0, fps=60, seed=None):
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        self.duration = duration
        self.fps      = fps
        self.dt       = 1.0 / fps

        self.light_controller = TrafficLightController()
        self.renderer         = SubPixelProceduralRenderer()

        # ── Lanes ──────────────────────────────────────────────────────
        self.lanes = {}
        for d in ("N", "S", "E", "W"):
            for idx in (0, 1):
                self.lanes[f"{d}_in_{idx}"]  = Lane(f"{d}_in_{idx}",  d, idx, True)
                self.lanes[f"{d}_out_{idx}"] = Lane(f"{d}_out_{idx}", d, idx, False)

        # ── Cameras (8 = 4 dir × 2) ───────────────────────────────────
        self.cameras = build_intersection_cameras()

        # Pre‑render the static road scene for each camera (distinct per cam)
        self._backgrounds: Dict[str, np.ndarray] = {
            cam.id: self.renderer.render_road_background(cam)
            for cam in self.cameras
        }

        # ── State ──────────────────────────────────────────────────────
        self.active_vehicles: List[VehicleAgent] = []
        self.v_counter = 0

        # Plate uniqueness
        self._used_plates: Set[str] = set()

        # CSV buffer
        self.csv_buffer: List[str] = []

    # ==================================================================
    # Main loop
    # ==================================================================
    def execute_generation(self, output_dir: str):
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        video_dir = output_path / "videos"
        video_dir.mkdir(exist_ok=True)

        # ── ffmpeg writers (H.264 / 1920×1080 / yuv420p / 60 fps) ────
        ffmpeg_procs: Dict[str, subprocess.Popen] = {}
        for cam in self.cameras:
            out_file = video_dir / f"{cam.id}.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "rawvideo",
                "-vcodec", "rawvideo",
                "-s", "1920x1080",
                "-pix_fmt", "bgr24",
                "-r", str(self.fps),
                "-i", "pipe:0",
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-r", str(self.fps),
                str(out_file),
            ]
            ffmpeg_procs[cam.id] = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )

        csv_path = output_path / "ground_truth.csv"
        csv_file = None

        try:
            csv_file = open(csv_path, "w")
            csv_file.write(
                "frame_idx,timestamp,camera_id,camera_kind,vehicle_id,plate,"
                "turn,phase,true_speed_mps,world_x,world_y,lane_id\n"
            )
            csv_file.flush()

            total_frames     = int(self.duration * self.fps)
            spawn_accumulator = 0.0

            for frame_idx in range(total_frames):
                current_time = frame_idx * self.dt

                # Spawn
                spawn_accumulator += self.dt
                burst = (current_time % 60.0) < 25.0
                spawn_interval = 0.3 if burst else 2.0
                if spawn_accumulator >= spawn_interval:
                    spawn_accumulator = 0.0
                    self._spawn_vehicle(current_time)

                # Physics + phase transitions
                self._update_vehicles(current_time)

                # Cull DONE vehicles
                self._cull_vehicles()

                # Render each camera
                for cam in self.cameras:
                    frame = self._backgrounds[cam.id].copy()

                    visible = self._vehicles_for_camera(cam)
                    sorted_vehs = sorted(
                        visible,
                        key=lambda v: self._vehicle_distance_to_camera(v, cam),
                        reverse=True,
                    )
                    for veh in sorted_vehs:
                        xyz, heading = veh.get_world_xyz_and_heading()
                        frame = self.renderer.project_and_warp(
                            frame, veh, xyz, heading, cam,
                        )

                    ffmpeg_procs[cam.id].stdin.write(frame.tobytes())

                # Ground truth
                self._log_ground_truth(frame_idx, current_time)
                if self.csv_buffer:
                    csv_file.writelines(self.csv_buffer)
                    csv_file.flush()
                    self.csv_buffer.clear()

        finally:
            # Flush remaining CSV
            if self.csv_buffer and csv_file is not None:
                csv_file.writelines(self.csv_buffer)
            if csv_file is not None:
                csv_file.close()

            # Close ffmpeg pipes (send EOF)
            for proc in ffmpeg_procs.values():
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            for proc in ffmpeg_procs.values():
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)

        # Write cameras.yml
        self._write_cameras_yml(output_path / "sim_cameras.yml")

        total_frames = int(self.duration * self.fps)
        print(f"[+] Generated {total_frames} frames across {len(self.cameras)} cameras")
        print(f"[+] Output: {video_dir}")
        print(f"[+] Config: {output_path / 'sim_cameras.yml'}")
        print(f"[+] Ground truth: {csv_path}")

    # ==================================================================
    # Per‑camera vehicle filter (isolation constraint)
    # ==================================================================
    def _vehicles_for_camera(self, cam: ProjectionCamera) -> List[VehicleAgent]:
        """Return vehicles visible to *this* camera only.

        - TRANSIT / DONE vehicles → invisible everywhere.
        - inbound_rear  → INBOUND vehicles on the same axis.
        - outbound_rear → OUTBOUND vehicles on the same axis.
        No camera sees the centre box or another road.
        """
        result = []
        for veh in self.active_vehicles:
            if veh.phase in (VehiclePhase.TRANSIT, VehiclePhase.DONE):
                continue
            if veh.lane.direction != cam.axis:
                continue
            if cam.kind == CameraKind.INBOUND_REAR and veh.phase != VehiclePhase.INBOUND:
                continue
            if cam.kind == CameraKind.OUTBOUND_REAR and veh.phase != VehiclePhase.OUTBOUND:
                continue
            result.append(veh)
        return result

    # ==================================================================
    # Spawn
    # ==================================================================
    def _spawn_vehicle(self, current_time: float):
        inbound_keys = [k for k, v in self.lanes.items() if v.is_inbound]
        lane_key = random.choice(inbound_keys)
        lane     = self.lanes[lane_key]

        self.v_counter += 1

        vtype = random.choices(
            ["car", "suv", "truck", "bus"],
            weights=[0.6, 0.2, 0.1, 0.1],
        )[0]

        plate = self._unique_plate()

        turn = random.choices(
            list(TURN_WEIGHTS.keys()),
            weights=list(TURN_WEIGHTS.values()),
        )[0]

        dest_key = TURN_ROUTES[lane.direction][turn]
        dest_lane = self.lanes[f"{dest_key}_{lane.lane_index}"]

        veh = VehicleAgent(
            vid=f"V{self.v_counter:04d}",
            plate=plate,
            vtype=vtype,
            lane=lane,
            spawn_time=current_time,
            turn=turn,
            dest_lane=dest_lane,
        )
        self.active_vehicles.append(veh)

    def _unique_plate(self) -> str:
        for _ in range(200):
            p = generate_vietnamese_plate()
            if p not in self._used_plates:
                self._used_plates.add(p)
                return p
        # Fallback: use counter suffix to guarantee uniqueness
        p = f"99A-{self.v_counter + 1:05d}"
        self._used_plates.add(p)
        return p

    # ==================================================================
    # Physics
    # ==================================================================
    def _update_vehicles(self, current_time: float):
        lane_groups: Dict[str, List[VehicleAgent]] = {}
        for veh in self.active_vehicles:
            lane_groups.setdefault(veh.lane.id, []).append(veh)

        for veh in self.active_vehicles:
            if veh.phase in (VehiclePhase.TRANSIT, VehiclePhase.DONE):
                # Still tick transit timer
                veh.update_physics(self.dt, 999.0, 0.0, "green")
                continue

            same_lane = lane_groups.get(veh.lane.id, [])
            leader = None
            min_d  = float("inf")
            for other in same_lane:
                if other is veh:
                    continue
                if other.phase != veh.phase:
                    continue
                if other.position_meters > veh.position_meters:
                    d = other.position_meters - veh.position_meters - veh.l
                    if d < min_d:
                        min_d  = d
                        leader = other

            leader_dist = min_d if leader else 999.0
            leader_vel  = leader.velocity if leader else 0.0
            light_state = self.light_controller.get_state_for_axis(
                current_time, veh.lane.direction,
            )

            veh.update_physics(self.dt, leader_dist, leader_vel, light_state)

    # ==================================================================
    # Cull
    # ==================================================================
    def _cull_vehicles(self):
        survivors = []
        for veh in self.active_vehicles:
            if veh.phase == VehiclePhase.DONE:
                continue
            if veh.phase == VehiclePhase.OUTBOUND and veh.position_meters >= ROAD_LENGTH:
                veh.phase = VehiclePhase.DONE
                continue
            survivors.append(veh)
        self.active_vehicles = survivors

    # ==================================================================
    # Depth sort
    # ==================================================================
    def _vehicle_distance_to_camera(self, vehicle, camera) -> float:
        xyz, _ = vehicle.get_world_xyz_and_heading()
        return float(np.linalg.norm(xyz - camera.position))

    # ==================================================================
    # Ground truth
    # ==================================================================
    def _log_ground_truth(self, frame_idx: int, timestamp: float):
        for cam in self.cameras:
            for veh in self._vehicles_for_camera(cam):
                xyz, heading = veh.get_world_xyz_and_heading()

                # Use 3D bounding box for visibility check
                corners_3d = SubPixelProceduralRenderer.get_3d_bounding_box_corners(
                    xyz[0], xyz[1], xyz[2],
                    veh.l, veh.w, veh.h, heading,
                )
                pixels = cam.project_3d_points(corners_3d)
                if pixels is None:
                    continue

                # Must have at least one corner in the image frame
                in_frame = np.array([
                    cam.is_point_visible(p) for p in pixels
                    if not np.any(np.isnan(p))
                ])
                if not np.any(in_frame):
                    continue

                self.csv_buffer.append(
                    f"{frame_idx},{timestamp:.3f},{cam.id},{cam.kind.value},"
                    f"{veh.id},{veh.plate},{veh.turn},{veh.phase.value},"
                    f"{veh.velocity:.2f},{xyz[0]:.2f},{xyz[1]:.2f},{veh.lane.id}\n"
                )

    # ==================================================================
    # cameras.yml generation
    # ==================================================================
    def _write_cameras_yml(self, out_path: Path):
        yaml_data = {
            "max_streams": 8,
            "tiler_mode": "auto",
            "cameras": {},
        }

        scale = 1280.0 / 1920.0   # MUX scale

        source_id = 0
        for axis in ("N", "S", "E", "W"):
            for cam in self.cameras:
                if cam.axis != axis:
                    continue

                if cam.kind == CameraKind.INBOUND_REAR:
                    # Measurement zone: from NEAR (far from centre) to FAR (near centre)
                    # on the INBOUND road.  Vehicles drive toward centre.
                    near_d = 110.0   # closer to the far‑end camera
                    far_d  = 50.0    # closer to centre
                    half_w = 3.5
                else:
                    # OUTBOUND: vehicles start near centre, drive away.
                    # Measurement zone is in the *same* world half-plane as the road.
                    near_d = 30.0
                    far_d  = 90.0
                    half_w = 3.5

                target_height = abs(far_d - near_d)
                target_width  = 2.0 * half_w

                # Build world rectangle
                if axis == "N":
                    if cam.kind == CameraKind.INBOUND_REAR:
                        w_pts = np.array([
                            [-half_w, near_d, 0.0], [half_w, near_d, 0.0],
                            [half_w, far_d, 0.0], [-half_w, far_d, 0.0],
                        ], dtype=np.float32)
                    else:
                        w_pts = np.array([
                            [-half_w, near_d, 0.0], [half_w, near_d, 0.0],
                            [half_w, far_d, 0.0], [-half_w, far_d, 0.0],
                        ], dtype=np.float32)
                elif axis == "S":
                    if cam.kind == CameraKind.INBOUND_REAR:
                        w_pts = np.array([
                            [half_w, -near_d, 0.0], [-half_w, -near_d, 0.0],
                            [-half_w, -far_d, 0.0], [half_w, -far_d, 0.0],
                        ], dtype=np.float32)
                    else:
                        w_pts = np.array([
                            [half_w, -near_d, 0.0], [-half_w, -near_d, 0.0],
                            [-half_w, -far_d, 0.0], [half_w, -far_d, 0.0],
                        ], dtype=np.float32)
                elif axis == "E":
                    if cam.kind == CameraKind.INBOUND_REAR:
                        w_pts = np.array([
                            [near_d, half_w, 0.0], [near_d, -half_w, 0.0],
                            [far_d, -half_w, 0.0], [far_d, half_w, 0.0],
                        ], dtype=np.float32)
                    else:
                        w_pts = np.array([
                            [near_d, half_w, 0.0], [near_d, -half_w, 0.0],
                            [far_d, -half_w, 0.0], [far_d, half_w, 0.0],
                        ], dtype=np.float32)
                else:  # W
                    if cam.kind == CameraKind.INBOUND_REAR:
                        w_pts = np.array([
                            [-near_d, -half_w, 0.0], [-near_d, half_w, 0.0],
                            [-far_d, half_w, 0.0], [-far_d, -half_w, 0.0],
                        ], dtype=np.float32)
                    else:
                        w_pts = np.array([
                            [-near_d, -half_w, 0.0], [-near_d, half_w, 0.0],
                            [-far_d, half_w, 0.0], [-far_d, -half_w, 0.0],
                        ], dtype=np.float32)

                p_pts = cam.project_3d_points(w_pts)
                if p_pts is None:
                    p_pts = np.array([
                        [100.0, 800.0], [1820.0, 800.0],
                        [1820.0, 200.0], [100.0, 200.0],
                    ], dtype=np.float32)

                scaled_src = (p_pts * scale).astype(int).tolist()
                scaled_roi = (p_pts * scale).astype(int).flatten().tolist()

                camera_id = cam.id
                yaml_data["cameras"][camera_id] = {
                    "camera_id": camera_id,
                    "source_id": source_id,
                    "uri": f"rtsp://192.168.212.20:8554/{camera_id}",
                    "enabled": True,
                    "name": f"Simulated {camera_id.replace('_', ' ').title()}",
                    "fps": float(self.fps),
                    "speed_limit_kmh": 50.0,
                    "homography": {
                        "source_points": scaled_src,
                        "target_width": float(target_width),
                        "target_height": float(target_height),
                    },
                    "roi_polygon": scaled_roi,
                    "output": {
                        "record": True,
                        "record_path": f"output/{camera_id}.mp4",
                    },
                }
                source_id += 1

        with open(out_path, "w") as f:
            yaml.dump(yaml_data, f, default_flow_style=False, sort_keys=False)