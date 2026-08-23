"""
speedflow_python/zenoh_subscriber.py

Zenoh Command & Control Subscriber for Worker Node (Jetson Edge).

Subscribes to key expression:
    peers/control/{node_id}

Command format (msgpack):
    {"cmd": "ADD", "camera_id": "...", "source_id": N, "uri": "...", ...}
    {"cmd": "REMOVE", "camera_id": "..."}
    {"cmd": "STATUS"}

Requirements:
    pip install zenoh msgpack opencv-python numpy
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import msgpack

from .camera_config import CameraManager, StreamDelta
from .zenoh_session import make_session

logger = logging.getLogger(__name__)


class ZenohCommandSubscriber:
    """
    Subscribe to Zenoh key expression to receive control commands
    from the Peer Orchestrator.

    Key expression: peers/control/{node_id}
    Status replies: peers/status/{node_id}
    Vote acks:      peers/vote/ack/{camera_id}
    """

    def __init__(
        self,
        camera_manager: CameraManager,
        node_id: str,
        session=None,
        ack_timeout_s: Optional[float] = None,
    ) -> None:
        self._camera_manager = camera_manager
        self._node_id = node_id
        self._external_session = session
        self._ack_timeout_s = ack_timeout_s

        self._control_key = f"peers/control/{node_id}"
        self._status_key = f"peers/status/{node_id}"
        self._session = None
        self._subscriber = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open Zenoh session and subscribe to control key."""
        if self._external_session is not None:
            self._session = self._external_session
            logger.info("[Zenoh C2] Using shared Zenoh session.")
        else:
            import zenoh
            self._session = make_session()
            logger.info("[Zenoh C2] Session opened (peer mode).")

        self._subscriber = self._session.declare_subscriber(
            self._control_key,
            self._on_sample,
        )
        logger.info(
            "[Zenoh C2] Subscribed to '%s'. Node='%s'",
            self._control_key, self._node_id,
        )

        # Signal that this node is online
        self.publish_status({
            "node_id": self._node_id,
            "event": "NODE_ONLINE",
            "active_cameras": [
                c.camera_id
                for c in self._camera_manager.get_enabled_configs()
            ],
        })

    def stop(self) -> None:
        """Unsubscribe and close Zenoh session."""
        self._running = False
        if self._subscriber:
            self._subscriber.undeclare()
        # BUG-10 fix: only close the session if we opened it ourselves.
        # When an external (shared) session was supplied, the caller
        # (PeerOrchestrator / run_python.py) owns the lifecycle — closing it
        # here would crash all sibling modules that share the same session.
        if self._session and self._external_session is None:
            self._session.close()

    def publish_status(self, payload: dict) -> None:
        """Publish status/event from this node."""
        if self._session:
            try:
                data = msgpack.packb(payload, use_bin_type=True)
                self._session.put(self._status_key, data)
            except Exception as exc:
                logger.warning("[Zenoh C2] Failed to publish status: %s", exc)

    # ------------------------------------------------------------------
    # Internal — Zenoh subscriber callback
    # ------------------------------------------------------------------

    def _on_sample(self, sample) -> None:
        """Handle incoming control command."""
        import zenoh
        try:
            payload = msgpack.unpackb(sample.payload.to_bytes(), raw=False)
        except Exception as exc:
            logger.error("[Zenoh C2] Invalid msgpack received: %s", exc)
            return

        cmd = payload.get("cmd", "").upper()
        logger.info("[Zenoh C2] Received command: %s", cmd)

        if cmd == "ADD":
            self._handle_add(payload)
        elif cmd == "REMOVE":
            self._handle_remove(payload)
        elif cmd == "STATUS":
            self._handle_status_request()
        else:
            logger.warning("[Zenoh C2] Unknown command: '%s'", cmd)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_add(self, payload: dict) -> None:
        try:
            cam_id    = payload["camera_id"]
            source_id = int(payload["source_id"])

            # Delegate config building + delta enqueue to CameraManager so the
            # same logic is shared with PeerOrchestrator's direct-dispatch path.
            queued = self._camera_manager.handle_add_command(payload)
            if not queued:
                self.publish_status({
                    "node_id": self._node_id,
                    "event": "ADD_REJECTED",
                    "camera_id": cam_id,
                    "reason": "camera_or_source_id_conflict",
                })
                return

            self.publish_status({
                "node_id": self._node_id,
                "event": "ADD_PROCESSING",
                "camera_id": cam_id,
                "source_id": source_id,
            })

            # P2P vote ack — wait until the stream actually reaches PLAYING
            # state (or timeout) before acknowledging Make-Before-Break.
            #
            # BUG-3 fix: the old implementation sent the ack unconditionally
            # after a hardcoded sleep(3), which could ack a failed ADD and
            # cause the requester to REMOVE its own stream before the new one
            # is actually up.
            #
            # Strategy: poll the CameraConfig.enabled flag AND verify that
            # the CameraManager has a live source_id mapping (set by on_add
            # which runs on the GLib main loop after dynamic_add_stream
            # succeeds).  We also apply a generous 15-second timeout so that
            # slow RTSP sources don't block forever.
            _session     = self._session
            _cam_id      = cam_id
            _source_id   = source_id
            _node_id     = self._node_id
            _cam_manager = self._camera_manager
            # Coordinate with migration_timeout_s: default 12.0s allows migration_timeout_s (15.0s) a 3.0s safety margin
            _ack_timeout = float(self._ack_timeout_s) if self._ack_timeout_s is not None else 12.0

            def _send_ack() -> None:
                import time as _time
                deadline = _time.monotonic() + _ack_timeout
                playing  = False

                # Config registration is synchronous and is not stream readiness.
                # Wait for the first decoded buffer from this source instead.
                ready_event = _cam_manager.stream_ready_event(_source_id)
                if ready_event is not None:
                    playing = ready_event.wait(timeout=max(0.0, deadline - _time.monotonic()))

                if not playing:
                    logger.warning(
                        "[Zenoh C2] ADD ack NOT sent for '%s': stream did not reach "
                        "PLAYING within %.0fs.", _cam_id, _ack_timeout,
                    )
                    # Local cleanup of unacknowledged stream to avoid duplicate orphan processing
                    try:
                        if hasattr(_cam_manager, "cleanup_stream_ready"):
                            _cam_manager.cleanup_stream_ready(_source_id)
                        with _cam_manager._lock:
                            cfg = _cam_manager._configs.get(_cam_id)
                            if cfg and cfg.enabled:
                                cfg.enabled = False
                                _cam_manager._rebuild_lookup()
                        logger.info("[Zenoh C2] Disabled timed-out config for stream '%s'", _cam_id)
                    except Exception as exc:
                        logger.error("[Zenoh C2] Failed local cleanup after ADD timeout for '%s': %s", _cam_id, exc)
                    return

                try:
                    ack_payload = msgpack.packb({
                        "node_id":   _node_id,
                        "camera_id": _cam_id,
                        "event":     "PLAYING",
                    }, use_bin_type=True)
                    _session.put(f"peers/vote/ack/{_cam_id}", ack_payload)
                    logger.info("[Zenoh C2] ADD ack sent for '%s' (stream PLAYING).", _cam_id)
                except Exception as exc:
                    logger.warning("[Zenoh C2] Failed to send ack for '%s': %s", _cam_id, exc)

            threading.Thread(target=_send_ack, daemon=True).start()

        except KeyError as exc:
            logger.error("[Zenoh C2] ADD command missing required field: %s", exc)
            self.publish_status({
                "node_id": self._node_id,
                "event": "ADD_FAILED",
                "reason": f"missing_field_{exc}",
            })
        except Exception as exc:
            logger.error("[Zenoh C2] ADD command error: %s", exc)
            self.publish_status({
                "node_id": self._node_id,
                "event": "ADD_FAILED",
                "reason": str(exc),
            })

    def _handle_remove(self, payload: dict) -> None:
        try:
            cam_id = payload["camera_id"]

            with self._camera_manager._lock:
                cfg = self._camera_manager._configs.get(cam_id)
                if not cfg or not cfg.enabled:
                    logger.warning(
                        "[Zenoh C2] REMOVE ignored: camera_id='%s' not active.", cam_id
                    )
                    self.publish_status({
                        "node_id": self._node_id,
                        "event": "REMOVE_REJECTED",
                        "camera_id": cam_id,
                        "reason": "not_active",
                    })
                    return
                # If command specifies source_id, verify it matches current active source_id.
                # Robustly reject malformed non-integer source_id without throwing out of Zenoh callback.
                cmd_sid = payload.get("source_id")
                if cmd_sid is not None:
                    try:
                        parsed_sid = int(cmd_sid)
                    except (ValueError, TypeError):
                        logger.warning(
                            "[Zenoh C2] Malformed REMOVE source_id ignored: camera_id='%s' source_id=%r",
                            cam_id, cmd_sid,
                        )
                        self.publish_status({
                            "node_id": self._node_id,
                            "event": "REMOVE_REJECTED",
                            "camera_id": cam_id,
                            "reason": "malformed_source_id",
                        })
                        return

                    if parsed_sid != cfg.source_id:
                        logger.warning(
                            "[Zenoh C2] Stale REMOVE ignored: camera_id='%s' active source_id=%d != cmd source_id=%d",
                            cam_id, cfg.source_id, parsed_sid,
                        )
                        self.publish_status({
                            "node_id": self._node_id,
                            "event": "REMOVE_REJECTED",
                            "camera_id": cam_id,
                            "reason": "stale_source_id",
                        })
                        return
                source_id = cfg.source_id
                cfg.enabled = False
                self._camera_manager._rebuild_lookup()
                if hasattr(self._camera_manager, "cleanup_stream_ready"):
                    self._camera_manager.cleanup_stream_ready(source_id)

            delta = StreamDelta(to_remove=[source_id])
            self._camera_manager._delta_q.put(delta)

            logger.info(
                "[Zenoh C2] REMOVE queued: camera_id='%s', source_id=%d", cam_id, source_id
            )
            self.publish_status({
                "node_id": self._node_id,
                "event": "REMOVE_PROCESSING",
                "camera_id": cam_id,
                "source_id": source_id,
            })

        except KeyError as exc:
            logger.error("[Zenoh C2] REMOVE command missing required field: %s", exc)
        except Exception as exc:
            logger.error("[Zenoh C2] REMOVE command error: %s", exc)

    def _handle_status_request(self) -> None:
        active = self._camera_manager.get_enabled_configs()
        self.publish_status({
            "node_id": self._node_id,
            "event": "STATUS_REPORT",
            "active_cameras": [c.camera_id for c in active],
            "active_count": len(active),
        })
