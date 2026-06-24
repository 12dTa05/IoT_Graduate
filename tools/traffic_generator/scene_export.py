"""Phase 1: Physics-only simulation → manifest + ground truth + cameras.yml.

Runs the IDM/turning simulation without any rendering and writes:
  - manifest.jsonl: per-frame vehicle states (id, model, x, y, heading, plate)
  - ground_truth.csv: same schema as the 2D pipeline
  - sim_cameras.yml: camera configs (unchanged)
Allows a separate Blender step to consume the manifest for 3D rendering.
"""

import json
import random
import numpy as np
from pathlib import Path
from typing import List, Dict

from .world import (
    Lane, VehicleAgent, VehiclePhase, TrafficLightController,
    ROAD_LENGTH, TURN_ROUTES, TURN_WEIGHTS,
)
from .camera import ProjectionCamera, CameraKind, build_intersection_cameras
from .plates import generate_vietnamese_plate


class SceneExporter:
    """Replicates the simulation core of IntersectionCoordinator without rendering."""

    def __init__(self, duration=30.0, fps=25, seed=None, traffic_scale=1.0):
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        self.duration = duration
        self.fps = fps
        self.dt = 1.0 / fps
        self._traffic_scale = max(0.01, float(traffic_scale))

        self.light_controller = TrafficLightController()
        self.cameras = build_intersection_cameras()

        # Lanes
        self.lanes = {}
        for d in ("N", "S", "E", "W"):
            for idx in (0, 1):
                self.lanes[f"{d}_in_{idx}"] = Lane(f"{d}_in_{idx}", d, idx, True)
                self.lanes[f"{d}_out_{idx}"] = Lane(f"{d}_out_{idx}", d, idx, False)

        # State
        self.active_vehicles: List[VehicleAgent] = []
        self.v_counter = 0
        self._used_plates = set()

    # ==================================================================
    # Spawn
    # ==================================================================
    def _spawn_vehicle(self, current_time: float):
        inbound_keys = [k for k, v in self.lanes.items() if v.is_inbound]
        lane_key = random.choice(inbound_keys)
        lane = self.lanes[lane_key]

        self.v_counter += 1

        # Only "car" type exists in 3D mode; pick a 3D model randomly
        vtype = "car"
        model = random.choice(["car2", "car9"])

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
        # Attach the 3D model choice for the renderer
        veh.model = model  # type: ignore
        self.active_vehicles.append(veh)

    def _unique_plate(self) -> str:
        for _ in range(200):
            p = generate_vietnamese_plate()
            if p not in self._used_plates:
                self._used_plates.add(p)
                return p
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
                veh.update_physics(self.dt, 999.0, 0.0, "green")
                continue

            same_lane = lane_groups.get(veh.lane.id, [])
            leader = None
            min_d = float("inf")
            for other in same_lane:
                if other is veh:
                    continue
                if other.phase != veh.phase:
                    continue
                if other.position_meters > veh.position_meters:
                    d = other.position_meters - veh.position_meters - veh.l
                    if d < min_d:
                        min_d = d
                        leader = other

            leader_dist = min_d if leader else 999.0
            leader_vel = leader.velocity if leader else 0.0
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
    # Ground truth (same as coordinator)
    # ==================================================================
    def _log_ground_truth(self, frame_idx: int, timestamp: float, csv_f):
        from .renderer import SubPixelProceduralRenderer
        for cam in self.cameras:
            for veh in self._vehicles_for_camera(cam):
                xyz, heading = veh.get_world_xyz_and_heading()
                corners_3d = SubPixelProceduralRenderer.get_3d_bounding_box_corners(
                    xyz[0], xyz[1], xyz[2],
                    veh.l, veh.w, veh.h, heading,
                )
                pixels = cam.project_3d_points(corners_3d)
                if pixels is None:
                    continue
                in_frame = np.array([
                    cam.is_point_visible(p) for p in pixels
                    if not np.any(np.isnan(p))
                ])
                if not np.any(in_frame):
                    continue
                csv_f.write(
                    f"{frame_idx},{timestamp:.3f},{cam.id},{cam.kind.value},"
                    f"{veh.id},{veh.plate},{veh.turn},{veh.phase.value},"
                    f"{veh.velocity:.2f},{xyz[0]:.2f},{xyz[1]:.2f},{veh.lane.id}\n"
                )

    def _vehicles_for_camera(self, cam: ProjectionCamera) -> List[VehicleAgent]:
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
    # Main export
    # ==================================================================
    def execute_export(self, output_dir: str):
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Open outputs
        manifest_path = output_path / "manifest.jsonl"
        manifest_f = open(manifest_path, "w")
        csv_path = output_path / "ground_truth.csv"
        csv_f = open(csv_path, "w")
        csv_f.write(
            "frame_idx,timestamp,camera_id,camera_kind,vehicle_id,plate,"
            "turn,phase,true_speed_mps,world_x,world_y,lane_id\n"
        )

        # Write cameras.yml using same logic as coordinator
        self._write_cameras_yml(output_path / "sim_cameras.yml")

        # Write cameras.json (for Blender)
        cameras_json_path = output_path / "cameras.json"
        cameras_dict = {}
        for cam in self.cameras:
            cameras_dict[cam.id] = {
                "position": [float(cam.position[0]), float(cam.position[1]), float(cam.position[2])],
                "look_at": [float(cam.look_at[0]), float(cam.look_at[1]), float(cam.look_at[2])],
                "fov": float(cam.fov_degrees),
                "resolution": list(cam.resolution),  # [width, height]
                "kind": cam.kind.value,
                "axis": cam.axis,
            }
        with open(cameras_json_path, "w") as f:
            json.dump({"cameras": cameras_dict}, f, indent=2)

        total_frames = int(self.duration * self.fps)
        spawn_accumulator = 0.0

        for frame_idx in range(total_frames):
            current_time = frame_idx * self.dt

            # Spawn
            spawn_accumulator += self.dt
            burst = (current_time % 60.0) < 25.0
            base_interval = 0.3 if burst else 2.0
            spawn_interval = base_interval / self._traffic_scale
            if spawn_accumulator >= spawn_interval:
                spawn_accumulator -= spawn_interval
                self._spawn_vehicle(current_time)

            # Physics
            self._update_vehicles(current_time)

            # Cull
            self._cull_vehicles()

            # Write manifest entry for this frame (all active vehicles)
            frame_vehs = []
            for veh in self.active_vehicles:
                xyz, heading = veh.get_world_xyz_and_heading()
                frame_vehs.append({
                    "id": veh.id,
                    "model": getattr(veh, "model", "car2"),
                    "x": float(xyz[0]),
                    "y": float(xyz[1]),
                    "heading": float(heading),
                    "plate": veh.plate,
                    "vtype": veh.type,
                    "turn": veh.turn,
                    "phase": veh.phase.value,
                    "velocity": float(veh.velocity),
                    "lane": veh.lane.id,
                })
            manifest_entry = {
                "frame": frame_idx,
                "t": round(current_time, 6),
                "vehicles": frame_vehs,
            }
            manifest_f.write(json.dumps(manifest_entry) + "\n")

            # Ground truth
            self._log_ground_truth(frame_idx, current_time, csv_f)

        manifest_f.close()
        csv_f.close()

        print(f"[+] Exported {total_frames} frames")
        print(f"[+] Manifest: {manifest_path}")
        print(f"[+] Ground truth: {csv_path}")
        print(f"[+] Config: {output_path / 'sim_cameras.yml'}")

    # ==================================================================
    # cameras.yml generation (copied from coordinator)
    # ==================================================================
    def _write_cameras_yml(self, out_path: Path):
        import yaml
        from .camera import CameraKind

        yaml_data = {
            "max_streams": 8,
            "tiler_mode": "auto",
            "cameras": {},
        }

        scale = 1280.0 / 1920.0  # MUX scale

        source_id = 0
        for axis in ("N", "S", "E", "W"):
            for cam in self.cameras:
                if cam.axis != axis:
                    continue

                if cam.kind == CameraKind.INBOUND_REAR:
                    near_d = 110.0
                    far_d = 50.0
                    half_w = 3.5
                else:
                    near_d = 30.0
                    far_d = 90.0
                    half_w = 3.5

                target_height = abs(far_d - near_d)
                target_width = 2.0 * half_w

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
