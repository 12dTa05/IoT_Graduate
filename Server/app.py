"""
Server/app.py — Central Monitoring Server.

Entry point for the Central Monitoring dashboard.

Usage:
    python -m Server.app          # as package (recommended)
    python Server/app.py          # standalone also works

Architecture:
  - aiohttp web server with REST API + WebSocket push
  - /ws/edge accepts inbound WebSocket connections FROM each Edge
  - EdgeRegistry tracks all connected Edges + live health
  - ViolationStore persists records to local filesystem
  - /ws/server pushes live events to browser dashboard clients
  - MediaMTX handles RTSP→WebRTC relay (separate container)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
from aiohttp import web, WSMsgType

# ---------------------------------------------------------------------------
# Path setup — allows both `python Server/app.py` and `python -m Server.app`
# ---------------------------------------------------------------------------
_SERVER_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SERVER_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(dotenv_path=_SERVER_DIR / ".env", override=False)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server_app")

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
from Server.edge_registry import EdgeRegistry     # noqa: E402
from Server.violation_store import ViolationStore  # noqa: E402


# ---------------------------------------------------------------------------
# App State
# ---------------------------------------------------------------------------

class ServerState:
    """Shared mutable state for the server application."""

    def __init__(self) -> None:
        self.registry: EdgeRegistry = None
        self.edge_ws: Dict[str, web.WebSocketResponse] = {}   # node_id → Edge WS
        self.browser_ws: List[web.WebSocketResponse] = []
        self.store: ViolationStore = None


# ---------------------------------------------------------------------------
# WebSocket broadcast to browsers
# ---------------------------------------------------------------------------

def _make_broadcast(state: ServerState):
    """Return a fire-and-forget broadcast function for the asyncio event loop."""

    async def _send_all(payload: str) -> None:
        dead: List[web.WebSocketResponse] = []
        for ws in list(state.browser_ws):
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
# Routes — static / REST
# ---------------------------------------------------------------------------

async def index(request: web.Request) -> web.Response:
    html_path = _SERVER_DIR / "static" / "index.html"
    if not html_path.exists():
        return web.Response(text="Dashboard not found", status=404)
    return web.FileResponse(html_path)


async def serve_static(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    filepath = _SERVER_DIR / "static" / filename
    if not filepath.exists() or not filepath.is_file():
        return web.Response(text="Not found", status=404)
    return web.FileResponse(filepath)


async def handle_edges(request: web.Request) -> web.Response:
    state: ServerState = request.app["state"]
    return web.json_response(state.registry.get_all())


async def handle_violations(request: web.Request) -> web.Response:
    state: ServerState = request.app["state"]
    node_id = request.query.get("node_id") or None
    date = request.query.get("date") or None
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    results = state.store.query(node_id=node_id, date=date, limit=limit)
    return web.json_response(results)


async def handle_snapshot(request: web.Request) -> web.Response:
    node_id = request.match_info["node_id"]
    filename = request.match_info["filename"]
    data_dir = _SERVER_DIR / os.getenv("DATA_DIR", "violations")
    if not data_dir.exists():
        return web.Response(text="No data", status=404)
    for date_dir in sorted(data_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        snap_path = date_dir / node_id / filename
        if snap_path.exists() and snap_path.is_file():
            return web.FileResponse(snap_path)
    return web.Response(text="Snapshot not found", status=404)


async def handle_health_check(request: web.Request) -> web.Response:
    state: ServerState = request.app["state"]
    return web.json_response({
        "status": "ok",
        "edges_registered": len(state.registry.get_all()),
        "edges_online": len(state.registry.get_online()),
        "browsers": len(state.browser_ws),
    })


async def handle_streams(request: web.Request) -> web.Response:
    """Proxy to MediaMTX API — list active RTSP streams."""
    mtx_api = os.getenv("MEDIAMTX_API", "http://localhost:9997")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{mtx_api}/v3/paths/list") as resp:
                data = await resp.json()
                return web.json_response(data)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)


# ---------------------------------------------------------------------------
# /ws/edge — Inbound WebSocket from Edge nodes
# ---------------------------------------------------------------------------

async def handle_ws_edge(request: web.Request) -> web.WebSocketResponse:
    """
    GET /ws/edge?node_id=jetson_A

    Edge nodes open this WebSocket on boot and push health + violation data.
    Registration is implicit: connecting == registering.
    The Edge's IP is obtained from the connection itself (request.remote).
    """
    state: ServerState = request.app["state"]
    broadcast = _make_broadcast(state)

    node_id = request.query.get("node_id", "").strip()
    advertise_ip = request.query.get("advertise_ip", "").strip()
    ip = advertise_ip or request.remote or ""

    if not node_id:
        return web.Response(text="node_id query param required", status=400)

    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)

    # Register (or re-register) the Edge
    state.registry.register(node_id, ip)

    # Track the live WS — replace before closing old so the old handler's
    # finally block sees it's no longer current and skips mark_offline.
    old_ws = state.edge_ws.get(node_id)
    state.edge_ws[node_id] = ws
    if old_ws and not old_ws.closed:
        await old_ws.close()

    logger.info("[EDGE-WS] '%s' connected (%s). Edges online: %d",
                node_id, ip, len(state.edge_ws))

    broadcast({
        "type": "edge_registered",
        "node_id": node_id,
        "ip": ip,
    })

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await _process_edge_message(state, broadcast, node_id, data)
                except json.JSONDecodeError:
                    pass
                except Exception as exc:
                    logger.warning("[EDGE-WS] '%s' message error: %s", node_id, exc)
            elif msg.type == WSMsgType.ERROR:
                logger.warning("[EDGE-WS] '%s' WS error: %s", node_id, ws.exception())
    finally:
        # Clean up — only if this WS is still the active one for this node_id.
        # If the Edge reconnected, a new WS replaced us; don't mark offline.
        is_current = state.edge_ws.get(node_id) is ws
        if is_current:
            state.edge_ws.pop(node_id, None)
            state.registry.mark_offline(node_id)
        logger.info("[EDGE-WS] '%s' disconnected (current=%s). Edges online: %d",
                    node_id, is_current, len(state.edge_ws))

    return ws


async def _process_edge_message(
    state: ServerState,
    broadcast,
    node_id: str,
    data: Dict[str, Any],
) -> None:
    """Route an incoming Edge message to the right handler."""
    msg_type = data.get("type", "")

    if msg_type == "health":
        state.registry.update_health(node_id, data)
        # Build broadcast payload — override type/node_id from data
        health_msg = {**data, "type": "health_update", "node_id": node_id}
        broadcast(health_msg)

    elif msg_type in ("violation", "overspeed"):
        # Copy before save() — save() mutates the dict (pops snapshot_b64)
        record = {**data, "type": "violation", "node_id": node_id}
        if "image_b64" in record:
            record["snapshot_b64"] = record.pop("image_b64")
        state.store.save(record)
        # Broadcast to browsers — override type, put node_id first
        violation_msg = {**data, "type": "violation", "node_id": node_id}
        violation_msg.pop("snapshot_b64", None)  # don't send b64 to browsers
        violation_msg.pop("image_b64", None)
        broadcast(violation_msg)


# ---------------------------------------------------------------------------
# /ws/server — Browser WebSocket for live push events
# ---------------------------------------------------------------------------

async def handle_ws_server(request: web.Request) -> web.WebSocketResponse:
    state: ServerState = request.app["state"]
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    state.browser_ws.append(ws)
    client_idx = len(state.browser_ws)
    logger.info("[WS] Browser #%d connected", client_idx)

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
                logger.warning("[WS] Browser #%d error: %s", client_idx, ws.exception())
    finally:
        if ws in state.browser_ws:
            state.browser_ws.remove(ws)
        logger.info("[WS] Browser #%d disconnected", client_idx)

    return ws


# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    state = ServerState()
    data_dir = os.getenv("DATA_DIR", "violations")
    state.store = ViolationStore(_SERVER_DIR / data_dir)

    def on_registry_change(event: str, node_id: str) -> None:
        # Only broadcast events not already handled by explicit broadcasts
        # in handle_ws_edge / _process_edge_message.
        # "offline" comes from the watchdog timer — needs broadcasting here.
        # "registered" / "health_updated" are already broadcast with full data.
        if event == "offline":
            broadcast = _make_broadcast(state)
            broadcast({"type": "edge_offline", "node_id": node_id})

    state.registry = EdgeRegistry(on_change=on_registry_change)

    app = web.Application()
    app["state"] = state

    async def on_startup(app: web.Application) -> None:
        state.registry.start_watchdog()
        logger.info("[Server] Watchdog started")

    async def on_shutdown(app: web.Application) -> None:
        for node_id, ws in list(state.edge_ws.items()):
            if not ws.closed:
                await ws.close()
        logger.info("[Server] All Edge connections closed")

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Routes
    app.router.add_get("/", index)
    app.router.add_get("/static/{filename}", serve_static)
    app.router.add_get("/api/edges", handle_edges)
    app.router.add_get("/api/violations", handle_violations)
    app.router.add_get("/api/streams", handle_streams)
    app.router.add_get("/api/snapshots/{node_id}/{filename}", handle_snapshot)
    app.router.add_get("/ws/edge", handle_ws_edge)
    app.router.add_get("/ws/server", handle_ws_server)
    app.router.add_get("/health", handle_health_check)

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
