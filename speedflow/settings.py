
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

# --- Video / Model ---
VIDEO_FPS = 25.0
GPU_ID = 0
VEHICLE_CLASS_IDS = {2,3,6,8}   

# --- Paths ---
PATH_LOGS   = ROOT / "logs"
PATH_LOGS.mkdir(parents=True, exist_ok=True)

INFER_CONFIG = "/home/mta/deepstream_speed/DeepStream-Yolo/config_infer_primary_yolo11.txt"
TRACKER_CFG  = "/opt/nvidia/deepstream/deepstream-6.3/samples/configs/deepstream-app/config_tracker_nvDCF_perf.yml"
ANALYTICS_CFG= "/home/mta/deepstream_speed/configs/config_nvdsanalytics.txt"
HOMO_YML     = "/home/mta/deepstream_speed/configs/points_source_target.yml"
SPEED_LOG    = str(PATH_LOGS / "speed_log.csv")

# --- Overspeed config ---
SPEED_LIMIT_KMH = 60.0        
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