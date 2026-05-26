"""
Server/violation_store.py — JSONL log + snapshot image storage.

Layout:
  violations/{YYYY-MM-DD}/{node_id}/
    violations.jsonl        # Newline-delimited JSON, one record per line
    {cam_id}_{ts_ms}.jpg    # Snapshot images

Usage:
  store = ViolationStore(Path("violations"))
  store.save({"type": "violation", "node_id": "jetson_A", "camera_id": "cam_01",
              "snapshot_b64": "...", ...})
  records = store.query(node_id="jetson_A", limit=20)
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("violation_store")


class ViolationStore:
    def __init__(self, data_dir: str | Path) -> None:
        self._root = Path(data_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self, record: Dict[str, Any]) -> Optional[Path]:
        """
        Persist one violation record.

        - Writes JSON line to `violations/{date}/{node_id}/violations.jsonl`
        - If `snapshot_b64` is present, decodes and saves as
          `{cam_id}_{ts_ms}.jpg` in the same folder.
        - Returns the image path if a snapshot was saved, else None.
        """
        node_id = record.get("node_id", "unknown")
        camera_id = record.get("camera_id", "unknown")

        # SpeedProbe sends "ts" (ISO string); health_agent sends "timestamp" (float).
        # Normalise to a Unix float so we can derive the date and filename.
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

        # Store the normalised float timestamp so query() sorting works correctly
        if "timestamp" not in record:
            record["timestamp"] = ts

        date_str = time.strftime("%Y-%m-%d", time.localtime(ts))

        node_dir = self._root / date_str / node_id
        node_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = node_dir / "violations.jsonl"
        snapshot_path: Optional[Path] = None

        # --- Save snapshot image (strip b64 before logging JSON) ---
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

        # --- Append JSON line (no snapshot_b64) ---
        try:
            with open(jsonl_path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.error("Failed to write JSONL for %s/%s: %s", node_id, camera_id, exc)

        return snapshot_path

    def query(
        self,
        node_id: Optional[str] = None,
        date: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Return recent violation records, newest first.

        Filters by node_id and/or date if provided.
        Max `limit` records returned (default 50).
        """
        results: List[Dict[str, Any]] = []

        # Walk date dirs
        date_dirs: List[Path] = []
        if date:
            d = self._root / date
            if d.is_dir():
                date_dirs.append(d)
        else:
            # Sort newest-first
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

            for node_dir in node_dirs:
                jsonl = node_dir / "violations.jsonl"
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

        # Sort newest first (by timestamp descending)
        results.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
        return results[:limit]
