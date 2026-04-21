"""Palace-graph — nodes and edges derived from drawer metadata.

MemPalace builds an implicit graph from drawer metadata:
  - nodes  = unique (wing, room) pairs carrying drawers
  - edges  = rooms that appear in multiple wings, connected by 'hall'

`traverse` is a BFS from a start room over that graph; `graph_stats`
reports node/edge counts. These derived queries hit only Chroma
metadata; no tunnels.json involvement.

This is a lean v1 port. MemPalace's palace_graph.py has richer
semantics (hall-matched edges, drawer enrichment on traversal) that we
will fill in as needs surface.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any


def _collect_nodes_and_rooms(drawers) -> tuple[dict[tuple[str, str], int],
                                                dict[str, set[str]]]:
    """Return (node_count_by_(wing,room), wings_per_room)."""
    got = drawers.get(include=["metadatas"])
    node_count: dict[tuple[str, str], int] = defaultdict(int)
    wings_per_room: dict[str, set[str]] = defaultdict(set)
    for m in got["metadatas"]:
        w = m.get("wing", "")
        r = m.get("room", "")
        if w and r:
            node_count[(w, r)] += 1
            wings_per_room[r].add(w)
    return node_count, wings_per_room


def graph_stats(drawers) -> dict:
    node_count, wings_per_room = _collect_nodes_and_rooms(drawers)
    cross_wing_rooms = {r for r, ws in wings_per_room.items() if len(ws) > 1}
    # Edge count: for each cross-wing room, C(len(ws), 2).
    edges = 0
    for ws in wings_per_room.values():
        n = len(ws)
        if n > 1:
            edges += n * (n - 1) // 2
    return {
        "node_count": len(node_count),
        "edge_count": edges,
        "unique_rooms": len(wings_per_room),
        "rooms_spanning_wings": sorted(cross_wing_rooms),
    }


def find_cross_wing_rooms(
    drawers, wing_a: str | None, wing_b: str | None,
) -> list[dict]:
    _, wings_per_room = _collect_nodes_and_rooms(drawers)
    results = []
    for room, wings in wings_per_room.items():
        if len(wings) < 2:
            continue
        if wing_a and wing_b:
            if wing_a not in wings or wing_b not in wings:
                continue
            results.append({"room": room, "wings": sorted(wings)})
        elif wing_a:
            if wing_a not in wings:
                continue
            results.append({"room": room, "wings": sorted(wings)})
        else:
            results.append({"room": room, "wings": sorted(wings)})
    return results


def traverse(drawers, start_room: str, max_hops: int = 2) -> dict[str, Any]:
    """BFS from `start_room` through cross-wing room-matching edges.

    Each hop moves to another room in the same wing (via shared wing),
    which is effectively neighboring-room co-traversal. Returns the
    visit order with wings observed.
    """
    _, wings_per_room = _collect_nodes_and_rooms(drawers)
    if start_room not in wings_per_room:
        return {"start_room": start_room, "visited": [],
                "reason": "start_room has no drawers"}

    visited: list[dict] = []
    seen: set[str] = {start_room}
    queue: deque[tuple[str, int]] = deque([(start_room, 0)])
    max_hops = max(0, min(int(max_hops), 6))

    while queue:
        room, hops = queue.popleft()
        visited.append({
            "room": room,
            "hops": hops,
            "wings": sorted(wings_per_room[room]),
        })
        if hops >= max_hops:
            continue
        # Neighbors: rooms that share a wing with the current room.
        current_wings = wings_per_room[room]
        for other_room, other_wings in wings_per_room.items():
            if other_room in seen:
                continue
            if current_wings & other_wings:  # any shared wing
                seen.add(other_room)
                queue.append((other_room, hops + 1))

    return {
        "start_room": start_room,
        "max_hops": max_hops,
        "visited": visited,
        "node_count": len(visited),
    }
