"""
FastAPI Backend for Center Server
Provides REST API and WebSocket for multi-camera dashboard
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import asyncio
import logging
from typing import List, Dict

from ..core.mqtt_broker import CenterMQTTBroker
from ..core.balancer import LoadBalancer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Center Server API", version="1.0.0")

# CORS for ReactJS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global instances
mqtt_broker: CenterMQTTBroker = None
load_balancer: LoadBalancer = None

# WebSocket connections
active_connections: List[WebSocket] = []

# Store alerts history
alerts_history: List[Dict] = []


@app.get("/")
async def root():
    """Health check"""
    return {"status": "ok", "service": "center_server"}


@app.get("/api/edges")
async def get_edges():
    """Get all edge nodes with their metrics"""
    if mqtt_broker is None:
        return JSONResponse({"error": "MQTT not initialized"}, status_code=503)
    
    all_metrics = mqtt_broker.get_all_metrics()
    
    edges = []
    for edge_id, metrics in all_metrics.items():
        edges.append({
            "id": edge_id,
            "metrics": metrics,
            "status": "online" if metrics else "offline"
        })
    
    return {"edges": edges}


@app.get("/api/edges/{edge_id}")
async def get_edge_detail(edge_id: str):
    """Get detailed info for specific edge"""
    if mqtt_broker is None:
        return JSONResponse({"error": "MQTT not initialized"}, status_code=503)
    
    metrics = mqtt_broker.get_edge_metrics(edge_id)
    
    if not metrics:
        return JSONResponse({"error": "Edge not found"}, status_code=404)
    
    return {
        "edge_id": edge_id,
        "metrics": metrics,
        "status": "online"
    }


@app.get("/api/balancer/status")
async def get_balancer_status():
    """Get load balancer status"""
    if load_balancer is None:
        return JSONResponse({"error": "Balancer not initialized"}, status_code=503)
    
    return load_balancer.get_offload_status()


@app.post("/api/balancer/offload")
async def manual_offload(source_edge: str, target_edge: str):
    """Manually trigger offload"""
    if load_balancer is None:
        return JSONResponse({"error": "Balancer not initialized"}, status_code=503)
    
    load_balancer.manual_offload(source_edge, target_edge)
    
    return {"status": "offload_started", "source": source_edge, "target": target_edge}


@app.post("/api/balancer/stop/{edge_id}")
async def stop_offload(edge_id: str):
    """Stop offloading from edge"""
    if load_balancer is None:
        return JSONResponse({"error": "Balancer not initialized"}, status_code=503)
    
    load_balancer.stop_offload(edge_id)
    
    return {"status": "offload_stopped", "edge_id": edge_id}


@app.get("/api/alerts")
async def get_alerts(limit: int = 50):
    """Get recent alerts"""
    return {"alerts": alerts_history[-limit:]}


@app.websocket("/ws/metrics")
async def websocket_metrics(websocket: WebSocket):
    """
    WebSocket for real-time metrics from all edges
    Sends updates every second
    """
    await websocket.accept()
    active_connections.append(websocket)
    
    try:
        while True:
            if mqtt_broker:
                all_metrics = mqtt_broker.get_all_metrics()
                balancer_status = load_balancer.get_offload_status() if load_balancer else {}
                
                await websocket.send_json({
                    "edges": all_metrics,
                    "balancer": balancer_status
                })
            
            await asyncio.sleep(1.0)
            
    except WebSocketDisconnect:
        active_connections.remove(websocket)
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if websocket in active_connections:
            active_connections.remove(websocket)


def set_mqtt_broker(broker: CenterMQTTBroker):
    """Set global MQTT broker instance"""
    global mqtt_broker
    mqtt_broker = broker


def set_load_balancer(balancer: LoadBalancer):
    """Set global load balancer instance"""
    global load_balancer
    load_balancer = balancer


def add_alert(edge_id: str, alert_data: Dict):
    """Add alert to history"""
    alerts_history.append({
        "edge_id": edge_id,
        **alert_data
    })
    
    # Keep only last 1000 alerts
    if len(alerts_history) > 1000:
        alerts_history.pop(0)
