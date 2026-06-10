from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web, WSMsgType

_SERVER_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SERVER_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(dotenv_path=_SERVER_DIR / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s \u2014 %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server_app")

from Server.edge_registry import EdgeRegistry
from Server.violation_store import ViolationStore


class ServerState:
    def __init__(self) -> None:
        self.registry: EdgeRegistry = None
        self.edge_ws: Dict[str, web.WebSocketResponse] = {}
        self.browser_ws: List[web.WebSocketResponse] = []
        self.store: ViolationStore = None
        self.http_session: aiohttp.ClientSession = None
        # BUG-05: keep a strong reference to the watchdog task so the GC
        # cannot collect it while it is still pending.
        self._watchdog_task: asyncio.Task = None

    def broadcast(self, msg: Dict[str, Any]) -> None:
        """Queue a JSON push to all connected browsers.

        BUG-F fix: create_task() raises RuntimeError when called outside a
        running event loop (e.g. tests, CLI tools).  Guard explicitly so
        callers in non-async contexts get a warning instead of a crash.
        """
        payload = json.dumps(msg, default=str)
        try:
            asyncio.get_running_loop()
            asyncio.create_task(self._send_all(payload))
        except RuntimeError:
            logger.warning("[ServerState] broadcast() called outside event loop — message dropped")

    async def _send_all(self, payload: str) -> None:
        dead: List[web.WebSocketResponse] = []
        for ws in list(self.browser_ws):
            try:
                await ws.send_str(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.browser_ws:
                self.browser_ws.remove(ws)


async def index(request: web.Request) -> web.Response:
    html_path = _SERVER_DIR / "static" / "index.html"
    if not html_path.exists():
        return web.Response(text="Dashboard not found", status=404)

    html = html_path.read_text(encoding="utf-8")
    # Derive WS scheme from the request scheme so WSS works behind TLS.
    ws_scheme = "wss" if request.secure else "ws"
    config_json = json.dumps({
        "MEDIAMTX_API": "/api/streams",  # always use the server-side proxy
        "MEDIAMTX_WEBRTC_URL": os.getenv("MEDIAMTX_WEBRTC_URL", ""),
        "WS_URL": f"{ws_scheme}://{request.host}/ws/server",
    })
    html = html.replace("<!-- SERVER_CONFIG -->",
                        f'<script id="server-config" type="application/json">{config_json}</script>')
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def serve_static(request: web.Request) -> web.Response:
    # BUG-03/12: Validate filename to prevent path traversal (../app.py etc.)
    from pathlib import PurePath
    filename = request.match_info["filename"]
    if PurePath(filename).name != filename:
        return web.Response(text="Invalid filename", status=400)
    filepath = _SERVER_DIR / "static" / filename
    # Resolve symlinks and ensure the result is still inside static/
    static_root = (_SERVER_DIR / "static").resolve()
    try:
        resolved = filepath.resolve()
        resolved.relative_to(static_root)  # raises ValueError if outside
    except (ValueError, OSError):
        return web.Response(text="Not found", status=404)
    if not resolved.exists() or not resolved.is_file():
        return web.Response(text="Not found", status=404)
    return web.FileResponse(resolved)


async def handle_edges(request: web.Request) -> web.Response:
    state: ServerState = request.app["state"]
    return web.json_response(state.registry.get_all())


async def handle_clusters(request: web.Request) -> web.Response:
    state: ServerState = request.app["state"]
    return web.json_response(state.registry.get_clusters())


async def handle_violations(request: web.Request) -> web.Response:
    state: ServerState = request.app["state"]
    node_id = request.query.get("node_id") or None
    date = request.query.get("date") or None
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    try:
        page = int(request.query.get("page", "0"))
    except ValueError:
        page = 0
    offset = page * limit
    # BUG-8 fix: use query_async() (runs in a thread via asyncio.to_thread)
    # instead of the synchronous query() which blocked the event loop and
    # stalled WebSocket heartbeats during large JSONL reads.
    results = await state.store.query_async(node_id=node_id, date=date, limit=limit, offset=offset)
    return web.json_response(results)


async def handle_snapshot(request: web.Request) -> web.Response:
    node_id = request.match_info["node_id"]
    filename = request.match_info["filename"]

    from pathlib import PurePath
    if PurePath(node_id).name != node_id:
        return web.Response(text="Invalid node_id", status=400)
    if PurePath(filename).name != filename:
        return web.Response(text="Invalid filename", status=400)

    data_dir = _SERVER_DIR / os.getenv("DATA_DIR", "violations")
    if not data_dir.exists():
        return web.Response(text="No data", status=404)

    # BUG-08: use asyncio.to_thread to avoid blocking the event loop while
    # iterating potentially hundreds of date directories synchronously.
    def _find_snapshot() -> Optional[Path]:
        for date_dir in sorted(data_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            snap_path = date_dir / node_id / filename
            if snap_path.exists() and snap_path.is_file():
                return snap_path
        return None

    snap_path = await asyncio.to_thread(_find_snapshot)
    if snap_path is None:
        return web.Response(text="Snapshot not found", status=404)
    return web.FileResponse(snap_path)


async def handle_health_check(request: web.Request) -> web.Response:
    state: ServerState = request.app["state"]
    return web.json_response({
        "status": "ok",
        "edges_registered": len(state.registry.get_all()),
        "edges_online": len(state.registry.get_online()),
        "browsers": len(state.browser_ws),
    })


async def handle_streams(request: web.Request) -> web.Response:
    mtx_api = os.getenv("MEDIAMTX_API", "http://localhost:9997")
    state: ServerState = request.app["state"]
    try:
        async with state.http_session.get(f"{mtx_api}/v3/paths/list") as resp:
            data = await resp.json()
            return web.json_response(data)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)


async def handle_ws_edge(request: web.Request) -> web.WebSocketResponse:
    state: ServerState = request.app["state"]

    node_id = request.query.get("node_id", "").strip()
    advertise_ip = request.query.get("advertise_ip", "").strip()
    ip = advertise_ip or request.remote or ""

    if not node_id:
        return web.Response(text="node_id query param required", status=400)

    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)

    # BUG-C/D fix: close the old WebSocket BEFORE registering the new one.
    # Closing first ensures the old connection's finally-block (mark_offline)
    # runs and completes before register() sets online=True.  This prevents
    # the race where the old WS's finally fires after the new registration,
    # re-marking a freshly connected edge as offline.
    old_ws = state.edge_ws.pop(node_id, None)
    if old_ws and not old_ws.closed:
        await old_ws.close()

    state.registry.register(node_id, ip)
    state.edge_ws[node_id] = ws

    logger.info("[EDGE-WS] '%s' connected (%s). Edges online: %d",
                node_id, ip, len(state.edge_ws))

    state.broadcast({
        "type": "edge_registered",
        "node_id": node_id,
        "ip": ip,
    })

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await _process_edge_message(state, node_id, data)
                except json.JSONDecodeError:
                    pass
                except Exception as exc:
                    logger.warning("[EDGE-WS] '%s' message error: %s", node_id, exc)
            elif msg.type == WSMsgType.ERROR:
                logger.warning("[EDGE-WS] '%s' WS error: %s", node_id, ws.exception())
    finally:
        is_current = state.edge_ws.get(node_id) is ws
        if is_current:
            state.edge_ws.pop(node_id, None)
            state.registry.mark_offline(node_id)
        logger.info("[EDGE-WS] '%s' disconnected (current=%s). Edges online: %d",
                    node_id, is_current, len(state.edge_ws))

    return ws


async def _process_edge_message(
    state: ServerState,
    node_id: str,
    data: Dict[str, Any],
) -> None:
    msg_type = data.get("type", "")

    if msg_type == "health":
        # Log every health payload at DEBUG level so you can inspect what
        # each edge is sending.  Run the server with DEBUG logging to see it:
        #   python3 app.py --log-level debug
        # or set the env var: LOG_LEVEL=DEBUG python3 app.py
        logger.debug(
            "[EDGE-HEALTH] '%s' → load=%.1f%% gpu=%.1f%% cpu=%.1f%% "
            "ram=%.1f%% temp=%.1f°C power=%.0fmW fps=%s active=%s source=%s",
            node_id,
            data.get("load_score", 0),
            data.get("gpu_percent", 0),
            data.get("cpu_percent", 0),
            data.get("ram_percent", 0),
            data.get("gpu_temp_c", 0),
            data.get("power_mw", 0),
            data.get("pipeline", {}).get("fps_per_camera", {}),
            data.get("pipeline", {}).get("active_cameras", []),
            data.get("source", "?"),
        )
        state.registry.update_health(node_id, data)
        health_msg = {**data, "type": "health_update", "node_id": node_id}
        state.broadcast(health_msg)

    elif msg_type in ("violation", "overspeed"):
        record = {**data, "type": "violation", "node_id": node_id}
        if "image_b64" in record:
            record["snapshot_b64"] = record.pop("image_b64")
        asyncio.create_task(state.store.save_async(record))
        violation_msg = {k: v for k, v in record.items() if k != "snapshot_b64"}
        state.broadcast(violation_msg)


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
            "clusters": state.registry.get_clusters(),
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
                    elif data.get("action") == "ping":
                        pass
                except json.JSONDecodeError:
                    pass
            elif msg.type == WSMsgType.ERROR:
                logger.warning("[WS] Browser #%d error: %s", client_idx, ws.exception())
    finally:
        if ws in state.browser_ws:
            state.browser_ws.remove(ws)
        logger.info("[WS] Browser #%d disconnected", client_idx)

    return ws


def create_app() -> web.Application:
    state = ServerState()
    data_dir = os.getenv("DATA_DIR", "violations")
    state.store = ViolationStore(_SERVER_DIR / data_dir)

    def on_registry_change(event: str, node_id: str) -> None:
        if event == "offline":
            state.broadcast({"type": "edge_offline", "node_id": node_id})

    state.registry = EdgeRegistry(on_change=on_registry_change)

    app = web.Application()
    app["state"] = state

    async def on_startup(app: web.Application) -> None:
        state.http_session = aiohttp.ClientSession()
        # BUG-05: store the task reference so GC cannot collect a pending task
        state._watchdog_task = state.registry.start_watchdog()
        logger.info("[Server] Watchdog started, HTTP session created")

    async def on_shutdown(app: web.Application) -> None:
        if state._watchdog_task and not state._watchdog_task.done():
            state._watchdog_task.cancel()
        if state.http_session and not state.http_session.closed:
            await state.http_session.close()
        for node_id, ws in list(state.edge_ws.items()):
            if not ws.closed:
                await ws.close()
        logger.info("[Server] All connections closed")

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    app.router.add_get("/", index)
    app.router.add_get("/static/{filename}", serve_static)
    app.router.add_get("/api/edges", handle_edges)
    app.router.add_get("/api/clusters", handle_clusters)
    app.router.add_get("/api/violations", handle_violations)
    app.router.add_get("/api/streams", handle_streams)
    app.router.add_get("/api/snapshots/{node_id}/{filename}", handle_snapshot)
    app.router.add_get("/ws/edge", handle_ws_edge)
    app.router.add_get("/ws/server", handle_ws_server)
    app.router.add_get("/health", handle_health_check)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="IoT Graduate \u2014 Central Monitoring Server")
    parser.add_argument("--host", default=os.getenv("SERVER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SERVER_PORT", "9090")))
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO").upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity. Use DEBUG to see every health payload from each edge.",
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    logger.info("=" * 55)
    logger.info("  IoT Graduate \u2014 Central Monitoring Server")
    logger.info("  Dashboard: http://%s:%d", args.host, args.port)
    logger.info("  Health:    http://%s:%d/health", args.host, args.port)
    logger.info("  Data dir:  %s", os.getenv("DATA_DIR", "violations"))
    logger.info("=" * 55)

    app = create_app()

    async def _start() -> None:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port, reuse_address=True)
        await site.start()
        logger.info("Server listening on %s:%d (SO_REUSEADDR)", args.host, args.port)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
        await runner.cleanup()

    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
