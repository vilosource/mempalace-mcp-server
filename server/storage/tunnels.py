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


# ── Read operations ──────────────────────────────────────────────────────

def load_all(data_root: Path) -> list[dict]:
    return _load(data_root)


def list_for_wing(data_root: Path, wing: str | None) -> list[dict]:
    tunnels = _load(data_root)
    if not wing:
        return tunnels
    return [
        t for t in tunnels
        if t.get("source", {}).get("wing") == wing
        or t.get("target", {}).get("wing") == wing
    ]


def endpoints_at(tunnel: dict, wing: str, room: str) -> bool:
    src = tunnel.get("source", {})
    tgt = tunnel.get("target", {})
    return ((src.get("wing") == wing and src.get("room") == room)
            or (tgt.get("wing") == wing and tgt.get("room") == room))


def follow(data_root: Path, wing: str, room: str) -> list[dict]:
    """Return tunnels that have (wing, room) as one endpoint, with the 'other
    side' spelled out as a separate field for callers."""
    hits = []
    for t in _load(data_root):
        if not endpoints_at(t, wing, room):
            continue
        src = t.get("source", {})
        tgt = t.get("target", {})
        if src.get("wing") == wing and src.get("room") == room:
            other = tgt
        else:
            other = src
        hits.append({
            "tunnel_id": t.get("id"),
            "label": t.get("label", ""),
            "other_wing": other.get("wing"),
            "other_room": other.get("room"),
            "other_drawer_id": other.get("drawer_id"),
            "created_at": t.get("created_at"),
            "caller_id": t.get("caller_id"),
        })
    return hits


def find_across_wings(
    data_root: Path, wing_a: str | None, wing_b: str | None,
) -> list[dict]:
    """Tunnels spanning two specific wings (unordered pair). If only one wing
    is supplied, returns tunnels where at least one endpoint is in that wing
    and the other is in a different wing."""
    results = []
    for t in _load(data_root):
        sw = t.get("source", {}).get("wing")
        tw = t.get("target", {}).get("wing")
        if sw == tw:
            continue  # only cross-wing tunnels
        if wing_a and wing_b:
            pair = {sw, tw}
            if pair != {wing_a, wing_b}:
                continue
        elif wing_a:
            if wing_a not in (sw, tw):
                continue
        results.append(t)
    return results
