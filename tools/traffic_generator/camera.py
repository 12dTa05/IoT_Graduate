"""Camera projection model using extrinsic and intrinsic matrices.

Each direction (N/S/E/W) has two rear‑watching cameras:

  inbound_rear  — mounted at the far end (150 m from centre), looking toward centre.
                   Sees rears of vehicles approaching the intersection.

  outbound_rear — mounted near the centre, looking outward (away from centre).
                   Sees rears of vehicles leaving the intersection.

Each camera only observes its own 150 m road segment.
"""

import numpy as np
from enum import Enum
from typing import Optional


class CameraKind(Enum):
    INBOUND_REAR  = "inbound_rear"
    OUTBOUND_REAR = "outbound_rear"


class ProjectionCamera:
    """Traffic camera with position, orientation, and perspective projection."""

    def __init__(self, cam_id: str, axis: str, position: list, look_at: list,
                 kind: CameraKind, fov: float = 60.0):
        self.id       = cam_id
        self.axis     = axis
        self.position = np.array(position, dtype=np.float32)
        self.look_at  = np.array(look_at, dtype=np.float32)
        self.kind     = kind
        self.resolution   = (1920, 1080)
        self.fov_degrees  = fov
        self.up_vector    = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        self._solve_camera_matrices()

    # ------------------------------------------------------------------
    # Matrices
    # ------------------------------------------------------------------
    def _solve_camera_matrices(self):
        z_c = self.look_at - self.position
        n   = np.linalg.norm(z_c)
        if n < 0.1:
            raise ValueError(f"Camera {self.id}: position and look_at too close")
        z_c /= n

        x_c = np.cross(self.up_vector, z_c)
        xn  = np.linalg.norm(x_c)
        if xn < 1e-6:
            x_c = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        else:
            x_c /= xn

        y_c = np.cross(z_c, x_c)

        # R: rows = camera basis vectors expressed in world coords
        #    so that  p_cam = R @ (p_world - position)  = R @ p_world + t
        self.R = np.stack([x_c, y_c, z_c])  # (3,3), rows are x_c, y_c, z_c
        self.t = -self.R @ self.position.reshape(3, 1)

        f_pixel = self.resolution[0] / (2.0 * np.tan(np.radians(self.fov_degrees) / 2.0))
        # Negative f_y flips the image vertical axis so that the road
        # recedes UPWARD (near ground at the bottom of the frame, far
        # ground toward the top) — the natural look of a pole‑mounted
        # rear‑watching traffic camera.  Vehicles enter large at the
        # bottom and shrink as they move toward the top.
        self.K = np.array([
            [f_pixel, 0.0, self.resolution[0] / 2.0],
            [0.0, -f_pixel, self.resolution[1] / 2.0],
            [0.0, 0.0, 1.0],
        ], dtype=np.float32)

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------
    def project_3d_points(self, points_3d: np.ndarray) -> Optional[np.ndarray]:
        """Project N×3 world points → N×2 pixels.

        Returns ``None`` only when **all** points are behind the camera.
        Points behind the camera are clipped (set to NaN) individually,
        fixing the all‑or‑nothing pop‑out bug.
        """
        cam_frame = self.R @ points_3d.T + self.t     # (3, N)
        z_vals    = cam_frame[2, :]
        in_front  = z_vals > 0.3

        if not np.any(in_front):
            return None

        x = np.full_like(z_vals, np.nan)
        y = np.full_like(z_vals, np.nan)

        x[in_front] = cam_frame[0, in_front] / z_vals[in_front]
        y[in_front] = cam_frame[1, in_front] / z_vals[in_front]

        u = self.K[0, 0] * x + self.K[0, 2]
        v = self.K[1, 1] * y + self.K[1, 2]

        return np.stack([u, v], axis=1)

    def is_point_visible(self, uv: np.ndarray) -> bool:
        """True if pixel coordinate (u,v) is inside the image frame."""
        u, v = float(uv[0]), float(uv[1])
        return 0.0 <= u <= self.resolution[0] and 0.0 <= v <= self.resolution[1]

    def project_and_check_visible(self, points_3d: np.ndarray) -> Optional[np.ndarray]:
        """Project and return pixels; ``None`` if no point is in front AND in frame."""
        pixels = self.project_3d_points(points_3d)
        if pixels is None:
            return None
        visible = np.array([self.is_point_visible(p) for p in pixels])
        if not np.any(visible):
            return None
        return pixels


# ---------------------------------------------------------------------------
# Factory: build the 8‑camera array for a 4‑way intersection
# ---------------------------------------------------------------------------
def build_intersection_cameras(mount_height: float = 7.0,
                               fov: float = 40.0) -> list:
    """Return list of 8 ``ProjectionCamera`` instances for N/S/E/W × 2.

    FOV 40° with mounts set back from the road ends gives vehicles a
    natural vanishing‑point perspective across the 150 m segments:

      - far car (135 m away): ~180 px wide
      - mid‑range (85 m):     ~60 px
      - near car (25 m):      ~35 px

    The first ~5–10 m at the far extreme are behind the camera and
    invisible, which is correct for a rear‑watching mount.
    """
    INBOUND_MOUNT_Y   = 160.0   # 10 m behind spawn point
    INBOUND_LOOK_Y    = 95.0    # mid‑segment, frames the whole road
    OUTBOUND_NEAR_Y   = 10.0
    OUTBOUND_LOOK_Y   = 80.0

    cameras = []

    for axis in ("N", "S", "E", "W"):
        # assign world‑axis positions
        if axis == "N":
            far_x, far_y  = 0.0,  INBOUND_MOUNT_Y
            far_lx, far_ly = 0.0, INBOUND_LOOK_Y
            near_x, near_y = 0.0,  OUTBOUND_NEAR_Y
            near_lx, near_ly = 0.0, OUTBOUND_LOOK_Y
        elif axis == "S":
            far_x, far_y  = 0.0, -INBOUND_MOUNT_Y
            far_lx, far_ly = 0.0, -INBOUND_LOOK_Y
            near_x, near_y = 0.0, -OUTBOUND_NEAR_Y
            near_lx, near_ly = 0.0, -OUTBOUND_LOOK_Y
        elif axis == "E":
            far_x, far_y  =  INBOUND_MOUNT_Y, 0.0
            far_lx, far_ly =  INBOUND_LOOK_Y, 0.0
            near_x, near_y =  OUTBOUND_NEAR_Y, 0.0
            near_lx, near_ly =  OUTBOUND_LOOK_Y, 0.0
        else:  # W
            far_x, far_y  = -INBOUND_MOUNT_Y, 0.0
            far_lx, far_ly = -INBOUND_LOOK_Y, 0.0
            near_x, near_y = -OUTBOUND_NEAR_Y, 0.0
            near_lx, near_ly = -OUTBOUND_LOOK_Y, 0.0

        # Inbound‑rear: behind spawn, looking down‑road toward centre
        cameras.append(ProjectionCamera(
            f"cam_{axis}_in",
            axis,
            [far_x, far_y, mount_height],
            [far_lx, far_ly, 1.5],
            CameraKind.INBOUND_REAR,
            fov=fov,
        ))

        # Outbound‑rear: near centre, looking outward
        cameras.append(ProjectionCamera(
            f"cam_{axis}_out",
            axis,
            [near_x, near_y, mount_height],
            [near_lx, near_ly, 1.5],
            CameraKind.OUTBOUND_REAR,
            fov=fov,
        ))

    return cameras