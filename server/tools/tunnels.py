"""Tunnel tools — create, delete (M1 writes).

Closes the pre-existing WAL gap from MemPalace stdio: every tunnel
mutation routes through dispatch_write and produces a WAL entry with
caller_id.
"""

from __future__ import annotations

from server.dispatch import dispatch_read, dispatch_write
from server.storage import graph as graph_store
from server.storage import tunnels as tunnels_store
from server.storage.palace import Palace
from server.wal import WalWriter


def register(mcp, palace: Palace, wal: WalWriter):

    # ── Explicit-tunnel readers (tunnels.json) ─────────────────────────────

    @mcp.tool()
    async def mempalace_list_tunnels(wing: str | None = None) -> dict:
        """Return explicit tunnels, optionally filtered by wing membership."""
        args = {"wing": wing}

        async def _impl(*, caller_id: str, wing) -> dict:
            t = tunnels_store.list_for_wing(palace.data_root, wing)
            return {"tunnels": t, "count": len(t), "filter_wing": wing}

        return await dispatch_read("mempalace_list_tunnels", _impl, args)

    @mcp.tool()
    async def mempalace_follow_tunnels(wing: str, room: str) -> dict:
        """Follow tunnels from (wing, room). Returns the other endpoint
        of each connected tunnel."""
        args = {"wing": wing, "room": room}

        async def _impl(*, caller_id: str, wing, room) -> dict:
            hits = tunnels_store.follow(palace.data_root, wing, room)
            return {"wing": wing, "room": room,
                    "connections": hits, "count": len(hits)}

        return await dispatch_read("mempalace_follow_tunnels", _impl, args)

    # ── Derived graph readers (from drawer metadata) ────────────────────────

    @mcp.tool()
    async def mempalace_find_tunnels(
        wing_a: str | None = None,
        wing_b: str | None = None,
    ) -> dict:
        """Find explicit cross-wing tunnels + rooms spanning both wings via
        shared metadata. Returns both views separately so callers can pick."""
        args = {"wing_a": wing_a, "wing_b": wing_b}

        async def _impl(*, caller_id: str, wing_a, wing_b) -> dict:
            explicit = tunnels_store.find_across_wings(
                palace.data_root, wing_a, wing_b
            )
            derived = graph_store.find_cross_wing_rooms(
                palace.drawers, wing_a, wing_b
            )
            return {
                "wing_a": wing_a, "wing_b": wing_b,
                "explicit_tunnels": explicit,
                "derived_cross_wing_rooms": derived,
            }

        return await dispatch_read("mempalace_find_tunnels", _impl, args)

    @mcp.tool()
    async def mempalace_traverse(
        start_room: str,
        max_hops: int = 2,
    ) -> dict:
        """BFS from `start_room` over shared-wing neighbors."""
        args = {"start_room": start_room, "max_hops": max_hops}

        async def _impl(*, caller_id: str, start_room, max_hops) -> dict:
            return graph_store.traverse(
                palace.drawers, start_room=start_room, max_hops=max_hops
            )

        return await dispatch_read("mempalace_traverse", _impl, args)

    @mcp.tool()
    async def mempalace_graph_stats() -> dict:
        """Node + edge counts in the derived palace graph."""
        async def _impl(*, caller_id: str) -> dict:
            derived = graph_store.graph_stats(palace.drawers)
            explicit_tunnels = tunnels_store.load_all(palace.data_root)
            return {
                "derived": derived,
                "explicit_tunnels": {"count": len(explicit_tunnels)},
            }
        return await dispatch_read("mempalace_graph_stats", _impl, {})

    # ── Writers ────────────────────────────────────────────────────────────

    @mcp.tool()
    async def mempalace_create_tunnel(
        source_wing: str,
        source_room: str,
        target_wing: str,
        target_room: str,
        label: str = "",
        source_drawer_id: str | None = None,
        target_drawer_id: str | None = None,
    ) -> dict:
        """Create/update an undirected tunnel between two rooms."""
        args = {
            "source_wing": source_wing,
            "source_room": source_room,
            "target_wing": target_wing,
            "target_room": target_room,
            "label": label,
            "source_drawer_id": source_drawer_id,
            "target_drawer_id": target_drawer_id,
        }

        async def _impl(*, caller_id: str, **kw) -> dict:
            return tunnels_store.create(
                palace.data_root,
                caller_id=caller_id,
                **kw,
            )

        return await dispatch_write("mempalace_create_tunnel", _impl, args, wal=wal)

    @mcp.tool()
    async def mempalace_delete_tunnel(tunnel_id: str) -> dict:
        """Delete a tunnel by its canonical ID."""
        args = {"tunnel_id": tunnel_id}

        async def _impl(*, caller_id: str, tunnel_id: str) -> dict:
            return tunnels_store.delete(palace.data_root, tunnel_id)

        return await dispatch_write("mempalace_delete_tunnel", _impl, args, wal=wal)
