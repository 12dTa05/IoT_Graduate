"""Master simulation coordinator - 4-camera synchronized output."""

import cv2
import yaml
import numpy as np
import random
from typing import List, Dict
from pathlib import Path

from .world import Lane, VehicleAgent, TrafficLightController
from .camera import ProjectionCamera
from .renderer import SubPixelProceduralRenderer
from .plates import generate_vietnamese_plate

class IntersectionCoordinator:
    def __init__(self, duration=30.0, fps=60, seed=None):
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)
            
        self.duration = duration
        self.fps = fps
        self.dt = 1.0 / fps
        self.light_controller = TrafficLightController()
        self.renderer = SubPixelProceduralRenderer()
        
        # Define all lanes (8 inbound for spawning, 8 outbound for completeness)
        self.lanes = {
            # Inbound (entering intersection)
            'N_in_0': Lane('N_in_0', 'N', 0, True),
            'N_in_1': Lane('N_in_1', 'N', 1, True),
            'S_in_0': Lane('S_in_0', 'S', 0, True),
            'S_in_1': Lane('S_in_1', 'S', 1, True),
            'E_in_0': Lane('E_in_0', 'E', 0, True),
            'E_in_1': Lane('E_in_1', 'E', 1, True),
            'W_in_0': Lane('W_in_0', 'W', 0, True),
            'W_in_1': Lane('W_in_1', 'W', 1, True),
            # Outbound (exiting) - not used for spawning
            'N_out_0': Lane('N_out_0', 'N', 0, False),
            'N_out_1': Lane('N_out_1', 'N', 1, False),
            'S_out_0': Lane('S_out_0', 'S', 0, False),
            'S_out_1': Lane('S_out_1', 'S', 1, False),
            'E_out_0': Lane('E_out_0', 'E', 0, False),
            'E_out_1': Lane('E_out_1', 'E', 1, False),
            'W_out_0': Lane('W_out_0', 'W', 0, False),
            'W_out_1': Lane('W_out_1', 'W', 1, False),
        }
        
        # Define 4 cameras - one per approach.
        # Each camera sits just inside the intersection (12m from center), 8.5m high,
        # and looks OUTWARD along its approach toward the far field (40m) where
        # vehicles enter. This keeps the whole inbound lane (60m -> ~28m) visible
        # and makes vehicles grow naturally as they approach, like a real
        # pole-mounted traffic camera.
        self.cameras = [
            ProjectionCamera("cam_north", "N", [0.0, 12.0, 8.5], [0.0, 40.0, 1.0], fov=60.0),
            ProjectionCamera("cam_south", "S", [0.0, -12.0, 8.5], [0.0, -40.0, 1.0], fov=60.0),
            ProjectionCamera("cam_east", "E", [12.0, 0.0, 8.5], [40.0, 0.0, 1.0], fov=60.0),
            ProjectionCamera("cam_west", "W", [-12.0, 0.0, 8.5], [-40.0, 0.0, 1.0], fov=60.0),
        ]
        
        self.active_vehicles: List[VehicleAgent] = []
        self.v_counter = 0
        
        # CSV logging buffer
        self.csv_buffer = []
        self.csv_flush_interval = 60  # flush every 60 frames
        
    def execute_generation(self, output_dir: str):
        """Main simulation and video generation loop."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        video_dir = output_path / "videos"
        video_dir.mkdir(exist_ok=True)
        
        # Initialize video writers
        writers = {}
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        for cam in self.cameras:
            writers[cam.id] = cv2.VideoWriter(
                str(video_dir / f"{cam.id}.mp4"),
                fourcc, self.fps, (1920, 1080)
            )
            
        # Open CSV file for ground truth logging
        csv_path = output_path / "ground_truth.csv"
        csv_file = open(csv_path, 'w')
        csv_file.write("frame_idx,timestamp,camera_id,vehicle_id,plate,true_speed_mps,world_x,world_y,lane_id\n")
        
        total_frames = int(self.duration * self.fps)
        spawn_accumulator = 0.0
        
        for frame_idx in range(total_frames):
            current_time = frame_idx * self.dt
            
            # Spawn logic: burst pattern (25s heavy, 35s light in 60s cycle)
            spawn_accumulator += self.dt
            burst = (current_time % 60.0) < 25.0
            spawn_interval = 0.3 if burst else 2.0  # high/low spawn rate
            if spawn_accumulator >= spawn_interval:
                spawn_accumulator = 0.0
                self._spawn_vehicle(current_time)
                
            # Update all vehicles with physics
            self._update_vehicles(current_time)
            
            # Cull vehicles that have moved beyond simulation bounds
            self._cull_vehicles()
            
            # Render all cameras
            frame_buffers = {}
            for cam in self.cameras:
                # Start with dark asphalt background
                frame = np.ones((1080, 1920, 3), dtype=np.uint8) * 45
                
                # Sort vehicles by distance to camera (far to near for painter's algorithm)
                sorted_vehs = sorted(
                    self.active_vehicles,
                    key=lambda v: self._vehicle_distance_to_camera(v, cam),
                    reverse=True
                )
                
                for veh in sorted_vehs:
                    xyz, heading = veh.get_world_xyz_and_heading()
                    frame = self.renderer.project_and_warp(frame, veh, xyz, heading, cam)
                    
                writers[cam.id].write(frame)
                frame_buffers[cam.id] = frame
                
            # Log ground truth for this frame (buffered)
            self._log_ground_truth(frame_idx, current_time, frame_buffers)
            if frame_idx % self.csv_flush_interval == 0:
                csv_file.writelines(self.csv_buffer)
                self.csv_buffer.clear()
                
        # Final CSV flush
        if self.csv_buffer:
            csv_file.writelines(self.csv_buffer)
            
        # Cleanup
        for w in writers.values():
            w.release()
        csv_file.close()
        
        # Generate cameras.yml configuration
        self._write_cameras_yml(output_path / "sim_cameras.yml")
        
        print(f"[+] Generated {total_frames} frames across {len(self.cameras)} cameras")
        print(f"[+] Output: {video_dir}")
        print(f"[+] Config: {output_path / 'sim_cameras.yml'}")
        print(f"[+] Ground truth: {csv_path}")
        
    def _spawn_vehicle(self, current_time: float):
        """Spawn a new vehicle on a random inbound lane."""
        inbound_lanes = [k for k, v in self.lanes.items() if v.is_inbound]
        lane_key = random.choice(inbound_lanes)
        lane = self.lanes[lane_key]
        
        self.v_counter += 1
        vtype = random.choices(
            ['car', 'suv', 'truck', 'bus'],
            weights=[0.6, 0.2, 0.1, 0.1]
        )[0]
        
        veh = VehicleAgent(
            vid=f"V{self.v_counter:04d}",
            plate=generate_vietnamese_plate(),
            vtype=vtype,
            lane=lane,
            spawn_time=current_time
        )
        self.active_vehicles.append(veh)
        
    def _update_vehicles(self, current_time: float):
        """Update physics for all vehicles using IDM."""
        # Group vehicles by lane for efficient leader lookup
        lane_groups = {}
        for veh in self.active_vehicles:
            lane_groups.setdefault(veh.lane.id, []).append(veh)
            
        for veh in self.active_vehicles:
            # Find closest leading vehicle in same lane
            same_lane = lane_groups.get(veh.lane.id, [])
            leader = None
            min_dist = float('inf')
            for other in same_lane:
                if other != veh and other.position_meters > veh.position_meters:
                    dist = other.position_meters - veh.position_meters - veh.l
                    if dist < min_dist:
                        min_dist = dist
                        leader = other
                        
            leader_dist = min_dist if leader else 999.0
            leader_vel = leader.velocity if leader else 0.0
            
            # Get traffic light state for this lane's direction
            light_state = self.light_controller.get_state_for_axis(current_time, veh.lane.direction)
            
            # Update physics
            veh.update_physics(self.dt, leader_dist, leader_vel, light_state)
            
    def _cull_vehicles(self):
        """Remove vehicles that have moved beyond the simulation bounds."""
        # Keep vehicles that haven't gone too far (position < 120m)
        self.active_vehicles = [v for v in self.active_vehicles if v.position_meters < 120.0]
        
    def _vehicle_distance_to_camera(self, vehicle, camera) -> float:
        """Compute Euclidean distance from vehicle to camera (for depth sorting)."""
        xyz, _ = vehicle.get_world_xyz_and_heading()
        return np.linalg.norm(xyz - camera.position)
        
    def _log_ground_truth(self, frame_idx: int, timestamp: float, frame_buffers: Dict):
        """Log vehicle positions for all cameras that can see them."""
        for cam in self.cameras:
            for veh in self.active_vehicles:
                xyz, _ = veh.get_world_xyz_and_heading()
                corners = veh.get_3d_bounding_box_corners(
                    xyz[0], xyz[1], xyz[2], 
                    veh.l, veh.w, veh.h, 0
                )
                pixels = cam.project_3d_points(corners)
                if pixels is not None:
                    # At least one corner is visible (in front of camera)
                    self.csv_buffer.append(
                        f"{frame_idx},{timestamp:.3f},{cam.id},{veh.id},{veh.plate},"
                        f"{veh.velocity:.2f},{xyz[0]:.2f},{xyz[1]:.2f},{veh.lane.id}\n"
                    )
                    
    def _write_cameras_yml(self, out_path: Path):
        """Generate cameras.yml with homography scaled to 720p MUX resolution."""
        yaml_data = {
            "max_streams": 8,
            "tiler_mode": "auto",
            "cameras": {}
        }
        
        # Scale factor: DeepStream MUX uses 1280x720, we render at 1920x1080
        scale = 1280.0 / 1920.0
        
        # Measurement zone along each approach: from NEAR (28m from center, in view)
        # to FAR (55m from center). Lateral span ±3.5m covers the two inbound lanes.
        near_d, far_d, half_w = 28.0, 55.0, 3.5
        target_height = far_d - near_d   # real-world meters along travel axis
        target_width = 2 * half_w        # real-world meters across lanes

        for i, cam in enumerate(self.cameras):
            # Build the ground rectangle in world coords oriented to this approach.
            # Point order: near-left, near-right, far-right, far-left.
            if cam.axis == 'N':
                w_pts = np.array([[-half_w, near_d, 0], [half_w, near_d, 0],
                                  [half_w, far_d, 0], [-half_w, far_d, 0]], dtype=np.float32)
            elif cam.axis == 'S':
                w_pts = np.array([[half_w, -near_d, 0], [-half_w, -near_d, 0],
                                  [-half_w, -far_d, 0], [half_w, -far_d, 0]], dtype=np.float32)
            elif cam.axis == 'E':
                w_pts = np.array([[near_d, half_w, 0], [near_d, -half_w, 0],
                                  [far_d, -half_w, 0], [far_d, half_w, 0]], dtype=np.float32)
            else:  # 'W'
                w_pts = np.array([[-near_d, -half_w, 0], [-near_d, half_w, 0],
                                  [-far_d, half_w, 0], [-far_d, -half_w, 0]], dtype=np.float32)

            p_pts = cam.project_3d_points(w_pts)
            if p_pts is None:
                # Fallback: generic rectangle covering most of frame
                p_pts = np.array([
                    [100, 800],
                    [1820, 800],
                    [1820, 200],
                    [100, 200]
                ], dtype=np.float32)

            # Scale coordinates to 720p
            scaled_src = (p_pts * scale).astype(int).tolist()
            scaled_roi = (p_pts * scale).astype(int).flatten().tolist()
            
            yaml_data["cameras"][cam.id] = {
                "camera_id": cam.id,
                "source_id": i,
                "uri": f"rtsp://192.168.212.20:8554/{cam.id}",
                "enabled": True,
                "name": f"Simulated {cam.id.replace('_', ' ').title()}",
                "fps": float(self.fps),
                "speed_limit_kmh": 50.0,
                "homography": {
                    "source_points": scaled_src,
                    "target_width": float(target_width),    # meters across lanes
                    "target_height": float(target_height)   # meters along travel axis
                },
                "roi_polygon": scaled_roi,
                "output": {
                    "record": True,
                    "record_path": f"output/{cam.id}.mp4"
                }
            }
            
        with open(out_path, 'w') as f:
            yaml.dump(yaml_data, f, default_flow_style=False, sort_keys=False)
