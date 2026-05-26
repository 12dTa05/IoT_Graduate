"""
Server/app.py — Central Monitoring Server.

Entry point for the Central Monitoring dashboard.

Usage:
    python Server/app.py
    # Opens http://localhost:9090

Architecture:
  - aiohttp web server with REST API + WebSocket push
  - EdgeRegistry tracks all connected Edges + live health
  - EdgeWebRTCClient connects per-Edge for data reception
  - ViolationStore persists records to local filesystem
  - /ws/server pushes live events to browser dashboard clients
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web, WSMsgType

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server_app")

# Load .env
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)


# ---------------------------------------------------------------------------
# Imports (late — after .env loaded)
# ---------------------------------------------------------------------------

def _imports():
    global EdgeRegistry, EdgeWebRTCClient, ViolationStore
    from .edge_registry import EdgeRegistry
    from .webrtc_client import EdgeWebRTCClient
    from .violation_store import ViolationStore


# ---------------------------------------------------------------------------
# App State
# ---------------------------------------------------------------------------

class ServerState:
    """Shared mutable state for the server application."""

    def __init__(self) -> None:
        self.registry: EdgeRegistry = None
        self.clients: Dict[str, EdgeWebRTCClient] = {}
        self.browser_ws: List[web.WebSocketResponse] = []
        self.store: ViolationStore = None


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------

def _make_broadcast(state: ServerState):
    """Return a broadcast function that pushes to all browser WS clients."""

    async def _send_all(payload: str) -> None:
        dead: List[web.WebSocketResponse] = []
        for ws in state.browser_ws:
            try:
                await ws.send_str(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in state.browser_ws:
                state.browser_ws.remove(ws)

    def broadcast(msg: Dict[str, Any]) -> None:
        payload = json.dumps(msg, default=str)
        asyncio.create_task(_send_all(payload))

    return broadcast


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def index(request: web.Request) -> web.Response:
    """Serve dashboard HTML."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if not html_path.exists():
        return web.Response(text="Dashboard not found", status=404)
    return web.FileResponse(html_path)


async def serve_static(request: web.Request) -> web.Response:
    """Serve static files under /static/."""
    filename = request.match_info["filename"]
    filepath = Path(__file__).parent / "static" / filename
    if not filepath.exists() or not filepath.is_file():
        return web.Response(text="Not found", status=404)
    return web.FileResponse(filepath)


async def handle_register(request: web.Request) -> web.Response:
    """
    POST /api/register
    Body: {"node_id": "jetson_A", "ip": "192.168.1.100", "signaling_port": 8080}

    Registers the Edge and starts a WebRTC data channel connection.
    """
    state: ServerState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    node_id = body.get("node_id", "").strip()
    ip = body.get("ip", "").strip()
    signaling_port = body.get("signaling_port", 8080)

    if not node_id or not ip:
        return web.json_response(
            {"error": "node_id and ip are required"}, status=400
        )

    was_new = state.registry.register(node_id, ip, signaling_port)

    # Start/restart WebRTC client for this Edge
    existing = state.clients.get(node_id)
    if existing:
        await existing.stop()

    client = EdgeWebRTCClient(
        node_id=node_id,
        ip=ip,
        port=signaling_port,
        registry=state.registry,
        store=state.store,
        broadcast_fn=_make_broadcast(state),
    )
    client.start()
    state.clients[node_id] = client

    logger.info("[API] Edge '%s' registered (%s:%d) — new=%s", node_id, ip, signaling_port, was_new)

    broadcast = _make_broadcast(state)
    broadcast({
        "type": "edge_registered",
        "node_id": node_id,
        "ip": ip,
        "signaling_port": signaling_port,
    })

    return web.json_response({"status": "ok", "node_id": node_id, "new": was_new})


async def handle_edges(request: web.Request) -> web.Response:
    """GET /api/edges — return all registered edges with live health."""
    state: ServerState = request.app["state"]
    return web.json_response(state.registry.get_all())


async def handle_violations(request: web.Request) -> web.Response:
    """GET /api/violations — query violation records."""
    state: ServerState = request.app["state"]
    node_id = request.query.get("node_id") or None
    date = request.query.get("date") or None
    limit = int(request.query.get("limit", "50"))
    results = state.store.query(node_id=node_id, date=date, limit=limit)
    return web.json_response(results)


async def handle_snapshot(request: web.Request) -> web.Response:
    """GET /api/snapshots/{node_id}/{filename} — serve snapshot image."""
    state: ServerState = request.app["state"]
    node_id = request.match_info["node_id"]
    filename = request.match_info["filename"]

    # Search in all date dirs
    data_dir = Path(__file__).parent / os.getenv("DATA_DIR", "violations")
    if not data_dir.exists():
        return web.Response(text="No data", status=404)

    for date_dir in sorted(data_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        snap_path = date_dir / node_id / filename
        if snap_path.exists() and snap_path.is_file():
            return web.FileResponse(snap_path)

    return web.Response(text="Snapshot not found", status=404)


async def handle_ws_server(request: web.Request) -> web.WebSocketResponse:
    """
    GET /ws/server — Browser WebSocket for live push events.

    Browsers connect here to receive real-time health updates and violations.
    """
    state: ServerState = request.app["state"]
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    state.browser_ws.append(ws)
    client_idx = len(state.browser_ws)
    logger.info("[WS] Browser client #%d connected", client_idx)

    # Send initial state
    try:
        await ws.send_str(json.dumps({
            "type": "init",
            "edges": state.registry.get_all(),
        }))
    except Exception:
        pass

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("action") == "list_edges":
                        await ws.send_str(json.dumps({
                            "type": "edge_list",
                            "edges": state.registry.get_all(),
                        }))
                except json.JSONDecodeError:
                    pass
            elif msg.type == WSMsgType.ERROR:
                logger.warning("[WS] Browser client #%d error: %s", client_idx, ws.exception())
    finally:
        if ws in state.browser_ws:
            state.browser_ws.remove(ws)
        logger.info("[WS] Browser client #%d disconnected", client_idx)

    return ws


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — server health check."""
    state: ServerState = request.app["state"]
    return web.json_response({
        "status": "ok",
        "edges_registered": len(state.registry.get_all()),
        "edges_online": len(state.registry.get_online()),
        "browsers": len(state.browser_ws),
    })


# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    _imports()

    state = ServerState()
    data_dir = os.getenv("DATA_DIR", "violations")
    state.store = ViolationStore(Path(__file__).parent / data_dir)

    # Registry with broadcast on change
    def on_registry_change(event: str, node_id: str) -> None:
        broadcast = _make_broadcast(state)
        broadcast({"type": f"edge_{event}", "node_id": node_id})

    state.registry = EdgeRegistry(on_change=on_registry_change)
    state.registry.start_watchdog()

    app = web.Application()
    app["state"] = state

    # Routes
    app.router.add_get("/", index)
    app.router.add_get("/static/{filename}", serve_static)
    app.router.add_post("/api/register", handle_register)
    app.router.add_get("/api/edges", handle_edges)
    app.router.add_get("/api/violations", handle_violations)
    app.router.add_get("/api/snapshots/{node_id}/{filename}", handle_snapshot)
    app.router.add_get("/ws/server", handle_ws_server)
    app.router.add_get("/health", handle_health)

    # Cleanup on shutdown
    async def on_shutdown(app: web.Application) -> None:
        for node_id, client in state.clients.items():
            await client.stop()
        logger.info("[Server] All Edge clients stopped")

    app.on_shutdown.append(on_shutdown)

    return app


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="IoT Graduate — Central Monitoring Server")
    parser.add_argument("--host", default=os.getenv("SERVER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SERVER_PORT", "9090")))
    args = parser.parse_args()

    logger.info("=" * 55)
    logger.info("  IoT Graduate — Central Monitoring Server")
    logger.info("  Dashboard: http://%s:%d", args.host, args.port)
    logger.info("  Health:    http://%s:%d/health", args.host, args.port)
    logger.info("  Data dir:  %s", os.getenv("DATA_DIR", "violations"))
    logger.info("=" * 55)

    app = create_app()
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
