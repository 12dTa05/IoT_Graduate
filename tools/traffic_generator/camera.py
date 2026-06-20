"""Camera projection model using extrinsic and intrinsic matrices."""

import numpy as np

class ProjectionCamera:
    """Traffic camera with position, orientation, and perspective projection."""
    
    def __init__(self, cam_id: str, axis: str, position: list, look_at: list, fov=48.0):
        """Initialize camera.
        
        Args:
            cam_id: Camera identifier (e.g., 'cam_north')
            axis: Primary axis ('N', 'S', 'E', 'W')
            position: [x, y, z] in world coordinates (meters)
            look_at: [x, y, z] point the camera is pointing at
            fov: Horizontal field of view in degrees
        """
        self.id = cam_id
        self.axis = axis
        self.position = np.array(position, dtype=np.float32)
        self.look_at = np.array(look_at, dtype=np.float32)
        self.resolution = (1920, 1080)
        self.fov_degrees = fov
        self.up_vector = np.array([0, 0, 1], dtype=np.float32)
        self._solve_camera_matrices()
        
    def _solve_camera_matrices(self):
        """Compute extrinsic (R, t) and intrinsic (K) matrices."""
        # Forward direction (z-axis in camera coordinates)
        z_c = self.look_at - self.position
        norm = np.linalg.norm(z_c)
        if norm < 0.1:
            raise ValueError(f"Camera {self.id}: position and look_at too close")
        z_c /= norm
        
        # Right direction (x-axis)
        x_c = np.cross(self.up_vector, z_c)
        x_norm = np.linalg.norm(x_c)
        if x_norm < 1e-6:
            # Camera is looking straight up/down; use default right
            x_c = np.array([1, 0, 0], dtype=np.float32)
        else:
            x_c /= x_norm
            
        # Up direction (y-axis) - completes right-handed system
        y_c = np.cross(z_c, x_c)
        
        # Rotation matrix: transforms world to camera coordinates
        # R rows are the camera basis vectors expressed in world coords
        self.R = np.stack([x_c, y_c, z_c], axis=1).T
        
        # Translation: t = -R * position
        self.t = -self.R @ self.position.reshape(3, 1)
        
        # Intrinsic matrix K
        f_pixel = self.resolution[0] / (2 * np.tan(np.radians(self.fov_degrees) / 2))
        self.K = np.array([
            [f_pixel, 0, self.resolution[0] / 2],
            [0, f_pixel, self.resolution[1] / 2],
            [0, 0, 1]
        ], dtype=np.float32)
        
    def project_3d_points(self, points_3d: np.ndarray) -> np.ndarray:
        """Project 3D world points to 2D pixel coordinates.
        
        Args:
            points_3d: (N, 3) array of (x, y, z) in world meters
            
        Returns:
            (N, 2) array of (u, v) pixel coordinates, or None if any point is behind camera
        """
        # Transform to camera coordinates: p_cam = R @ p_world + t
        cam_frame = self.R @ points_3d.T + self.t  # (3, N)
        
        # Check if any point is behind the camera (z <= 0)
        z_vals = cam_frame[2, :]
        if np.any(z_vals <= 0.3):
            return None
            
        # Perspective division: (x/z, y/z)
        x = cam_frame[0, :] / z_vals
        y = cam_frame[1, :] / z_vals
        
        # Apply intrinsic matrix: u = f*x + c_x, v = f*y + c_y
        u = self.K[0, 0] * x + self.K[0, 2]
        v = self.K[1, 1] * y + self.K[1, 2]
        
        return np.stack([u, v], axis=1)
