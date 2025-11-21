"""
Enhanced FastAPI with WebRTC signaling and metadata streaming
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import asyncio
import logging
from typing import List, Dict
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Edge Node API", version="1.0.0")

# CORS for ReactJS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global instances (will be set by main_edge.py)
system_monitor = None
mqtt_client = None
shared_memory = None
signaling_server = None

# WebSocket connections
active_connections: List[WebSocket] = []
latest_metadata: List[Dict] = []


@app.get("/")
async def root():
    """Health check"""
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
        "pipeline_running": True,
        "active_websockets": len(active_connections),
        "webrtc_clients": len(signaling_server.clients) if signaling_server else 0
    }


@app.websocket("/ws/signaling")
async def websocket_signaling(websocket: WebSocket):
    """
    WebSocket for WebRTC signaling (SDP offer/answer, ICE candidates)
    """
    if signaling_server is None:
        await websocket.close(code=1011, reason="Signaling server not initialized")
        return
    
    import uuid
    client_id = str(uuid.uuid4())
    
    await signaling_server.handle_client(websocket, client_id)


@app.websocket("/ws/metadata")
async def websocket_metadata(websocket: WebSocket):
    """
    WebSocket for real-time metadata streaming
    """
    await websocket.accept()
    active_connections.append(websocket)
    logger.info(f"Metadata client connected (total: {len(active_connections)})")
    
    try:
        while True:
            # Read metadata from shared memory
            if shared_memory:
                metadata = shared_memory.read_metadata()
                if metadata:
                    latest_metadata.clear()
                    latest_metadata.extend(metadata)
            
            # Send to client
            await websocket.send_json(latest_metadata if latest_metadata else [])
            
            # 30 FPS
            await asyncio.sleep(0.033)
            
    except WebSocketDisconnect:
        active_connections.remove(websocket)
        logger.info(f"Metadata client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if websocket in active_connections:
            active_connections.remove(websocket)


@app.post("/api/pipeline/start")
async def start_pipeline():
    """Start DeepStream pipeline"""
    return {"status": "started"}


@app.post("/api/pipeline/stop")
async def stop_pipeline():
    """Stop DeepStream pipeline"""
    return {"status": "stopped"}


def set_system_monitor(monitor):
    """Set global system monitor instance"""
    global system_monitor
    system_monitor = monitor


def set_mqtt_client(client):
    """Set global MQTT client instance"""
    global mqtt_client
    mqtt_client = client


def set_shared_memory(shm):
    """Set global shared memory instance"""
    global shared_memory
    shared_memory = shm


def set_signaling_server(server):
    """Set global signaling server instance"""
    global signaling_server
    signaling_server = server
