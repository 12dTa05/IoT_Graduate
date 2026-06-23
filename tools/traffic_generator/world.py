"""Physical world model: lanes, traffic lights, vehicles with IDM physics.

Topology
--------
Four 150 m road segments radiate from a centre intersection (N, S, E, W).
Each segment has two inbound lanes and two outbound lanes (right‑hand traffic,
lane width = 3.5 m, half‑offset = ±1.75 m).

Vehicle lifecycle (phase machine)
---------------------------------
INBOUND   →  vehicle spawns at 150 m from centre, drives toward centre.
             Inbound‑rear camera (far end) sees its rear + plate.
             At position = 150 m (= centre) it enters TRANSIT.
TRANSIT   →  hidden phase (no camera sees it).  Speed‑based delay
             proportional to turn‑arc length.
OUTBOUND  →  reappears on the resolved outbound lane at position = 0,
             drives away from centre.  Outbound‑rear camera (near centre)
             sees its rear + plate.
DONE      →  vehicle leaves the 150 m outbound segment, gets culled.
"""

import random
import numpy as np
from dataclasses import dataclass
from enum import Enum

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROAD_LENGTH    = 150.0        # metres from centre to far end
STOP_LINE_POS  = 132.0        # distance from spawn where stop line sits (18 m from centre)
LANE_HALF_W    = 1.75         # half lane width (3.5 m lanes)
LANE_SPEED_LIMIT = 13.89      # 50 km/h in m/s


class VehiclePhase(Enum):
    INBOUND  = "inbound"
    TRANSIT  = "transit"
    OUTBOUND = "outbound"
    DONE     = "done"


# Turn routing → outbound direction key (right‑hand traffic)
TURN_ROUTES = {
    "N": {"uturn": "N_out", "left": "E_out", "right": "W_out", "straight": "S_out"},
    "S": {"uturn": "S_out", "left": "W_out", "right": "E_out", "straight": "N_out"},
    "E": {"uturn": "E_out", "left": "N_out", "right": "S_out", "straight": "W_out"},
    "W": {"uturn": "W_out", "left": "S_out", "right": "N_out", "straight": "E_out"},
}

# Approximate path length through the intersection centre for each turn type
TURN_PATH_LENGTHS = {
    "uturn":    50.0,
    "left":     42.0,
    "right":    42.0,
    "straight": 36.0,
}

TURN_WEIGHTS = {
    "straight": 0.45,
    "right":    0.25,
    "left":     0.20,
    "uturn":    0.10,
}


# ---------------------------------------------------------------------------
# Lane
# ---------------------------------------------------------------------------
@dataclass
class Lane:
    """A single lane at the intersection.

    Attributes
    ----------
    id:            Unique lane identifier (e.g. ``N_in_0``, ``S_out_1``).
    direction:     Cardinal direction of the ROAD this lane runs on.
    lane_index:    0 = right (curb‑side), 1 = left (median‑side).
    is_inbound:    True → entering; False → exiting.
    speed_limit:   Speed limit in m/s.
    """
    id: str
    direction: str
    lane_index: int
    is_inbound: bool
    speed_limit: float = LANE_SPEED_LIMIT


# ---------------------------------------------------------------------------
# Traffic‑light controller (unchanged logic)
# ---------------------------------------------------------------------------
class TrafficLightController:
    """Two‑phase traffic light with yellow and all‑red intervals."""

    def __init__(self, cycle_time=90.0, ns_green=35.0, ew_green=35.0,
                 yellow=4.0, all_red=3.0):
        self.cycle_time = cycle_time
        self.ns_green = ns_green
        self.ew_green = ew_green
        self.yellow = yellow
        self.all_red = all_red
        self.ns_end = ns_green + yellow
        self.ew_start = self.ns_end + all_red
        self.ew_end = self.ew_start + ew_green + yellow

    def get_state_for_axis(self, current_time: float, axis: str) -> str:
        t = current_time % self.cycle_time
        if axis in ("N", "S"):
            if t < self.ns_green:      return "green"
            if t < self.ns_end:        return "yellow"
            return "red"
        else:
            if t < self.ew_start:                          return "red"
            if t < self.ew_start + self.ew_green:          return "green"
            if t < self.ew_end:                            return "yellow"
            return "red"


# ---------------------------------------------------------------------------
# Vehicle agent with IDM car‑following + turning phase machine
# ---------------------------------------------------------------------------
class VehicleAgent:
    """Autonomous vehicle with IDM car‑following and intersection turning."""

    IDM_PARAMS = {
        "car":   {"s0": 2.5, "T": 1.5, "a_max": 1.8, "b_comf": 2.5},
        "suv":   {"s0": 2.8, "T": 1.6, "a_max": 1.6, "b_comf": 2.5},
        "truck": {"s0": 5.0, "T": 1.8, "a_max": 0.8, "b_comf": 2.0},
        "bus":   {"s0": 6.0, "T": 2.0, "a_max": 0.6, "b_comf": 1.8},
    }

    VEHICLE_DIMS = {
        "car":   (4.6,  1.8, 1.4),
        "suv":   (4.8,  2.0, 1.6),
        "truck": (8.5,  2.4, 2.8),
        "bus":   (11.5, 2.5, 3.2),
    }

    def __init__(self, vid: str, plate: str, vtype: str, lane: Lane,
                 spawn_time: float, turn: str, dest_lane: Lane):
        self.id         = vid
        self.plate      = plate
        self.type       = vtype
        self.lane       = lane              # current lane (inbound)
        self.spawn_time = spawn_time
        self.turn       = turn              # uturn / left / right / straight
        self.dest_lane  = dest_lane         # outbound Lane object after transit

        self.l, self.w, self.h = self.VEHICLE_DIMS.get(vtype, (4.6, 1.8, 1.4))

        params = self.IDM_PARAMS.get(vtype, self.IDM_PARAMS["car"])
        self.s0       = params["s0"]
        self.T        = params["T"]
        self.a_max    = params["a_max"]
        self.b_comf   = params["b_comf"]
        self.max_speed = lane.speed_limit

        # ── state ──
        self.phase            = VehiclePhase.INBOUND
        self.position_meters  = 0.0       # distance along current lane from its origin
        self.velocity         = random.uniform(5.0, self.max_speed * 0.8)
        self.acceleration     = 0.0
        self._transit_remain  = 0.0       # seconds remaining in TRANSIT

    # ------------------------------------------------------------------
    # Physics
    # ------------------------------------------------------------------
    def update_physics(self, dt: float, leader_dist: float, leader_vel: float,
                       light_state: str):
        if self.phase == VehiclePhase.TRANSIT:
            self._transit_remain -= dt
            if self._transit_remain <= 0.0:
                self._enter_outbound()
            return

        if self.phase == VehiclePhase.DONE:
            return

        # Inbound / Outbound IDM with stop‑line integration for inbound red
        if self.phase == VehiclePhase.INBOUND and light_state in ("red", "yellow"):
            d_stop = self.get_distance_to_stop_line()
            if 0 < d_stop < leader_dist:
                leader_dist, leader_vel = d_stop, 0.0

        v0 = self.max_speed
        dv = self.velocity - leader_vel
        s_star = self.s0 + max(0.0, self.velocity * self.T +
                               (self.velocity * dv) / (2 * np.sqrt(self.a_max * self.b_comf)))

        if leader_dist < 0.5:
            acc = -self.b_comf * 2.0
        else:
            acc = self.a_max * (1 - (self.velocity / v0) ** 4
                                - (s_star / leader_dist) ** 2)

        self.acceleration = np.clip(acc, -4.5, self.a_max)
        self.velocity += self.acceleration * dt
        self.velocity = max(0.0, self.velocity)
        self.position_meters += self.velocity * dt

        # Phase transition: INBOUND → TRANSIT at road end
        if self.phase == VehiclePhase.INBOUND and self.position_meters >= ROAD_LENGTH:
            self._enter_transit()

    # ------------------------------------------------------------------
    # Phase transitions
    # ------------------------------------------------------------------
    def _enter_transit(self):
        path_len = TURN_PATH_LENGTHS.get(self.turn, 36.0)
        vel = max(self.velocity, 3.0)  # m/s — never divide by zero
        self._transit_remain = path_len / vel
        self.phase = VehiclePhase.TRANSIT

    def _enter_outbound(self):
        self.phase            = VehiclePhase.OUTBOUND
        self.lane             = self.dest_lane
        self.position_meters  = 0.0
        # Let vehicle continue with its current velocity (no physics gap)

    # ------------------------------------------------------------------
    # Stop line
    # ------------------------------------------------------------------
    def get_distance_to_stop_line(self) -> float:
        return max(0.0, STOP_LINE_POS - self.position_meters)

    # ------------------------------------------------------------------
    # World coordinates
    # ------------------------------------------------------------------
    def get_world_xyz_and_heading(self) -> tuple:
        """Convert lane‑relative position to world (x, y, 0) and heading rad.

        Heading convention: 0 = +X (east), π/2 = +Y (north).

        Inbound:  vehicle faces TOWARD centre, camera is behind → sees rear.
        Outbound: vehicle faces AWAY from centre, camera is behind → sees rear.
        """
        d       = self.lane.direction
        pos     = self.position_meters
        idx     = self.lane.lane_index

        # Lane offset, right‑hand traffic
        if d in ("N", "S"):
            off_x = (-LANE_HALF_W if idx == 0 else LANE_HALF_W) if d == "N" else \
                    ( LANE_HALF_W if idx == 0 else -LANE_HALF_W)
            off_y = 0.0
        else:  # E, W
            off_y = (-LANE_HALF_W if idx == 0 else LANE_HALF_W) if d == "E" else \
                    ( LANE_HALF_W if idx == 0 else -LANE_HALF_W)
            off_x = 0.0

        if self.lane.is_inbound:
            # spawn at far end (150 m), drive toward centre
            if d == "N":
                world_x, world_y = off_x, ROAD_LENGTH - pos
                heading = -np.pi / 2    # facing south
            elif d == "S":
                world_x, world_y = off_x, -ROAD_LENGTH + pos
                heading = np.pi / 2     # facing north
            elif d == "E":
                world_x, world_y = ROAD_LENGTH - pos, off_y
                heading = np.pi         # facing west
            else:  # W
                world_x, world_y = -ROAD_LENGTH + pos, off_y
                heading = 0.0          # facing east
        else:
            # outbound — start near centre (pos=0), drive away from centre
            if d == "N":
                world_x, world_y = off_x, pos
                heading = np.pi / 2     # facing north
            elif d == "S":
                world_x, world_y = off_x, -pos
                heading = -np.pi / 2    # facing south
            elif d == "E":
                world_x, world_y = pos, off_y
                heading = 0.0          # facing east
            else:  # W
                world_x, world_y = -pos, off_y
                heading = np.pi         # facing west

        return np.array([world_x, world_y, 0.0]), heading
