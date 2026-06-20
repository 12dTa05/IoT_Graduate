"""Physical world model: lanes, traffic lights, vehicles with IDM physics."""

import random
import numpy as np
from dataclasses import dataclass
from typing import List

@dataclass
class Lane:
    """A single lane at the intersection.
    
    Attributes:
        id: Unique lane identifier (e.g., 'N_in_0')
        direction: Cardinal direction ('N', 'S', 'E', 'W')
        lane_index: 0 for right lane, 1 for left lane (right-hand traffic)
        is_inbound: True if entering intersection, False if exiting
        speed_limit: Speed limit in m/s (default 13.89 = 50 km/h)
    """
    id: str
    direction: str          # 'N', 'S', 'E', 'W'
    lane_index: int         # 0: right lane (nearer to curb), 1: left lane (nearer to median)
    is_inbound: bool        # True: entering intersection, False: exiting
    speed_limit: float = 13.89  # 50 km/h in m/s

class TrafficLightController:
    """Two-phase traffic light with optional yellow and all-red intervals."""
    
    def __init__(self, cycle_time=90.0, ns_green=35.0, ew_green=35.0, 
                 yellow=4.0, all_red=3.0):
        self.cycle_time = cycle_time
        self.ns_green = ns_green
        self.ew_green = ew_green
        self.yellow = yellow
        self.all_red = all_red
        
        # Compute phase boundaries
        # Phase 1: NS green, EW red
        # Then NS yellow (while EW still red)
        # Then all-red interphase
        # Then EW green, NS red
        # Then EW yellow
        # Then all-red, repeat
        self.ns_end = ns_green + yellow
        self.ew_start = self.ns_end + all_red
        self.ew_end = self.ew_start + ew_green + yellow
        
    def get_state_for_axis(self, current_time: float, axis: str) -> str:
        """Get traffic light state for an approach axis.
        
        Args:
            current_time: Simulation time in seconds
            axis: 'N', 'S', 'E', or 'W'
            
        Returns:
            'green', 'yellow', or 'red'
        """
        t = current_time % self.cycle_time
        
        if axis in ['N', 'S']:
            if t < self.ns_green:
                return 'green'
            elif t < self.ns_end:
                return 'yellow'
            else:
                return 'red'
        else:  # E, W
            if t < self.ew_start:
                return 'red'
            elif t < self.ew_start + self.ew_green:
                return 'green'
            elif t < self.ew_end:
                return 'yellow'
            else:
                return 'red'


class VehicleAgent:
    """Autonomous vehicle with IDM car-following behavior."""
    
    # Class-level IDM parameters by vehicle type
    IDM_PARAMS = {
        'car':   {'s0': 2.5, 'T': 1.5, 'a_max': 1.8, 'b_comf': 2.5},
        'suv':   {'s0': 2.8, 'T': 1.6, 'a_max': 1.6, 'b_comf': 2.5},
        'truck': {'s0': 5.0, 'T': 1.8, 'a_max': 0.8, 'b_comf': 2.0},
        'bus':   {'s0': 6.0, 'T': 2.0, 'a_max': 0.6, 'b_comf': 1.8},
    }
    
    # Vehicle dimensions (length, width, height) in meters
    VEHICLE_DIMS = {
        'car':   (4.6, 1.8, 1.4),
        'suv':   (4.8, 2.0, 1.6),
        'truck': (8.5, 2.4, 2.8),
        'bus':   (11.5, 2.5, 3.2),
    }
    
    def __init__(self, vid: str, plate: str, vtype: str, lane: Lane, spawn_time: float):
        """Initialize a vehicle agent.
        
        Args:
            vid: Unique vehicle ID
            plate: License plate string
            vtype: Vehicle type ('car', 'suv', 'truck', 'bus')
            lane: Lane object this vehicle travels in
            spawn_time: Simulation time when vehicle spawns
        """
        self.id = vid
        self.plate = plate
        self.type = vtype
        self.lane = lane
        self.spawn_time = spawn_time
        
        # Set physical dimensions
        self.l, self.w, self.h = self.VEHICLE_DIMS.get(vtype, (4.6, 1.8, 1.4))
        
        # Set IDM parameters based on vehicle type
        params = self.IDM_PARAMS.get(vtype, self.IDM_PARAMS['car'])
        self.s0 = params['s0']
        self.T = params['T']
        self.a_max = params['a_max']
        self.b_comf = params['b_comf']
        
        self.max_speed = lane.speed_limit
        
        # State
        self.position_meters = 0.0  # Distance along lane from spawn point
        self.velocity = random.uniform(5.0, self.max_speed * 0.8)
        self.acceleration = 0.0
        
    def update_physics(self, dt: float, leader_dist: float, leader_vel: float, light_state: str):
        """Update vehicle position using IDM with traffic light integration.
        
        Args:
            dt: Time step in seconds
            leader_dist: Distance to rear bumper of leading vehicle (meters)
            leader_vel: Velocity of leading vehicle (m/s)
            light_state: Traffic light state ('green', 'yellow', 'red')
        """
        # If inbound and light is red/yellow, treat stop line as virtual obstacle
        if self.lane.is_inbound and light_state in ['red', 'yellow']:
            dist_to_stop = self.get_distance_to_stop_line()
            # Only use stop line as leader if it's closer than actual leader
            if 0 < dist_to_stop < leader_dist:
                leader_dist = dist_to_stop
                leader_vel = 0.0
                
        # IDM acceleration calculation
        v0 = self.max_speed
        dv = self.velocity - leader_vel
        
        # Desired dynamic gap
        s_star = self.s0 + max(0.0, self.velocity * self.T + 
                               (self.velocity * dv) / (2 * np.sqrt(self.a_max * self.b_comf)))
        
        # Acceleration term
        if leader_dist < 0.5:
            # Very close - emergency braking
            acc = -self.b_comf * 2.0
        else:
            acc = self.a_max * (1 - (self.velocity / v0)**4 - (s_star / leader_dist)**2)
            
        self.acceleration = np.clip(acc, -4.5, self.a_max)
        self.velocity += self.acceleration * dt
        self.velocity = max(0.0, self.velocity)
        self.position_meters += self.velocity * dt
        
    def get_distance_to_stop_line(self) -> float:
        """Return distance from vehicle front bumper to stop line (at 18m from center).
        
        For inbound vehicles, the stop line is at position = 42m along the lane.
        """
        return max(0.0, 42.0 - self.position_meters)
        
    def get_world_xyz_and_heading(self) -> tuple:
        """Convert lane-relative position to world coordinates.
        
        Returns:
            (xyz, heading): xyz is np.array([x, y, 0]) in meters, 
                           heading is radians (0 = east, π/2 = north)
        """
        d = self.lane.direction
        pos = self.position_meters
        lane_idx = self.lane.lane_index
        
        # Determine X/Y offsets for the lane
        # Right-hand traffic: lane 0 is right (curbside), lane 1 is left (median)
        # For N-S roads, lanes offset in X direction
        # For E-W roads, lanes offset in Y direction
        
        # Coordinate system: X east (+), Y north (+)
        if d in ['N', 'S']:
            # Road runs north-south
            # Right lane offset: for N-bound (going north) right lane is west (-X)
            #                   for S-bound (going south) right lane is east (+X)
            if d == 'N':
                offset_x = -1.75 if lane_idx == 0 else 1.75
            else:  # S
                offset_x = 1.75 if lane_idx == 0 else -1.75
            offset_y = 0.0
        else:  # E, W
            # Road runs east-west
            # Right lane offset: for E-bound (going east) right lane is south (-Y)
            #                   for W-bound (going west) right lane is north (+Y)
            if d == 'E':
                offset_y = -1.75 if lane_idx == 0 else 1.75
            else:  # W
                offset_y = 1.75 if lane_idx == 0 else -1.75
            offset_x = 0.0
            
        # Position along the road axis
        if self.lane.is_inbound:
            # Inbound: start at far end (60m from center), move toward stop line (18m)
            if d == 'N':
                world_x, world_y = offset_x, 60.0 - pos
                heading = -np.pi/2  # facing south
            elif d == 'S':
                world_x, world_y = offset_x, -60.0 + pos
                heading = np.pi/2   # facing north
            elif d == 'E':
                world_x, world_y = 60.0 - pos, offset_y
                heading = np.pi     # facing west
            else:  # W
                world_x, world_y = -60.0 + pos, offset_y
                heading = 0.0       # facing east
        else:
            # Outbound: start at stop line (18m), move outward
            if d == 'N':
                world_x, world_y = offset_x, 18.0 + pos
                heading = np.pi/2   # facing north
            elif d == 'S':
                world_x, world_y = offset_x, -18.0 - pos
                heading = -np.pi/2  # facing south
            elif d == 'E':
                world_x, world_y = 18.0 + pos, offset_y
                heading = 0.0       # facing east
            else:  # W
                world_x, world_y = -18.0 - pos, offset_y
                heading = np.pi     # facing west
                
        return np.array([world_x, world_y, 0.0]), heading
        
    def get_3d_bounding_box_corners(self, x, y, z, length, width, height, heading):
        """Generate 8 corners of the vehicle's 3D bounding box.
        
        Order: 0-3 ground corners (rear-left, front-left, front-right, rear-right)
               4-7 roof corners   (same order)
        """
        hl, hw = length/2, width/2
        
        # Local corners in vehicle coordinate system (center at origin, x=forward)
        local = np.array([
            [-hl, -hw, 0],     # 0: rear-left ground
            [ hl, -hw, 0],     # 1: front-left ground
            [ hl,  hw, 0],     # 2: front-right ground
            [-hl,  hw, 0],     # 3: rear-right ground
            [-hl, -hw, height],  # 4: rear-left roof
            [ hl, -hw, height],  # 5: front-left roof
            [ hl,  hw, height],  # 6: front-right roof
            [-hl,  hw, height],  # 7: rear-right roof
        ], dtype=np.float32)
        
        # Rotate around Z axis by heading
        cos_h, sin_h = np.cos(heading), np.sin(heading)
        rot = np.array([
            [cos_h, -sin_h, 0],
            [sin_h, cos_h, 0],
            [0, 0, 1]
        ])
        rotated = (rot @ local.T).T
        
        # Translate to world position
        world = rotated + np.array([x, y, z])
        return world
