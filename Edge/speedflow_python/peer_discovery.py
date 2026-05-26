"""
peer_discovery.py — No-op shim.

Peer discovery is handled automatically by Zenoh peer-mode scouting.
Peers become visible when they publish their first peers/status/{node_id}
heartbeat. No static registry or mDNS needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class PeerInfo:
    """Minimal peer information — kept for backward compat imports."""
    node_id: str
    ip: str
    signaling_port: int = 8080
    max_streams: int = 4
