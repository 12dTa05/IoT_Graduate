from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles

logger = logging.getLogger("violation_store")


class ViolationStore:
    def __init__(self, data_dir: str | Path) -> None:
        self._root = Path(data_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        # BUG-02: asyncio.Lock only protects coroutines; sync save() also
        # writes the same JSONL files. Use a threading.Lock that works in
        # both sync and async contexts (via asyncio.to_thread for async).
        self._write_lock = threading.Lock()
        self._async_write_lock = asyncio.Lock()

    def save(self, record: Dict[str, Any]) -> Optional[Path]:
        node_id = record.get("node_id", "unknown")
        camera_id = record.get("camera_id", "unknown")

        ts_raw = record.get("timestamp") or record.get("ts")
        if ts_raw is None:
            ts = time.time()
        elif isinstance(ts_raw, str):
            try:
                import datetime
                ts = datetime.datetime.fromisoformat(ts_raw).timestamp()
            except ValueError:
                ts = time.time()
        else:
            ts = float(ts_raw)

        if "timestamp" not in record:
            record["timestamp"] = ts

        date_str = time.strftime("%Y-%m-%d", time.localtime(ts))

        node_dir = self._root / date_str / node_id
        node_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = node_dir / "violations.jsonl"
        snapshot_path: Optional[Path] = None

        record = dict(record)
        snapshot_b64 = record.pop("snapshot_b64", None)
        if snapshot_b64:
            ts_ms = int(ts * 1000)
            snapshot_path = node_dir / f"{camera_id}_{ts_ms}.jpg"
            try:
                img_bytes = base64.b64decode(snapshot_b64)
                snapshot_path.write_bytes(img_bytes)
                record["snapshot_file"] = str(snapshot_path.relative_to(self._root))
            except Exception as exc:
                logger.warning("Failed to save snapshot for %s/%s: %s", node_id, camera_id, exc)

        try:
            with self._write_lock:
                with open(jsonl_path, "a") as f:
                    f.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.error("Failed to write JSONL for %s/%s: %s", node_id, camera_id, exc)

        return snapshot_path

    async def save_async(self, record: Dict[str, Any]) -> Optional[Path]:
        node_id = record.get("node_id", "unknown")
        camera_id = record.get("camera_id", "unknown")

        ts_raw = record.get("timestamp") or record.get("ts")
        if ts_raw is None:
            ts = time.time()
        elif isinstance(ts_raw, str):
            try:
                import datetime
                ts = datetime.datetime.fromisoformat(ts_raw).timestamp()
            except ValueError:
                ts = time.time()
        else:
            ts = float(ts_raw)

        if "timestamp" not in record:
            record["timestamp"] = ts

        date_str = time.strftime("%Y-%m-%d", time.localtime(ts))

        node_dir = self._root / date_str / node_id
        node_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = node_dir / "violations.jsonl"
        snapshot_path: Optional[Path] = None

        record = dict(record)
        snapshot_b64 = record.pop("snapshot_b64", None)
        if snapshot_b64:
            ts_ms = int(ts * 1000)
            snapshot_path = node_dir / f"{camera_id}_{ts_ms}.jpg"
            try:
                img_bytes = base64.b64decode(snapshot_b64)
                await asyncio.to_thread(snapshot_path.write_bytes, img_bytes)
                record["snapshot_file"] = str(snapshot_path.relative_to(self._root))
            except Exception as exc:
                logger.warning("Failed to save snapshot for %s/%s: %s", node_id, camera_id, exc)

        async with self._async_write_lock:
            try:
                async with aiofiles.open(jsonl_path, "a") as f:
                    await f.write(json.dumps(record, default=str) + "\n")
            except Exception as exc:
                logger.error("Failed to write JSONL for %s/%s: %s", node_id, camera_id, exc)

        return snapshot_path

    def query(
        self,
        node_id: Optional[str] = None,
        date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []

        date_dirs: List[Path] = []
        if date:
            d = self._root / date
            if d.is_dir():
                date_dirs.append(d)
        else:
            for d in sorted(self._root.iterdir(), reverse=True):
                if d.is_dir():
                    date_dirs.append(d)

        for date_dir in date_dirs:
            node_dirs: List[Path] = []
            if node_id:
                nd = date_dir / node_id
                if nd.is_dir():
                    node_dirs.append(nd)
            else:
                for nd in date_dir.iterdir():
                    if nd.is_dir():
                        node_dirs.append(nd)

            for nd in node_dirs:
                jsonl = nd / "violations.jsonl"
                if not jsonl.exists():
                    continue
                try:
                    with open(jsonl) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    rec = json.loads(line)
                                    results.append(rec)
                                except json.JSONDecodeError:
                                    continue
                except Exception as exc:
                    logger.warning("Failed to read %s: %s", jsonl, exc)

        results.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
        return results[offset:offset + limit]

    async def query_async(
        self,
        node_id: Optional[str] = None,
        date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.query, node_id=node_id, date=date, limit=limit, offset=offset)
