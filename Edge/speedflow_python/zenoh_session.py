"""
speedflow_python/zenoh_session.py

Factory for a Zenoh peer-mode session on the LAN.
All modules call make_session() to get a configured session.

No broker required — Zenoh peer mode uses UDP multicast scouting
so nodes on the same LAN switch discover each other automatically.
"""

from __future__ import annotations

import zenoh


def make_config() -> zenoh.Config:
    cfg = zenoh.Config()
    cfg.insert_json5("mode", '"peer"')
    cfg.insert_json5("scouting/multicast/enabled", "true")
    return cfg


def make_session() -> zenoh.Session:
    return zenoh.open(make_config())
