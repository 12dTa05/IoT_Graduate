"""
Server/edge_registry.py — Tracks registered Edge nodes + live health state.

Each Edge registers implicitly by opening a WebSocket to /ws/edge on startup.
Health updates arrive via the same WebSocket (type: "health" messages).
A background watchdog marks nodes offline after 15s of no heartbeat.

Events emitted via `on_change` callback:
  on_change("registered", node_id)
  on_change("health_updated", node_id)
  on_change("offline", node_id)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("edge_registry")

HEARTBEAT_TIMEOUT = 15.0  # seconds without health update → offline
WATCHDOG_INTERVAL = 5.0


class EdgeInfo:
    def __init__(self, node_id: str, ip: str) -> None:
        self.node_id = node_id
        self.ip = ip
        self.online = True
        self.last_heartbeat = time.time()
        self.health: Dict[str, Any] = {}
        self.registered_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "ip": self.ip,
            "online": self.online,
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
            "health": self.health,
        }


class EdgeRegistry:
    def __init__(self, on_change: Optional[Callable[[str, str], None]] = None) -> None:
        self._edges: Dict[str, EdgeInfo] = {}
        self._on_change = on_change

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, node_id: str, ip: str) -> bool:
        """Register or update an Edge. Returns True if newly registered."""
        existing = self._edges.get(node_id)
        if existing:
            existing.ip = ip
            existing.online = True
            existing.last_heartbeat = time.time()
            logger.info("[Registry] Edge '%s' re-registered at %s", node_id, ip)
            return False
        self._edges[node_id] = EdgeInfo(node_id, ip)
        logger.info("[Registry] Edge '%s' registered at %s", node_id, ip)
        self._emit("registered", node_id)
        return True

    def update_health(self, node_id: str, payload: Dict[str, Any]) -> None:
        """Update live health data from a heartbeat message."""
        info = self._edges.get(node_id)
        if not info:
            return
        info.online = True
        info.last_heartbeat = time.time()
        # Store health fields only (strip message routing keys)
        health = {k: v for k, v in payload.items() if k not in ("type", "node_id")}
        info.health = health
        self._emit("health_updated", node_id)

    def mark_offline(self, node_id: str) -> None:
        """Mark an Edge as offline."""
        info = self._edges.get(node_id)
        if not info or not info.online:
            return
        info.online = False
        logger.info("[Registry] Edge '%s' marked offline", node_id)
        self._emit("offline", node_id)

    def get(self, node_id: str) -> Optional[EdgeInfo]:
        return self._edges.get(node_id)

    def get_all(self) -> List[Dict[str, Any]]:
        return [info.to_dict() for info in self._edges.values()]

    def get_online(self) -> List[Dict[str, Any]]:
        return [info.to_dict() for info in self._edges.values() if info.online]

    def get_cluster(self) -> List[str]:
        """One cluster = all currently online nodes."""
        return [info.node_id for info in self._edges.values() if info.online]

    # ------------------------------------------------------------------
    # Heartbeat Watchdog
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Background coroutine: check heartbeats and mark stale nodes offline."""
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            now = time.time()
            for node_id, info in list(self._edges.items()):
                if info.online and (now - info.last_heartbeat) > HEARTBEAT_TIMEOUT:
                    self.mark_offline(node_id)

    def start_watchdog(self) -> asyncio.Task:
        return asyncio.create_task(self._watchdog_loop())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit(self, event: str, node_id: str) -> None:
        if self._on_change:
            try:
                self._on_change(event, node_id)
            except Exception as exc:
                logger.warning("[Registry] on_change callback error: %s", exc)
