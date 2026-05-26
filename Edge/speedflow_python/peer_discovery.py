"""
Edge/speedflow_python/peer_discovery.py

Peer Discovery — Khám phá các Peer Node trên LAN.

Chiến lược hybrid:
  1. Static registry từ edge_node.yml (primary — luôn có)
  2. mDNS (optional — yêu cầu pip install zeroconf)

Static registry là nguồn tin cậy chính. mDNS là enhancement
cho zero-config deployment trong lab.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("peer_discovery")


@dataclass
class PeerInfo:
    """Thông tin cơ bản của một peer."""
    node_id: str
    ip: str
    signaling_port: int = 8080
    max_streams: int = 4


class PeerDiscovery:
    """
    Quản lý danh sách peer từ static config + mDNS (optional).
    Thread-safe.
    """

    def __init__(
        self,
        static_peers: List[dict],
        mdns_enabled: bool = False,
        service_type: str = "_iot_graduate._tcp.local.",
    ) -> None:
        self._static = [PeerInfo(**p) for p in static_peers]
        self._dynamic: Dict[str, PeerInfo] = {}   # node_id → PeerInfo (from mDNS)
        self._mdns_enabled = mdns_enabled
        self._service_type = service_type
        self._lock = threading.Lock()

        # Pre-populate dynamic map with static entries
        for p in self._static:
            self._dynamic[p.node_id] = p

    def start(self) -> None:
        """Khởi động mDNS worker nếu được bật."""
        if self._mdns_enabled:
            t = threading.Thread(target=self._mdns_worker, daemon=True)
            t.start()
            logger.info("[PeerDiscovery] mDNS worker started (service=%s)", self._service_type)
        else:
            logger.info("[PeerDiscovery] mDNS disabled. Using static registry only.")

    def get_peers(self) -> List[PeerInfo]:
        """Trả về danh sách tất cả peer đang biết đến."""
        with self._lock:
            return list(self._dynamic.values())

    def get_peer(self, node_id: str) -> Optional[PeerInfo]:
        """Tra cứu peer theo node_id."""
        with self._lock:
            return self._dynamic.get(node_id)

    def upsert_peer(self, info: PeerInfo) -> None:
        """Thêm hoặc cập nhật peer từ mDNS."""
        with self._lock:
            existing = self._dynamic.get(info.node_id)
            if existing and existing.ip == info.ip:
                return  # No change
            self._dynamic[info.node_id] = info
            logger.info("[PeerDiscovery] mDNS discovered peer: '%s' @ %s", info.node_id, info.ip)

    # ------------------------------------------------------------------
    # mDNS worker (optional)
    # ------------------------------------------------------------------

    def _mdns_worker(self) -> None:
        """
        Lắng nghe service broadcast _iot_graduate._tcp trên LAN.

        Sử dụng zeroconf (pip install zeroconf).
        Nếu không cài zeroconf → worker silently no-op.
        """
        try:
            from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
        except ImportError:
            logger.warning(
                "[PeerDiscovery] zeroconf not installed. mDNS disabled. "
                "Install: pip install zeroconf"
            )
            return

        class _Listener(ServiceListener):
            def __init__(self, discovery: PeerDiscovery) -> None:
                self._discovery = discovery

            def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                info = zc.get_service_info(type_, name)
                if info is None or not info.parsed_addresses():
                    return
                # name format: "jetson_A._iot_graduate._tcp.local."
                node_id = name.split(".")[0]
                ip = info.parsed_addresses()[0]
                port = info.port or 8080
                self._discovery.upsert_peer(PeerInfo(
                    node_id=node_id,
                    ip=ip,
                    signaling_port=port,
                    max_streams=4,
                ))

            def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                pass  # MQTT heartbeat timeout là tín hiệu OFFLINE chính thức

            def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                pass  # Không cần xử lý update

        zeroconf = Zeroconf()
        listener = _Listener(self)
        ServiceBrowser(zeroconf, self._service_type, listener)

        # Giữ thread sống
        import time
        while True:
            time.sleep(60)
