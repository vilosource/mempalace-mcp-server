"""Tunnels — explicit undirected links between rooms, stored as tunnels.json.

Lifts the core semantics from `mempalace/palace_graph.py` (v3.3.0):
  - canonical tunnel ID is the sha256 of the sorted endpoint pair, so
    create(A,B) and create(B,A) resolve to the same record.
  - atomic writes via .tmp + os.replace (no partial-write truncation).
  - caller_id stamped on create (new). MemPalace's stdio version did not
    WAL-log tunnel mutations at all; the server closes that gap via the
    dispatch chokepoint.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _tunnel_file(data_root: Path) -> Path:
    return data_root / "tunnels.json"


def _load(data_root: Path) -> list[dict]:
    p = _tunnel_file(data_root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _save(data_root: Path, tunnels: list[dict]) -> None:
    p = _tunnel_file(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tunnels, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, p)


def _canonical_id(sw: str, sr: str, tw: str, tr: str) -> str:
    a, b = sorted((f"{sw}/{sr}", f"{tw}/{tr}"))
    return hashlib.sha256(f"{a}↔{b}".encode("utf-8")).hexdigest()[:16]


def create(
    data_root: Path,
    *,
    source_wing: str,
    source_room: str,
    target_wing: str,
    target_room: str,
    label: str,
    source_drawer_id: str | None,
    target_drawer_id: str | None,
    caller_id: str,
) -> dict:
    """Insert or update a tunnel. Returns the resulting record."""
    for name, val in [
        ("source_wing", source_wing), ("source_room", source_room),
        ("target_wing", target_wing), ("target_room", target_room),
    ]:
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{name} must be a non-empty string")

    tid = _canonical_id(source_wing, source_room, target_wing, target_room)
    now = datetime.now(timezone.utc).isoformat()

    tunnels = _load(data_root)
    for t in tunnels:
        if t.get("id") == tid:
            t["label"] = label
            t["source"]["drawer_id"] = source_drawer_id
            t["target"]["drawer_id"] = target_drawer_id
            t["updated_at"] = now
            t["caller_id"] = caller_id
            _save(data_root, tunnels)
            return {"created": False, "tunnel": t}

    tunnel = {
        "id": tid,
        "source": {"wing": source_wing, "room": source_room,
                   "drawer_id": source_drawer_id},
        "target": {"wing": target_wing, "room": target_room,
                   "drawer_id": target_drawer_id},
        "label": label,
        "created_at": now,
        "updated_at": now,
        "caller_id": caller_id,
    }
    tunnels.append(tunnel)
    _save(data_root, tunnels)
    return {"created": True, "tunnel": tunnel}


def delete(data_root: Path, tunnel_id: str) -> dict:
    tunnels = _load(data_root)
    before = len(tunnels)
    remaining = [t for t in tunnels if t.get("id") != tunnel_id]
    _save(data_root, remaining)
    return {
        "success": True,
        "tunnel_id": tunnel_id,
        "deleted": before != len(remaining),
    }
