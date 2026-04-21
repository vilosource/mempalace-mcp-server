"""Drawer-level tools — add / update / delete / get / list / search.

M1 scope: `mempalace_add_drawer`. Others land incrementally.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server.dispatch import dispatch_write
from server.storage.drawer_id import drawer_id
from server.storage.palace import Palace
from server.wal import WalWriter


def register(mcp, palace: Palace, wal: WalWriter):

    @mcp.tool()
    async def mempalace_add_drawer(
        wing: str,
        room: str,
        content: str,
        source_file: str | None = None,
        added_by: str = "mcp",
    ) -> dict:
        """Add a drawer. Idempotent on identical (wing, room, content)."""
        args = {
            "wing": wing,
            "room": room,
            "content": content,
            "source_file": source_file,
            "added_by": added_by,
        }

        async def _impl(*, caller_id: str, wing: str, room: str, content: str,
                        source_file: str | None, added_by: str) -> dict:
            did = drawer_id(wing, room, content)
            existing = palace.drawers.get(ids=[did])
            if existing["ids"]:
                return {"success": True, "drawer_id": did, "reason": "already_exists"}
            meta: dict[str, Any] = {
                "wing": wing,
                "room": room,
                "source_file": source_file or "",
                "added_by": added_by,
                "filed_at": datetime.now(timezone.utc).isoformat(),
                "chunk_index": 0,
                "normalize_version": 2,
                "caller_id": caller_id,
            }
            palace.drawers.upsert(
                documents=[content],
                ids=[did],
                metadatas=[meta],
            )
            return {"success": True, "drawer_id": did, "wing": wing, "room": room}

        return await dispatch_write("mempalace_add_drawer", _impl, args, wal=wal)
