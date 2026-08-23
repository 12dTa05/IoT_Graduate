"""
speedflow_python/zenoh_publisher.py

Zenoh Publisher for Worker Node (Jetson Edge).

Non-blocking design:
    - SpeedProbe calls `publisher.put(data)` (< 0.1ms, no network wait).
    - Internal `_publish_loop` thread consumes the queue and publishes via Zenoh.
    - Queue has maxsize (from .env ZENOH_QUEUE_MAXSIZE) to prevent OOM.
    - If Queue is full → drop oldest (block=False) so pipeline never locks.

Key expression:
    traffic/events/{node_id}/{camera_id}  — speed events, overspeed alerts

Requirements:
    pip install zenoh msgpack
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Dict, Optional

import msgpack

from .settings import ZENOH_QUEUE_MAXSIZE
from .zenoh_session import make_session

logger = logging.getLogger(__name__)


class ZenohPublisher:
    """
    Non-blocking Zenoh Publisher for GStreamer probes.

    Usage:
        publisher = ZenohPublisher(node_id=NODE_ID)
        publisher.start()

        # Inside SpeedProbe (30fps callback — must not block):
        publisher.put({"camera_id": "cam_01", "speed_kmh": 92.5, ...})

        publisher.stop()
    """

    def __init__(self, node_id: str, session=None) -> None:
        self._node_id = node_id
        self._ext_sess = session        # shared Zenoh session if provided

        self._queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(maxsize=int(ZENOH_QUEUE_MAXSIZE))
        self._session = None
        self._publishers: Dict[str, Any] = {}   # cache: key_expr → publisher
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._sent_count = 0
        self._drop_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open Zenoh session (or use shared session) and start publisher thread."""
        if self._ext_sess is not None:
            self._session = self._ext_sess
            logger.info("[ZenohPub] Using shared Zenoh session.")
        else:
            import zenoh
            self._session = make_session()
            logger.info("[ZenohPub] Session opened (peer mode).")

        self._running = True
        self._thread = threading.Thread(
            target=self._publish_loop,
            name=f"ZenohPublisher-{self._node_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop publisher, flush remaining queue, close session if owned."""
        self._running = False
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=10)
        if self._ext_sess is None:
            for pub in self._publishers.values():
                try:
                    pub.undeclare()
                except Exception:
                    pass
            if self._session:
                self._session.close()
        self._publishers.clear()
        logger.info(
            "[ZenohPub] Stopped. Sent=%d, Dropped=%d",
            self._sent_count, self._drop_count,
        )

    def put(self, data: Dict[str, Any]) -> None:
        """
        Enqueue data for publish (NON-BLOCKING).

        If the queue is full (network down), the OLDEST event is dropped
        in favour of newer data. Pipeline never blocks.
        """
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(data)
                self._drop_count += 1
                if self._drop_count % 100 == 1:
                    logger.warning(
                        "[ZenohPub] Queue full (%d slots). "
                        "Dropping oldest events (total dropped: %d).",
                        ZENOH_QUEUE_MAXSIZE, self._drop_count,
                    )
            except queue.Empty:
                pass

    # ------------------------------------------------------------------
    # Internal — publish loop
    # ------------------------------------------------------------------

    def _publish_loop(self) -> None:
        """Consume queue and publish via Zenoh."""
        import zenoh
        while self._running:
            try:
                item = self._queue.get(timeout=2.0)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._send(item)
            except Exception as exc:
                logger.warning("[ZenohPub] Send error: %s", exc)
            # Zenoh peer mode has no "reconnect" — session stays valid

    def _send(self, data: Dict[str, Any]) -> None:
        """Msgpack-serialize and publish one event."""
        camera_id = data.get("camera_id", "unknown")
        key = f"traffic/events/{self._node_id}/{camera_id}"
        # Ensure timestamp and schema metadata are attached to traffic events
        if isinstance(data, dict):
            if "timestamp" not in data and "ts" not in data:
                data["timestamp"] = time.time()
                data["ts"] = data["timestamp"]
            elif "timestamp" not in data and "ts" in data:
                data["timestamp"] = data["ts"]
            elif "ts" not in data and "timestamp" in data:
                data["ts"] = data["timestamp"]
            if "schema_version" not in data and "version" not in data:
                data["schema_version"] = 1
                data["version"] = 1
            elif "schema_version" not in data and "version" in data:
                data["schema_version"] = data["version"]
            elif "version" not in data and "schema_version" in data:
                data["version"] = data["schema_version"]
        payload = msgpack.packb(data, use_bin_type=True)
        if self._session:
            # Reuse declared publishers to avoid resource leak
            pub = self._publishers.get(key)
            if pub is None:
                pub = self._session.declare_publisher(key)
                self._publishers[key] = pub
            pub.put(payload)
            self._sent_count += 1
