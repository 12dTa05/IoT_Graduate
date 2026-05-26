"""
Server/webrtc_client.py — Per-Edge WebSocket data channel client.

Connects to each Edge's signaling server WebSocket (ws://<ip>:<port>/ws?role=server).
Receives health + violation data pushed by the Edge over the same WebSocket.

Reconnects with exponential backoff on disconnect.

Naming note: "WebRTC" because the data travels over the same signaling
connection used for WebRTC negotiation on the Edge side — the Edge's
signaling.py forwards internal data to this channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional

import aiohttp

from .edge_registry import EdgeRegistry
from .violation_store import ViolationStore

logger = logging.getLogger("webrtc_client")

_RECONNECT_DELAYS = [2.0, 5.0, 10.0, 30.0]


class EdgeWebRTCClient:
    """
    Connects to one Edge's signaling WebSocket for data reception.

    Args:
        node_id: Edge node identifier
        ip: Edge IP address
        port: Edge signaling port (typically 8080)
        registry: Shared edge registry to update health data
        store: ViolationStore to persist violation records
        broadcast_fn: Callable(msg_dict) to push live events to browser WS clients
    """

    def __init__(
        self,
        node_id: str,
        ip: str,
        port: int,
        registry: EdgeRegistry,
        store: ViolationStore,
        broadcast_fn: Callable[[Dict[str, Any]], None],
    ) -> None:
        self._node_id = node_id
        self._ip = ip
        self._port = port
        self._registry = registry
        self._store = store
        self._broadcast = broadcast_fn

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Connection loop with reconnection
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        delay_idx = 0
        self._session = aiohttp.ClientSession()

        while self._running:
            try:
                ws_url = f"ws://{self._ip}:{self._port}/ws?role=server"
                logger.info("[%s] Connecting to %s", self._node_id, ws_url)

                async with self._session.ws_connect(
                    ws_url,
                    heartbeat=10.0,
                    close_timeout=5.0,
                ) as ws:
                    self._ws = ws
                    delay_idx = 0  # Reset backoff on successful connect
                    logger.info("[%s] Data channel connected", self._node_id)
                    await self._handle_messages(ws)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                delay = _RECONNECT_DELAYS[min(delay_idx, len(_RECONNECT_DELAYS) - 1)]
                logger.warning(
                    "[%s] Connection error: %s — reconnecting in %.1fs",
                    self._node_id, exc, delay,
                )
                delay_idx += 1
                await asyncio.sleep(delay)

    async def _handle_messages(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Read messages from the WebSocket until closed."""
        async for msg in ws:
            if not self._running:
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._process_message(data)
                except json.JSONDecodeError:
                    logger.debug("[%s] Bad JSON from data channel", self._node_id)
                except Exception as exc:
                    logger.warning("[%s] Error processing message: %s", self._node_id, exc)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    async def _process_message(self, data: Dict[str, Any]) -> None:
        """Route incoming data to the right handler."""
        msg_type = data.get("type", "")

        if msg_type == "health":
            self._registry.update_health(self._node_id, data)
            self._broadcast({
                "type": "health_update",
                "node_id": self._node_id,
                **data,
            })

        elif msg_type in ("violation", "overspeed"):
            # Normalize: SpeedProbe sends "overspeed" with image_b64
            data["type"] = "violation"
            if "image_b64" in data:
                data["snapshot_b64"] = data.pop("image_b64")
            self._store.save(data)
            self._broadcast({
                "type": "violation",
                "node_id": self._node_id,
                **data,
            })

        else:
            logger.debug("[%s] Unknown message type: %s", self._node_id, msg_type)
