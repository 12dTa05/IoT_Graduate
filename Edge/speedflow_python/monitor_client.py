"""
speedflow_python/monitor_client.py — Outbound WebSocket client to Central Monitor.

The Edge opens a persistent WS connection to the Server. Health + violation
data is pushed from daemon threads via a thread-safe `send()` method.
Reconnects automatically with exponential backoff.

Usage:
    client = MonitorClient(server_url, node_id)
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
import urllib.parse
from typing import Any, Dict, Optional

logger = logging.getLogger("monitor_client")

_RECONNECT_DELAYS = [2, 5, 10, 30]


class MonitorClient:
    """
    Persistent outbound WebSocket client to the Central Monitoring Server.

    Runs a daemon thread that:
      1. Connects to ws://<server>/ws/edge?node_id=<node_id>
      2. Sends queued messages as JSON text frames
      3. Reconnects on disconnect with exponential backoff

    Thread-safe: `send()` can be called from any thread.
    """

    def __init__(
        self,
        server_url: str,
        node_id: str,
        advertise_ip: str = "",
    ) -> None:
        base = server_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        # BUG-18: URL-encode node_id and advertise_ip to handle special chars
        self._ws_url = (
            f"{base}/ws/edge"
            f"?node_id={urllib.parse.quote(node_id, safe='')}"
            f"&advertise_ip={urllib.parse.quote(advertise_ip, safe='')}"
        )
        self._node_id = node_id

        self._queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=500)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._sent_count = 0
        self._drop_count = 0
        # BUG-B fix: a single stats lock is sufficient.  The old _send_lock
        # existed only to serialise the get_nowait/put_nowait eviction pair,
        # but queue.Queue's internal lock already makes each operation atomic.
        # Having two locks acquired in different orders in different call sites
        # created a lock-ordering inversion risk.
        self._stats_lock = threading.Lock()

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
        # BUG-09: read counters under lock so we see the final value
        with self._stats_lock:
            sent = self._sent_count
            dropped = self._drop_count
        logger.info(
            "[MonitorClient] Stopped. Sent=%d, Dropped=%d",
            sent, dropped,
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
            # Evict the oldest message to make room.  queue.Queue.get_nowait()
            # and put_nowait() are each individually atomic; if another thread
            # evicts between our get and put we may still see Full — catch it
            # and simply discard the new payload rather than blocking.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass
            with self._stats_lock:
                self._drop_count += 1

    # ------------------------------------------------------------------
    # Internal — connection loop (runs in daemon thread)
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Connect → drain queue → reconnect on failure.

        Uses WebSocketApp which handles PING/PONG on its own internal
        recv thread, avoiding thread-safety issues with concurrent
        recv()/send() on a plain WebSocket object.
        """
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
            self._ws_app = None
            self._ws_connected = threading.Event()
            self._ws_closed = threading.Event()
            app_thread = None

            try:
                logger.info("[MonitorClient] Connecting to %s", self._ws_url)

                def on_open(wsapp):
                    logger.info("[MonitorClient] Connected")
                    self._ws_connected.set()

                def on_message(wsapp, message):
                    pass  # ignore server→edge text frames

                def on_error(wsapp, error):
                    logger.warning("[MonitorClient] WS error: %s", error)

                def on_close(wsapp, close_status, close_msg):
                    logger.warning("[MonitorClient] WS closed: status=%s msg=%s", close_status, close_msg)
                    self._ws_closed.set()

                def on_ping(wsapp, data):
                    pass  # WebSocketApp auto-sends PONG

                app = websocket.WebSocketApp(
                    self._ws_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                    on_ping=on_ping,
                )
                self._ws_app = app

                # Run WebSocketApp in a background thread — it handles
                # recv + PING/PONG internally.
                app_thread = threading.Thread(
                    target=lambda: app.run_forever(
                        ping_interval=0,
                        ping_timeout=None,
                    ),
                    name=f"MonitorClient-ws-{self._node_id}",
                    daemon=True,
                )
                app_thread.start()

                # Wait for connection
                # BUG-01: explicitly close the app before continuing so the
                # background run_forever() thread is unblocked and the WS is
                # not left dangling. The finally block will join app_thread.
                if not self._ws_connected.wait(timeout=10):
                    logger.warning("[MonitorClient] Connection timeout")
                    app.close()   # signal run_forever() to exit
                    continue

                delay_idx = 0  # reset backoff
                self._drain_loop_app(app)

            except Exception as exc:
                delay = _RECONNECT_DELAYS[min(delay_idx, len(_RECONNECT_DELAYS) - 1)]
                logger.warning(
                    "[MonitorClient] Connection error: %s — reconnect in %ds",
                    exc, delay,
                )
                delay_idx += 1
                for _ in range(int(delay * 10)):
                    if not self._running:
                        break
                    time.sleep(0.1)
            finally:
                if self._ws_app:
                    try:
                        self._ws_app.close()
                    except Exception:
                        pass
                if app_thread:
                    app_thread.join(timeout=5)

    def _drain_loop_app(self, app) -> None:
        """Send queued messages via WebSocketApp until disconnect or stop."""
        while self._running and not self._ws_closed.is_set():
            try:
                payload = self._queue.get(timeout=2.0)
            except queue.Empty:
                continue

            if payload is None:
                break  # stop sentinel

            try:
                app.send(payload)
                with self._stats_lock:
                    self._sent_count += 1
            except Exception as exc:
                logger.warning("[MonitorClient] Send failed: %s", exc)
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
    # BUG-E fix: stop the previous client before replacing it so its daemon
    # thread and WebSocket connection are not leaked.
    if _default_client is not None and _default_client is not client:
        try:
            _default_client.stop()
        except Exception:
            pass
    _default_client = client


def send_to_monitor(data: Dict[str, Any]) -> None:
    """
    Thread-safe convenience function called from health_agent /
    zenoh_publisher.  No-op if no MonitorClient has been set.
    """
    if _default_client is not None:
        _default_client.send(data)
