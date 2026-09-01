import asyncio
import base64
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import msgpack
import sys
from pathlib import Path
server_dir = Path(__file__).resolve().parent
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))
repo_root = server_dir.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from app import _start_zenoh_subscriber, ServerState
from violation_store import ViolationStore


class TestServerTrafficEvents(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.store = ViolationStore(self.test_dir)
        self.state = ServerState()
        self.state.store = self.store
        self.state._loop = asyncio.get_running_loop()

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_traffic_event_normalization_and_save(self):
        # 1x1 8-bit dummy JPEG image base64
        dummy_b64 = base64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xd9").decode("ascii")

        raw_payload = {
            "type": "overspeed",
            "node_id": "edge_01",
            "camera_id": "cam_main",
            "ts": 1700000000.0,
            "speed_kmh": 65.5,
            "image_b64": dummy_b64,
        }

        # Mock Zenoh session and verify subscriber registration
        handlers = {}

        class DummySession:
            def declare_subscriber(self, key_expr, callback):
                handlers[key_expr] = callback
                mock_sub = MagicMock()
                return mock_sub

        class DummySample:
            def __init__(self, key_expr, payload_dict):
                self.key_expr = key_expr
                self.payload = MagicMock()
                self.payload.to_bytes.return_value = msgpack.packb(payload_dict)

        dummy_session = DummySession()

        # Simulate handler execution with _on_traffic_event logic
        # Directly invoke the subscriber callback logic inside Server/app.py
        import zenoh
        old_open = getattr(zenoh, "open", None)
        try:
            zenoh.open = lambda cfg: dummy_session
            sess = _start_zenoh_subscriber(self.state)
            self.assertIsNotNone(sess)
            self.assertIn("traffic/events/**", handlers)
            self.assertIn("peers/status/**", handlers)

            # Fire sample on traffic/events/edge_01/cam_main
            sample = DummySample("traffic/events/edge_01/cam_main", raw_payload)
            handlers["traffic/events/**"](sample)

            # Allow event loop tasks to run
            await asyncio.sleep(0.1)

            # Verify ViolationStore saved it with normalization
            records = await self.store.query_async(node_id="edge_01")
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec["node_id"], "edge_01")
            self.assertEqual(rec["camera_id"], "cam_main")
            self.assertEqual(rec["speed_kmh"], 65.5)
            self.assertIn("snapshot_file", rec)
            self.assertNotIn("image_b64", rec)
            self.assertNotIn("snapshot_b64", rec)

            # Verify saved snapshot file exists on disk
            snap_path = self.test_dir / rec["snapshot_file"]
            self.assertTrue(snap_path.exists())
            self.assertEqual(snap_path.read_bytes(), base64.b64decode(dummy_b64))

        finally:
            if old_open:
                zenoh.open = old_open


if __name__ == "__main__":
    unittest.main()
