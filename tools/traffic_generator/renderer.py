"""Procedural vehicle renderer with sub‑pixel Lanczos4 warping.

The vehicle texture is drawn as a top‑down plan view:

  canvas X‑axis → vehicle RIGHT side (passenger side in right‑hand traffic)
  canvas Y‑axis → vehicle FRONT  →  REAR  direction

The homography maps the texture onto the projected ground quad.
The quad corners (from ``get_ground_quad_corners``) are ordered:

  0: rear‑left   ground
  1: front‑left  ground
  2: front‑right ground
  3: rear‑right  ground

The source quad must follow the SAME order so the plate (drawn at the
REAR of the texture) lands on the REAR of the projected vehicle.
"""

import cv2
import numpy as np


class SubPixelProceduralRenderer:
    """Renders high‑resolution vehicle textures and warps them onto the frame."""

    # Per‑camera scene variation so the 8 views are visually distinct
    # (sky tint, asphalt tone, shoulder colour) keyed by camera id.
    SCENE_STYLES = {
        "cam_N_in":  {"sky": (150, 130, 110), "road": (52, 52, 54), "shoulder": (70, 95, 70)},
        "cam_N_out": {"sky": (165, 150, 130), "road": (46, 46, 48), "shoulder": (90, 90, 95)},
        "cam_S_in":  {"sky": (120, 140, 165), "road": (55, 53, 50), "shoulder": (60, 85, 60)},
        "cam_S_out": {"sky": (135, 150, 170), "road": (48, 48, 50), "shoulder": (85, 88, 92)},
        "cam_E_in":  {"sky": (160, 145, 120), "road": (50, 51, 53), "shoulder": (72, 92, 68)},
        "cam_E_out": {"sky": (170, 158, 140), "road": (44, 45, 47), "shoulder": (95, 92, 90)},
        "cam_W_in":  {"sky": (128, 135, 158), "road": (53, 52, 51), "shoulder": (65, 88, 64)},
        "cam_W_out": {"sky": (142, 148, 165), "road": (47, 47, 49), "shoulder": (88, 90, 94)},
    }
    _DEFAULT_STYLE = {"sky": (150, 140, 125), "road": (50, 50, 52), "shoulder": (80, 90, 80)}

    def __init__(self):
        self.class_colors = {
            "car":   (50, 100, 200),     # Blue
            "suv":   (50, 150, 100),     # Green
            "truck": (200, 100, 50),     # Orange
            "bus":   (150, 150, 50),     # Olive
        }

    # ------------------------------------------------------------------
    # Road‑scene background (perspective‑correct, per camera)
    # ------------------------------------------------------------------
    def render_road_background(self, camera) -> np.ndarray:
        """Render a distinct perspective road scene for *camera*.

        Projects the camera's own 150 m road segment (asphalt, lane
        markings, edge lines, shoulders) through its projection matrix
        so each of the 8 cameras gets a recognisably different view
        instead of a flat grey plate.
        """
        from .world import ROAD_LENGTH, LANE_HALF_W

        H, W = 1080, 1920
        style = self.SCENE_STYLES.get(camera.id, self._DEFAULT_STYLE)
        frame = np.empty((H, W, 3), dtype=np.uint8)
        frame[:] = style["sky"]                     # sky / far backdrop

        axis = camera.axis
        inbound = (camera.kind.value == "inbound_rear")

        # Build the world‑space extent of THIS camera's road segment.
        # near_d / far_d are distances from centre along the road axis.
        # The near edge must stay in FRONT of the mount, otherwise the
        # ground quad straddles the near plane and gets skipped.
        if inbound:
            # mount at 160 m looking toward centre → road 8..150 m all visible
            near_d, far_d = 8.0, ROAD_LENGTH
        else:
            # mount at ~10 m looking outward → road must start past the mount
            near_d, far_d = 18.0, ROAD_LENGTH

        road_half = LANE_HALF_W * 2.0 + 1.0         # both lanes + margin
        shoulder_half = road_half + 6.0

        # World rectangle helper for each axis (along=distance from centre,
        # lat=lateral offset). Sign chosen so the strip sits on the camera's road.
        def strip(lat0, lat1):
            d0, d1 = near_d, far_d
            if axis == "N":
                sgn = 1.0
                quad = [[lat0, d0 * sgn, 0], [lat1, d0 * sgn, 0],
                        [lat1, d1 * sgn, 0], [lat0, d1 * sgn, 0]]
            elif axis == "S":
                sgn = -1.0
                quad = [[lat0, d0 * sgn, 0], [lat1, d0 * sgn, 0],
                        [lat1, d1 * sgn, 0], [lat0, d1 * sgn, 0]]
            elif axis == "E":
                sgn = 1.0
                quad = [[d0 * sgn, lat0, 0], [d0 * sgn, lat1, 0],
                        [d1 * sgn, lat1, 0], [d1 * sgn, lat0, 0]]
            else:  # W
                sgn = -1.0
                quad = [[d0 * sgn, lat0, 0], [d0 * sgn, lat1, 0],
                        [d1 * sgn, lat1, 0], [d1 * sgn, lat0, 0]]
            return np.array(quad, dtype=np.float32)

        def fill_world_poly(world_quad, color):
            px = camera.project_3d_points(world_quad)
            if px is None or np.any(np.isnan(px)):
                return
            pts = px.reshape(-1, 1, 2).astype(np.int32)
            cv2.fillPoly(frame, [pts], color)

        # Shoulders (grass/kerb), then asphalt on top
        fill_world_poly(strip(-shoulder_half, shoulder_half), style["shoulder"])
        fill_world_poly(strip(-road_half, road_half), style["road"])

        # Lane markings: dashed centre line + solid edge lines
        # Centre dashed line (between the two lanes, lateral = 0)
        n_dash = 26
        for i in range(0, n_dash, 2):
            a0 = near_d + (far_d - near_d) * (i / n_dash)
            a1 = near_d + (far_d - near_d) * ((i + 1) / n_dash)
            self._fill_lane_dash(frame, camera, axis, a0, a1, -0.15, 0.15,
                                 (235, 235, 235))
        # Solid white edge lines
        for lat in (-LANE_HALF_W * 2.0, LANE_HALF_W * 2.0):
            q = strip(lat - 0.15, lat + 0.15)
            fill_world_poly(q, (220, 220, 220))

        return frame

    @staticmethod
    def _fill_lane_dash(frame, camera, axis, d0, d1, lat0, lat1, color):
        if axis == "N":
            quad = [[lat0, d0, 0], [lat1, d0, 0], [lat1, d1, 0], [lat0, d1, 0]]
        elif axis == "S":
            quad = [[lat0, -d0, 0], [lat1, -d0, 0], [lat1, -d1, 0], [lat0, -d1, 0]]
        elif axis == "E":
            quad = [[d0, lat0, 0], [d0, lat1, 0], [d1, lat1, 0], [d1, lat0, 0]]
        else:  # W
            quad = [[-d0, lat0, 0], [-d0, lat1, 0], [-d1, lat1, 0], [-d1, lat0, 0]]
        px = camera.project_3d_points(np.array(quad, dtype=np.float32))
        if px is None or np.any(np.isnan(px)):
            return
        cv2.fillPoly(frame, [px.reshape(-1, 1, 2).astype(np.int32)], color)

    # ------------------------------------------------------------------
    # Texture (2048×1024 RGBA canvas)
    #   Layout:  Y‑axis = FRONT (top, y≈0) → REAR (bottom, y≈1024)
    #            X‑axis = LEFT (x≈0) → RIGHT (x≈2048)
    # ------------------------------------------------------------------
    def render_high_res_vehicle(self, vtype: str, plate_text: str) -> np.ndarray:
        canvas = np.zeros((1024, 2048, 4), dtype=np.uint8)
        color = self.class_colors.get(vtype, (100, 100, 100))

        # Main body
        cv2.rectangle(canvas, (200, 212), (1848, 812), (*color, 255), -1)

        # Windshield (FRONT — top of canvas, X‑centred horizontal band)
        cv2.rectangle(canvas, (624, 212), (1424, 420), (60, 60, 60, 255), -1)

        # Rear window (REAR — bottom of canvas, X‑centred horizontal band)
        cv2.rectangle(canvas, (724, 580), (1324, 812), (40, 40, 40, 255), -1)

        # Side windows (middle band, left and right edges)
        cv2.rectangle(canvas, (250, 430), (550, 560), (80, 80, 80, 255), -1)
        cv2.rectangle(canvas, (1498, 430), (1798, 560), (80, 80, 80, 255), -1)

        # Wheels (front = top, rear = bottom; left/right on X)
        wc = (20, 20, 20, 255)
        ww, wh = 120, 80
        cv2.rectangle(canvas, (400, 200), (400 + ww, 200 + wh), wc, -1)
        cv2.rectangle(canvas, (1500, 200), (1500 + ww, 200 + wh), wc, -1)
        cv2.rectangle(canvas, (400, 850), (400 + ww, 850 + wh), wc, -1)
        cv2.rectangle(canvas, (1500, 850), (1500 + ww, 850 + wh), wc, -1)

        # License plate at the REAR (bottom of canvas, X‑centred)
        plate_w, plate_h = 300, 180
        plate_x0 = (2048 - plate_w) // 2
        plate_y0 = 800
        cv2.rectangle(canvas, (plate_x0, plate_y0),
                      (plate_x0 + plate_w, plate_y0 + plate_h),
                      (255, 255, 255, 255), -1)
        cv2.rectangle(canvas, (plate_x0, plate_y0),
                      (plate_x0 + plate_w, plate_y0 + plate_h),
                      (0, 0, 0, 255), 6)

        # Auto‑fit text inside the plate rectangle
        font      = cv2.FONT_HERSHEY_SIMPLEX
        thickness = 4
        scale = 0.6       # L1: default in case the scale list is ever empty
        ts = (0, 0)
        for scale in (1.8, 1.6, 1.4, 1.2, 1.0, 0.8, 0.6):
            ts = cv2.getTextSize(plate_text, font, scale, thickness)[0]
            if ts[0] <= plate_w - 20 and ts[1] <= plate_h - 20:
                break
        tx = plate_x0 + (plate_w - ts[0]) // 2
        ty = plate_y0 + (plate_h + ts[1]) // 2
        cv2.putText(canvas, plate_text, (tx, ty), font, scale,
                    (0, 0, 0, 255), thickness, cv2.LINE_AA)

        # Subtle shadow
        shadow = (canvas[:, :, 3].astype(np.float32) * 0.3).astype(np.uint8)
        canvas[:, :, 3] = np.clip(
            canvas[:, :, 3].astype(np.int32) + shadow, 0, 255
        ).astype(np.uint8)

        return canvas

    # ------------------------------------------------------------------
    # 3D corners → ground quad (shared utility — no duplicate)
    # ------------------------------------------------------------------
    @staticmethod
    def get_ground_quad_corners(x, y, z, length, width, height, heading):
        """Return (4, 3) world coords of the ground rectangle.

        Order:  0: rear‑left,  1: front‑left,  2: front‑right,  3: rear‑right.
        This is the canonical order used by the homography source quad.
        """
        hl, hw = length / 2, width / 2
        local = np.array([
            [-hl, -hw, 0.0],   # 0 rear‑left
            [ hl, -hw, 0.0],   # 1 front‑left
            [ hl,  hw, 0.0],   # 2 front‑right
            [-hl,  hw, 0.0],   # 3 rear‑right
        ], dtype=np.float32)

        cos_h, sin_h = np.cos(heading), np.sin(heading)
        rot = np.array([
            [cos_h, -sin_h, 0.0],
            [sin_h,  cos_h, 0.0],
            [0.0,     0.0,  1.0],
        ])
        rotated = (rot @ local.T).T
        return rotated + np.array([x, y, z])

    @staticmethod
    def get_3d_bounding_box_corners(x, y, z, length, width, height, heading):
        """Return (8, 3) world coords: 0‑3 ground, 4‑7 roof."""
        hl, hw = length / 2, width / 2
        local = np.array([
            [-hl, -hw, 0.0], [ hl, -hw, 0.0], [ hl, hw, 0.0], [-hl, hw, 0.0],
            [-hl, -hw, height], [ hl, -hw, height], [ hl, hw, height], [-hl, hw, height],
        ], dtype=np.float32)
        cos_h, sin_h = np.cos(heading), np.sin(heading)
        rot = np.array([
            [cos_h, -sin_h, 0.0],
            [sin_h,  cos_h, 0.0],
            [0.0,     0.0,  1.0],
        ])
        rotated = (rot @ local.T).T
        return rotated + np.array([x, y, z])

    @staticmethod
    def _dst_quad_sane(dst_quad: np.ndarray, frame_shape: tuple) -> bool:
        """Validate a projected destination quad before warping.

        Rejects quads that are:
          - non-finite (NaN / inf from near-camera projection)
          - degenerate or near-collinear (area too small)
          - wildly extrapolated (area or coordinates far outside the frame)

        These cases would produce a singular or extreme homography that
        smears the vehicle texture across the entire frame.
        """
        if not np.all(np.isfinite(dst_quad)):
            return False

        x = dst_quad[:, 0]
        y = dst_quad[:, 1]
        # Shoelace formula for polygon area
        area = 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))

        # Minimum: a plausible distant vehicle still covers > 200 px²
        if area < 200.0:
            return False

        # Maximum: shouldn't be many times the frame area (extrapolation)
        h, w = frame_shape[0], frame_shape[1]
        if area > h * w * 5:
            return False

        # Bounds: allow some overflow but reject extreme extrapolation
        margin = max(w, h) * 0.5
        if np.any(x < -margin) or np.any(x > w + margin):
            return False
        if np.any(y < -margin) or np.any(y > h + margin):
            return False

        return True

    # ------------------------------------------------------------------
    # Project + warp
    # ------------------------------------------------------------------
    def project_and_warp(self, frame: np.ndarray, vehicle, xyz: np.ndarray,
                         heading: float, camera) -> np.ndarray:
        """Project a vehicle onto the frame using homography warping."""
        # Ground quad in world coords
        quad_3d = self.get_ground_quad_corners(
            xyz[0], xyz[1], xyz[2],
            vehicle.l, vehicle.w, vehicle.h, heading,
        )

        # Project to 2D
        pixels = camera.project_3d_points(quad_3d)
        if pixels is None:
            return frame

        # If any quad corner is NaN (behind camera), skip rendering
        if np.any(np.isnan(pixels)):
            return frame

        dst_quad = pixels[:4].astype(np.float32)

        # Source quad — MUST match the canonical corner order:
        #   0: rear‑left  → (0, h_src)     bottom‑left
        #   1: front‑left → (0, 0)          top‑left
        #   2: front‑right → (w_src, 0)     top‑right
        #   3: rear‑right  → (w_src, h_src) bottom‑right
        high_res = self.render_high_res_vehicle(vehicle.type, vehicle.plate)
        h_src, w_src = high_res.shape[:2]
        src_quad = np.array([
            [0.0,  h_src],     # rear‑left
            [0.0,  0.0],      # front‑left
            [w_src, 0.0],     # front‑right
            [w_src, h_src],   # rear‑right
        ], dtype=np.float32)

        # Validate the projected quad before warping (reject degenerate /
        # near-collinear / wildly extrapolated quads that would smear the
        # texture across the entire frame).
        if not self._dst_quad_sane(dst_quad, frame.shape):
            return frame

        try:
            H = cv2.getPerspectiveTransform(src_quad, dst_quad)
        except cv2.error:
            # Singular matrix — collinear source/dest points
            return frame

        warped = cv2.warpPerspective(
            high_res, H, (frame.shape[1], frame.shape[0]),
            flags=cv2.INTER_LANCZOS4,
            borderMode=cv2.BORDER_TRANSPARENT,
        )

        if warped.shape[2] == 4:
            alpha = warped[:, :, 3].astype(np.float32) / 255.0
            alpha = alpha[:, :, np.newaxis]
            rgb   = warped[:, :, :3].astype(np.float32)
            frame_f = frame.astype(np.float32)
            blended = frame_f * (1.0 - alpha) + rgb * alpha
            return blended.clip(0, 255).astype(np.uint8)
        return warped