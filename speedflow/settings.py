import os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

# --- Video / Model ---
VIDEO_FPS = float(os.getenv("VIDEO_FPS", "25.0"))
GPU_ID = int(os.getenv("GPU_ID", "0"))
VEHICLE_CLASS_IDS = {2,3,6,8}   

# --- Paths ---
PATH_LOGS   = ROOT / "logs"
PATH_LOGS.mkdir(parents=True, exist_ok=True)

# Use environment variables with fallback to container paths
INFER_CONFIG = os.getenv("INFER_CONFIG", "/app/DeepStream-YoLo/config_infer_primary_yolo11.txt")
TRACKER_CFG  = os.getenv("TRACKER_CFG", "/opt/nvidia/deepstream/deepstream/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml")
ANALYTICS_CFG= os.getenv("ANALYTICS_CFG", "/app/configs/config_nvdsanalytics.txt")
HOMO_YML     = os.getenv("HOMO_YML", "/app/configs/points_source_target.yml")
SPEED_LOG    = str(PATH_LOGS / "speed_log.csv")

# --- Muxer resolution ---
MUX_WIDTH  = int(os.getenv("MUX_WIDTH", "1920"))
MUX_HEIGHT = int(os.getenv("MUX_HEIGHT", "1080"))

# --- Overspeed config ---
SPEED_LIMIT_KMH = float(os.getenv("SPEED_LIMIT", "60.0"))
JPEG_QUALITY    = 100       
SNAP_DIR        = PATH_LOGS / "overspeed_snaps"
SNAP_DIR.mkdir(parents=True, exist_ok=True)
MAX_SNAPSHOT_PER_ID = 1        

MIN_TRACK_AGE_FRAMES = int(VIDEO_FPS * 0.5)  
MIN_WORLD_DISPL_M    = 0.5                   
MAX_ABS_KMH          = 160.0                 
BBOX_AREA_JUMP       = 2.5                  
MIN_DET_CONF         = 0.45                  
MEDIAN_WINDOW        = 5