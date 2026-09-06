"""
Edge/speedflow_python/lease_state.py  — P5

Crash-safe, stdlib-only persistence of the minimal lease state required for
boot fencing:

  * boot_id        — monotonic per-node counter, bumped on every boot.
  * camera_epochs  — per-camera epoch high-water mark (integer wire epoch).

Why this exists
---------------
On reboot, in-flight ADD/REMOVE commands may be replayed by Zenoh or by retry
logic. Two independent fences stop a stale pre-reboot command from passing the
receiver:

  1. boot_id fence — every command/ack carries the *recipient's* current
     boot_id. After a reboot the node's boot_id is strictly greater than the
     value baked into any pre-reboot command, so the subscriber rejects
     commands whose boot_id != current boot_id.

  2. epoch floor fence — the persisted per-camera epoch high-water is loaded
     into the receiver's held-epoch floor, so a command with an epoch below
     the floor (or below what was already applied this boot) is rejected.

Fail-safe-high on load
----------------------
  * missing file  -> first boot: boot_id=0, camera_epochs={}
  * corrupt JSON  -> fail-safe-high: boot_id jumps to FAILSAFE_HIGH (a value no
    real command can carry), so every replayed pre-reboot command is rejected.
    Peers re-learn the new boot_id from this node's heartbeat and re-issue with
    the correct boot_id, so the node self-heals.

No dependencies, no DB. Atomic write = same-dir tmp + fsync file + os.replace
+ fsync directory.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

# ponytail: sentinel far above any plausible monotonic epoch/boot_id. On a
# corrupt load we cannot trust the persisted counters, so we jump boot_id to
# this value — any replayed command (which carried a real, small boot_id) is
# then strictly lower and rejected. Python ints are unbounded, so +1 per boot
# never overflows in practice.
FAILSAFE_HIGH = 2 ** 62

SCHEMA_VERSION = 1


def _is_int(v) -> bool:
    """True for a real integer (bool excluded)."""
    return isinstance(v, int) and not isinstance(v, bool)


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename/creation is durable. Best-effort."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically.

    same-dir tmp -> fsync file -> os.replace -> fsync dir. A crash mid-write
    leaves the prior file intact (the .tmp is ignored by readers).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{time.monotonic_ns()}"
    )
    blob = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, blob)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def load_lease_state(path: Path, *, fail_safe_high: bool = True) -> dict:
    """Load lease state from disk.

    Returns a plain dict:
        schema, boot_id, camera_epochs, loaded, corrupt, first_boot
    """
    path = Path(path)
    if not path.exists():
        return {
            "schema": SCHEMA_VERSION,
            "boot_id": 0,
            "camera_epochs": {},
            "loaded": False,
            "corrupt": False,
            "first_boot": True,
        }
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        boot_id = int(data.get("boot_id", 0))
        camera_epochs: Dict[str, int] = {}
        raw = data.get("camera_epochs") or {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if _is_int(v):
                    camera_epochs[str(k)] = int(v)
        return {
            "schema": SCHEMA_VERSION,
            "boot_id": boot_id,
            "camera_epochs": camera_epochs,
            "loaded": True,
            "corrupt": False,
            "first_boot": False,
        }
    except Exception:
        if not fail_safe_high:
            raise
        return {
            "schema": SCHEMA_VERSION,
            "boot_id": FAILSAFE_HIGH,
            "camera_epochs": {},
            "loaded": True,
            "corrupt": True,
            "first_boot": False,
        }


class LeaseState:
    """Bundles boot_id + per-camera epoch high-water with persistence.

    Usage:
        ls = LeaseState(path).load()
        ls.bump_and_persist()          # monotonic boot_id, persisted
        ls.record_epoch(cam, epoch)     # high-water, persisted
        floor = ls.epoch_floor(cam)     # receiver fence floor
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.boot_id: int = 0
        self.camera_epochs: Dict[str, int] = {}
        self.first_boot: bool = True
        self.corrupt: bool = False

    def load(self, *, fail_safe_high: bool = True) -> "LeaseState":
        state = load_lease_state(self.path, fail_safe_high=fail_safe_high)
        self.boot_id = state["boot_id"]
        self.camera_epochs = state["camera_epochs"]
        self.first_boot = state["first_boot"]
        self.corrupt = state["corrupt"]
        return self

    def bump_and_persist(self) -> "LeaseState":
        """Monotonic boot_id: loaded + 1, then persist. Returns self so callers
        can chain (e.g. ``LeaseState(path).load().bump_and_persist()`` yields a
        usable lease object). New boot_id is on ``self.boot_id``.
        """
        self.boot_id = self.boot_id + 1
        self._save()
        return self

    def epoch_floor(self, cam_id: str) -> int:
        """High-water epoch for cam_id, or -1 if never seen."""
        return self.camera_epochs.get(cam_id, -1)

    def record_epoch(self, cam_id: str, epoch: int) -> None:
        """Raise the per-camera high-water to ``epoch`` (if higher) and persist."""
        e = int(epoch)
        if e > self.camera_epochs.get(cam_id, -1):
            self.camera_epochs[cam_id] = e
            self._save()

    def _save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "schema": SCHEMA_VERSION,
                "boot_id": self.boot_id,
                "camera_epochs": self.camera_epochs,
                "updated_at": time.time(),
            },
        )
