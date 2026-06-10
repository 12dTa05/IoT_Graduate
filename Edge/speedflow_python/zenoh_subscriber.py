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

import cv2
import msgpack
import numpy as np

from .camera_config import CameraConfig, CameraManager, StreamDelta
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
    ) -> None:
        self._camera_manager = camera_manager
        self._node_id = node_id
        self._external_session = session

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
            uri       = payload["uri"]

            existing = self._camera_manager.get_config(source_id)
            if existing and existing.enabled:
                logger.warning(
                    "[Zenoh C2] ADD ignored: source_id=%d ('%s') already active.",
                    source_id, existing.camera_id,
                )
                self.publish_status({
                    "node_id": self._node_id,
                    "event": "ADD_REJECTED",
                    "camera_id": cam_id,
                    "reason": "source_id_conflict",
                })
                return

            homo_cfg   = payload["homography"]
            src_pts    = np.array(homo_cfg["source_points"], dtype=np.float32)
            tw         = int(homo_cfg["target_width"])
            th         = int(homo_cfg["target_height"])
            tgt_pts    = np.array([[0, 0], [tw, 0], [tw, th], [0, th]], dtype=np.float32)
            homo_mat, _ = cv2.findHomography(src_pts, tgt_pts)
            if homo_mat is None:
                homo_mat = cv2.getPerspectiveTransform(src_pts, tgt_pts)

            roi_raw = payload.get("roi_polygon", [])
            roi_arr = np.array(roi_raw, dtype=np.int32).reshape(-1, 2) if roi_raw else np.zeros((0, 2), dtype=np.int32)

            out_cfg = payload.get("output", {})

            cam_cfg = CameraConfig(
                camera_id=cam_id,
                source_id=source_id,
                uri=uri,
                enabled=True,
                name=payload.get("name", cam_id),
                fps=float(payload.get("fps", 25.0)),
                speed_limit_kmh=float(payload.get("speed_limit_kmh", 80.0)),
                source_points=src_pts,
                target_points=tgt_pts,
                homo_matrix=homo_mat,
                roi_polygon=roi_arr,
                record=bool(out_cfg.get("record", False)),
                record_path=str(out_cfg.get("record_path", f"output/{cam_id}.mp4")),
            )

            with self._camera_manager._lock:
                self._camera_manager._configs[cam_id] = cam_cfg
                self._camera_manager._rebuild_lookup()

            delta = StreamDelta(to_add=[cam_cfg])
            self._camera_manager._delta_q.put(delta)

            logger.info("[Zenoh C2] ADD queued: camera_id='%s', source_id=%d", cam_id, source_id)

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
            _ack_timeout = 15.0   # seconds — matches migration_timeout_s default

            def _send_ack() -> None:
                import time as _time
                deadline = _time.monotonic() + _ack_timeout
                playing  = False

                # Poll until the source_id appears in the live lookup table
                # (populated by on_add on the GLib main loop thread after
                # dynamic_add_stream completes) or until timeout.
                while _time.monotonic() < deadline:
                    _time.sleep(0.25)
                    try:
                        with _cam_manager._lock:
                            live_cfg = _cam_manager._by_source_id.get(_source_id)
                        if live_cfg is not None and live_cfg.camera_id == _cam_id and live_cfg.enabled:
                            playing = True
                            break
                    except Exception:
                        pass

                if not playing:
                    logger.warning(
                        "[Zenoh C2] ADD ack NOT sent for '%s': stream did not reach "
                        "PLAYING within %.0fs.", _cam_id, _ack_timeout,
                    )
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
                source_id = cfg.source_id
                cfg.enabled = False
                self._camera_manager._rebuild_lookup()

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
