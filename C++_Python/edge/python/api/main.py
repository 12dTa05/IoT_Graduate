"""
FastAPI Backend for Edge Node
Provides REST API and WebSocket for dashboard
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import asyncio
import logging
from typing import List

from ..core.monitor import SystemMonitor
from ..core.mqtt_client import EdgeMQTTClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Edge Node API", version="1.0.0")

# CORS for ReactJS frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global instances (will be initialized in main_edge.py)
system_monitor: SystemMonitor = None
mqtt_client: EdgeMQTTClient = None

# WebSocket connections for real-time metadata
active_connections: List[WebSocket] = []


@app.get("/")
async def root():
    """Health check endpoint"""
    return {"status": "ok", "service": "edge_node"}


@app.get("/api/metrics")
async def get_metrics():
    """Get current system metrics"""
    if system_monitor is None:
        return JSONResponse({"error": "Monitor not initialized"}, status_code=503)
    
    metrics = system_monitor.get_metrics()
    return {
        "metrics": metrics,
        "load_level": system_monitor.get_load_level(),
        "should_offload": system_monitor.should_offload()
    }


@app.get("/api/status")
async def get_status():
    """Get edge node status"""
    return {
        "mqtt_connected": mqtt_client.connected if mqtt_client else False,
        "monitor_running": system_monitor._running if system_monitor else False,
        "pipeline_running": False  # TODO: Get from C++ pipeline
    }


@app.websocket("/ws/metadata")
async def websocket_metadata(websocket: WebSocket):
    """
    WebSocket endpoint for real-time metadata (bboxes, speed, license plates)
    Sends metadata at ~30 FPS for dashboard overlay
    """
    await websocket.accept()
    active_connections.append(websocket)
    
    try:
        while True:
            # TODO: Get metadata from C++ pipeline via shared memory
            # For now, send dummy data
            metadata = [
                {
                    "track_id": 1,
                    "x": 100,
                    "y": 200,
                    "width": 150,
                    "height": 200,
                    "speed": 65,
                    "plate": "29A-12345",
                    "class": "car"
                }
            ]
            
            await websocket.send_json(metadata)
            await asyncio.sleep(0.033)  # ~30 FPS
            
    except WebSocketDisconnect:
        active_connections.remove(websocket)
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if websocket in active_connections:
            active_connections.remove(websocket)


@app.post("/api/pipeline/start")
async def start_pipeline():
    """Start DeepStream pipeline"""
    # TODO: Call C++ pipeline start
    return {"status": "started"}


@app.post("/api/pipeline/stop")
async def stop_pipeline():
    """Stop DeepStream pipeline"""
    # TODO: Call C++ pipeline stop
    return {"status": "stopped"}


def set_system_monitor(monitor: SystemMonitor):
    """Set global system monitor instance"""
    global system_monitor
    system_monitor = monitor


def set_mqtt_client(client: EdgeMQTTClient):
    """Set global MQTT client instance"""
    global mqtt_client
    mqtt_client = client
