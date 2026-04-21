"""Tunnel tools — create, delete (M1 writes).

Closes the pre-existing WAL gap from MemPalace stdio: every tunnel
mutation routes through dispatch_write and produces a WAL entry with
caller_id.
"""

from __future__ import annotations

from server.dispatch import dispatch_write
from server.storage import tunnels as tunnels_store
from server.storage.palace import Palace
from server.wal import WalWriter


def register(mcp, palace: Palace, wal: WalWriter):

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
