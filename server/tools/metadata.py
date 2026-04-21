"""Metadata-scan readers — list_wings, list_rooms, get_taxonomy.

These scan Chroma drawer metadata and aggregate in Python. Adequate up
to ~100K drawers; cache or paginate if a palace grows beyond that.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from server.dispatch import dispatch_read
from server.storage.palace import Palace


def register(mcp, palace: Palace):

    @mcp.tool()
    async def mempalace_list_wings() -> dict:
        """Return all distinct wings with drawer counts."""
        async def _impl(*, caller_id: str) -> dict:
            got = palace.drawers.get(include=["metadatas"])
            counts = Counter(m.get("wing", "") for m in got["metadatas"])
            counts.pop("", None)
            return {
                "wings": [
                    {"wing": w, "drawer_count": n}
                    for w, n in sorted(counts.items())
                ],
                "total": sum(counts.values()),
            }
        return await dispatch_read("mempalace_list_wings", _impl, {})

    @mcp.tool()
    async def mempalace_list_rooms(wing: str | None = None) -> dict:
        """Return all distinct rooms, optionally filtered to one wing."""
        args = {"wing": wing}

        async def _impl(*, caller_id: str, wing) -> dict:
            where = {"wing": wing} if wing else None
            got = palace.drawers.get(where=where, include=["metadatas"])
            counts: Counter[tuple[str, str]] = Counter()
            for m in got["metadatas"]:
                w = m.get("wing", "")
                r = m.get("room", "")
                if w and r:
                    counts[(w, r)] += 1
            return {
                "rooms": [
                    {"wing": w, "room": r, "drawer_count": n}
                    for (w, r), n in sorted(counts.items())
                ],
                "filter_wing": wing,
            }
        return await dispatch_read("mempalace_list_rooms", _impl, args)

    @mcp.tool()
    async def mempalace_get_taxonomy() -> dict:
        """Hierarchical wing → room → drawer count tree."""
        async def _impl(*, caller_id: str) -> dict:
            got = palace.drawers.get(include=["metadatas"])
            tree: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            for m in got["metadatas"]:
                w = m.get("wing", "")
                r = m.get("room", "")
                if w and r:
                    tree[w][r] += 1
            return {
                "taxonomy": {
                    wing: {
                        "drawer_count": sum(rooms.values()),
                        "rooms": dict(sorted(rooms.items())),
                    }
                    for wing, rooms in sorted(tree.items())
                },
                "total_drawers": sum(
                    sum(rs.values()) for rs in tree.values()
                ),
            }
        return await dispatch_read("mempalace_get_taxonomy", _impl, {})
