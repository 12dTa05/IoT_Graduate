"""
Baseline offloading and coordination policies for experimental evaluation and benchmarking.
Used to compare against the proposed multi-granularity P2P framework (Finding 2.5).

Supported policies:
  1. NoOffloadPolicy: Baseline 0 — local execution only, no offload under any condition.
  2. RoundRobinPolicy: Baseline 1 — static round-robin offload across active peers without load awareness.
  3. LeastLoadGreedyPolicy: Baseline 2 — pure least-load greedy assignment bypassing Pareto epsilon-constraints.
  4. CentralizedGreedyPolicy: Baseline 3 — centralized greedy assignment mimicking cloud/edge-controller.
"""

from typing import Dict, Optional, Any


class BaseOffloadPolicy:
    def __init__(self, name: str):
        self.name = name

    def select_peer(
        self,
        camera_id: str,
        self_node_id: str,
        peers: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        raise NotImplementedError


class NoOffloadPolicy(BaseOffloadPolicy):
    def __init__(self):
        super().__init__("no_offload")

    def select_peer(
        self,
        camera_id: str,
        self_node_id: str,
        peers: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        return None


class RoundRobinPolicy(BaseOffloadPolicy):
    def __init__(self):
        super().__init__("round_robin")
        self._counter = 0

    def select_peer(
        self,
        camera_id: str,
        self_node_id: str,
        peers: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        alive_peers = sorted([nid for nid in peers.keys() if nid != self_node_id])
        if not alive_peers:
            return None
        selected = alive_peers[self._counter % len(alive_peers)]
        self._counter += 1
        return selected


class LeastLoadGreedyPolicy(BaseOffloadPolicy):
    def __init__(self):
        super().__init__("least_load_greedy")

    def select_peer(
        self,
        camera_id: str,
        self_node_id: str,
        peers: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        best_peer = None
        best_load = float("inf")
        for nid, peer in peers.items():
            if nid == self_node_id:
                continue
            load = getattr(peer, "load_score", 100.0) if hasattr(peer, "load_score") else peer.get("load_score", 100.0)
            if load < best_load:
                best_load = load
                best_peer = nid
        return best_peer


class CentralizedGreedyPolicy(BaseOffloadPolicy):
    def __init__(self):
        super().__init__("centralized_greedy")

    def select_peer(
        self,
        camera_id: str,
        self_node_id: str,
        peers: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        # Centralized view: finds absolute global minimum across all known nodes
        candidates = []
        for nid, peer in peers.items():
            if nid == self_node_id:
                continue
            load = getattr(peer, "load_score", 100.0) if hasattr(peer, "load_score") else peer.get("load_score", 100.0)
            candidates.append((load, nid))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]
