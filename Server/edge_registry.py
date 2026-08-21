from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("edge_registry")

# Must match Edge/configs/edge_node.yml peer.heartbeat_timeout_s.
HEARTBEAT_TIMEOUT = 10.0
WATCHDOG_INTERVAL = 5.0


class EdgeInfo:
    def __init__(self, node_id: str, ip: str) -> None:
        self.node_id = node_id
        self.ip = ip
        self.online = True
        self.last_heartbeat = time.time()
        self.health: Dict[str, Any] = {}
        self.registered_at = time.time()

    @property
    def cluster_id(self) -> str:
        cluster = self.health.get("cluster_id")
        if cluster:
            return str(cluster)
        parts = self.ip.rsplit(".", 1)
        if len(parts) == 2:
            return parts[0]
        return self.ip or "default"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "ip": self.ip,
            "online": self.online,
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
            "health": self.health,
            "cluster_id": self.cluster_id,
        }


class EdgeRegistry:
    def __init__(self, on_change: Optional[Callable[[str, str], None]] = None) -> None:
        self._edges: Dict[str, EdgeInfo] = {}
        self._on_change = on_change
        self._lock = threading.Lock()

    def register(self, node_id: str, ip: str) -> bool:
        with self._lock:
            existing = self._edges.get(node_id)
            if existing:
                if ip:
                    existing.ip = ip
                existing.online = True
                existing.last_heartbeat = time.time()
                logger.info("[Registry] Edge '%s' re-registered at %s", node_id, existing.ip)
                return False
            self._edges[node_id] = EdgeInfo(node_id, ip)
            logger.info("[Registry] Edge '%s' registered at %s", node_id, ip)
        self._emit("registered", node_id)
        return True

    def update_health(self, node_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            info = self._edges.get(node_id)
            if not info:
                # Auto-register if not yet registered
                ip = payload.get("advertise_ip", "")
                self._edges[node_id] = EdgeInfo(node_id, ip)
                info = self._edges[node_id]
                logger.info("[Registry] Edge '%s' auto-registered via status", node_id)
            info.online = True
            info.last_heartbeat = time.time()
            if payload.get("advertise_ip"):
                info.ip = payload["advertise_ip"]
            health = {k: v for k, v in payload.items() if k not in ("type", "node_id")}
            info.health = health
        self._emit("health_updated", node_id)

    def mark_offline(self, node_id: str) -> None:
        with self._lock:
            info = self._edges.get(node_id)
            if not info or not info.online:
                return
            info.online = False
        logger.info("[Registry] Edge '%s' marked offline", node_id)
        self._emit("offline", node_id)

    def get_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [info.to_dict() for info in self._edges.values()]

    def get_online(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [info.to_dict() for info in self._edges.values() if info.online]

    def get_clusters(self) -> Dict[str, List[Dict[str, Any]]]:
        clusters: Dict[str, List[Dict[str, Any]]] = {}
        with self._lock:
            for info in self._edges.values():
                cid = info.cluster_id
                if cid not in clusters:
                    clusters[cid] = []
                clusters[cid].append(info.to_dict())
        return clusters

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            now = time.time()
            offline_candidates = []
            with self._lock:
                for node_id, info in self._edges.items():
                    if info.online and (now - info.last_heartbeat) > HEARTBEAT_TIMEOUT:
                        offline_candidates.append(node_id)
            for node_id in offline_candidates:
                self.mark_offline(node_id)

    def start_watchdog(self) -> asyncio.Task:
        return asyncio.create_task(self._watchdog_loop())

    def _emit(self, event: str, node_id: str) -> None:
        if self._on_change:
            try:
                self._on_change(event, node_id)
            except Exception as exc:
                logger.warning("[Registry] on_change callback error: %s", exc)
