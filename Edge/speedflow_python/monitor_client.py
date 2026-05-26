"""
speedflow_python/monitor_client.py — Outbound WebSocket client to Central Monitor.

The Edge opens a persistent WS connection to the Server. Health + violation
data is pushed from daemon threads via a thread-safe `send()` method.
Reconnects automatically with exponential backoff.

Usage:
    client = MonitorClient(server_url, node_id, signaling_port)
    client.start()

    # From any thread:
    client.send({"type": "health", ...})
    client.send({"type": "overspeed", ...})

    client.stop()
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("monitor_client")

_RECONNECT_DELAYS = [2, 5, 10, 30]


class MonitorClient:
    """
    Persistent outbound WebSocket client to the Central Monitoring Server.

    Runs a daemon thread that:
      1. Connects to ws://<server>/ws/edge?node_id=<node_id>&signaling_port=<port>
      2. Sends queued messages as JSON text frames
      3. Reconnects on disconnect with exponential backoff

    Thread-safe: `send()` can be called from any thread.
    """

    def __init__(
        self,
        server_url: str,
        node_id: str,
        signaling_port: int,
    ) -> None:
        base = server_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        self._ws_url = (
            f"{base}/ws/edge"
            f"?node_id={node_id}"
            f"&signaling_port={signaling_port}"
        )
        self._node_id = node_id

        self._queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=500)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._sent_count = 0
        self._drop_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name=f"MonitorClient-{self._node_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("[MonitorClient] Started. URL=%s", self._ws_url)

    def stop(self) -> None:
        self._running = False
        self._queue.put(None)  # sentinel to unblock
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(
            "[MonitorClient] Stopped. Sent=%d, Dropped=%d",
            self._sent_count, self._drop_count,
        )

    def send(self, data: Dict[str, Any]) -> None:
        """
        Enqueue a message for sending to the Server (NON-BLOCKING).

        Can be called from any thread.  If the queue is full the oldest
        message is dropped so callers never block.
        """
        payload = json.dumps(data, default=str)
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(payload)
                self._drop_count += 1
            except queue.Empty:
                pass

    # ------------------------------------------------------------------
    # Internal — connection loop (runs in daemon thread)
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Connect → drain queue → reconnect on failure."""
        # Import websocket-client here (lightweight, stdlib-compatible)
        try:
            import websocket  # websocket-client package
        except ImportError:
            logger.error(
                "[MonitorClient] websocket-client not installed. "
                "Run: pip install websocket-client"
            )
            return

        delay_idx = 0

        while self._running:
            ws = None
            try:
                logger.info("[MonitorClient] Connecting to %s", self._ws_url)
                ws = websocket.WebSocket()
                ws.settimeout(5)
                ws.connect(self._ws_url)
                logger.info("[MonitorClient] Connected")
                delay_idx = 0  # reset backoff

                self._drain_loop(ws)

            except Exception as exc:
                delay = _RECONNECT_DELAYS[min(delay_idx, len(_RECONNECT_DELAYS) - 1)]
                logger.warning(
                    "[MonitorClient] Connection error: %s — reconnect in %ds",
                    exc, delay,
                )
                delay_idx += 1
                # Sleep in short steps so stop() is responsive
                for _ in range(int(delay * 10)):
                    if not self._running:
                        break
                    time.sleep(0.1)
            finally:
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def _drain_loop(self, ws) -> None:
        """Send queued messages until disconnect or stop."""
        while self._running:
            try:
                payload = self._queue.get(timeout=2.0)
            except queue.Empty:
                # Send a ping to detect broken connections
                try:
                    ws.ping()
                except Exception:
                    break
                continue

            if payload is None:
                break  # stop sentinel

            try:
                ws.send(payload)
                self._sent_count += 1
            except Exception as exc:
                logger.warning("[MonitorClient] Send failed: %s", exc)
                # Put the failed message back for retry on reconnect
                try:
                    self._queue.put_nowait(payload)
                except queue.Full:
                    pass
                break  # reconnect


# ---------------------------------------------------------------------------
# Module-level convenience API (late-binding, set_default_client first)
# ---------------------------------------------------------------------------

_default_client: Optional[MonitorClient] = None


def set_default_client(client: MonitorClient) -> None:
    global _default_client
    _default_client = client


def send_to_monitor(data: Dict[str, Any]) -> None:
    """
    Thread-safe convenience function called from health_agent /
    zenoh_publisher.  No-op if no MonitorClient has been set.
    """
    if _default_client is not None:
        _default_client.send(data)
