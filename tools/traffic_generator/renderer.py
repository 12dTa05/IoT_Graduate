"""Procedural vehicle renderer with sub-pixel Lanczos4 warping."""

import cv2
import numpy as np

class SubPixelProceduralRenderer:
    """Renders high-resolution vehicle textures and warps them onto the frame."""
    
    def __init__(self):
        # Vehicle colors (BGR)
        self.class_colors = {
            'car': (50, 100, 200),     # Blue
            'suv': (50, 150, 100),     # Green
            'truck': (200, 100, 50),   # Orange
            'bus': (150, 150, 50),     # Olive
        }
        
    def render_high_res_vehicle(self, vtype: str, plate_text: str) -> np.ndarray:
        """Procedurally draw a vehicle on 2048x1024 RGBA canvas.
        
        The vehicle is drawn as a top-down view with:
        - Main body rectangle
        - Windshield and rear window
        - Side windows
        - Wheels at corners
        - License plate at rear
        """
        canvas = np.zeros((1024, 2048, 4), dtype=np.uint8)
        color = self.class_colors.get(vtype, (100, 100, 100))
        
        # Main body (chassis) - large rectangle
        cv2.rectangle(canvas, (200, 212), (1848, 812), (*color, 255), -1)
        
        # Windshield (front)
        cv2.rectangle(canvas, (500, 262), (800, 762), (60, 60, 60, 255), -1)
        
        # Rear window
        cv2.rectangle(canvas, (1400, 262), (1550, 762), (40, 40, 40, 255), -1)
        
        # Side windows (two rectangles)
        cv2.rectangle(canvas, (800, 262), (1000, 762), (80, 80, 80, 255), -1)
        cv2.rectangle(canvas, (1050, 262), (1250, 762), (80, 80, 80, 255), -1)
        
        # Wheels (black rectangles at corners)
        wheel_color = (20, 20, 20, 255)
        wheel_w, wheel_h = 120, 80
        # Front left
        cv2.rectangle(canvas, (400, 200), (400 + wheel_w, 200 + wheel_h), wheel_color, -1)
        # Front right
        cv2.rectangle(canvas, (1500, 200), (1500 + wheel_w, 200 + wheel_h), wheel_color, -1)
        # Rear left
        cv2.rectangle(canvas, (400, 850), (400 + wheel_w, 850 + wheel_h), wheel_color, -1)
        # Rear right
        cv2.rectangle(canvas, (1500, 850), (1500 + wheel_w, 850 + wheel_h), wheel_color, -1)
        
        # License plate area (white rectangle at rear)
        cv2.rectangle(canvas, (1700, 437), (1830, 587), (255, 255, 255, 255), -1)
        cv2.rectangle(canvas, (1700, 437), (1830, 587), (0, 0, 0, 255), 6)  # border
        
        # License plate text
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 1.6
        thickness = 5
        text_size = cv2.getTextSize(plate_text, font, scale, thickness)[0]
        text_x = 1700 + (130 - text_size[0]) // 2
        text_y = 437 + (150 + text_size[1]) // 2
        cv2.putText(canvas, plate_text, (text_x, text_y), 
                    font, scale, (0, 0, 0, 255), thickness, cv2.LINE_AA)
        
        # Add subtle shadow for depth
        shadow = (canvas[:, :, 3].astype(np.float32) * 0.3).astype(np.uint8)
        canvas[:, :, 3] = np.clip(canvas[:, :, 3].astype(np.int32) + shadow, 0, 255).astype(np.uint8)
        
        return canvas
    
    def get_vehicle_3d_corners(self, x, y, z, length, width, height, heading):
        """Generate 8 corners of vehicle bounding box in world coordinates.
        
        Returns:
            corners: (8, 3) array with corners 0-3 ground (rear-left, front-left, front-right, rear-right)
                     and 4-7 roof (same order)
        """
        hl, hw = length / 2, width / 2
        
        # Local corners in vehicle coordinate system (origin at center, x=forward)
        local = np.array([
            [-hl, -hw, 0],      # 0: rear-left ground
            [ hl, -hw, 0],      # 1: front-left ground
            [ hl,  hw, 0],      # 2: front-right ground
            [-hl,  hw, 0],      # 3: rear-right ground
            [-hl, -hw, height], # 4: rear-left roof
            [ hl, -hw, height], # 5: front-left roof
            [ hl,  hw, height], # 6: front-right roof
            [-hl,  hw, height], # 7: rear-right roof
        ], dtype=np.float32)
        
        # Rotation around Z axis
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
    
    def project_and_warp(self, frame: np.ndarray, vehicle, xyz: np.ndarray, 
                        heading: float, camera) -> np.ndarray:
        """Project a vehicle onto the frame using homography warping.
        
        Args:
            frame: (H, W, 3) BGR image to draw on
            vehicle: VehicleAgent object
            xyz: World position (x, y, z)
            heading: Heading in radians
            camera: ProjectionCamera object
            
        Returns:
            Updated frame with vehicle composited
        """
        # Get 3D corners of vehicle
        corners_3d = self.get_vehicle_3d_corners(
            xyz[0], xyz[1], xyz[2],
            vehicle.l, vehicle.w, vehicle.h,
            heading
        )
        
        # Project to 2D image coordinates
        pixels = camera.project_3d_points(corners_3d)
        if pixels is None:
            return frame
            
        # Use ground quadrilateral (first 4 corners) for homography
        dst_quad = pixels[:4].astype(np.float32)
        
        # Generate high-res vehicle texture with plate
        high_res = self.render_high_res_vehicle(vehicle.type, vehicle.plate)
        h_src, w_src = high_res.shape[:2]
        src_quad = np.array([[0, 0], [w_src, 0], [w_src, h_src], [0, h_src]], dtype=np.float32)
        
        # Compute homography from source to destination
        H, mask = cv2.findHomography(src_quad, dst_quad)
        if H is None:
            return frame
            
        # Warp with Lanczos4 interpolation for sub-pixel text preservation
        warped = cv2.warpPerspective(
            high_res, H, (frame.shape[1], frame.shape[0]),
            flags=cv2.INTER_LANCZOS4,
            borderMode=cv2.BORDER_TRANSPARENT
        )
        
        # Alpha blending: foreground over background using alpha channel
        if warped.shape[2] == 4:
            alpha = warped[:, :, 3].astype(np.float32) / 255.0
            alpha = alpha[:, :, np.newaxis]
            rgb = warped[:, :, :3].astype(np.float32)
            frame_float = frame.astype(np.float32)
            blended = frame_float * (1 - alpha) + rgb * alpha
            return blended.clip(0, 255).astype(np.uint8)
        else:
            return warped
