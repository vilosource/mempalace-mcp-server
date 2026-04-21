"""Drawer-level tools — add / update / delete / get / list / search.

M1 writes: add_drawer, update_drawer, delete_drawer. M2 adds readers.
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

    @mcp.tool()
    async def mempalace_update_drawer(
        drawer_id: str,
        content: str | None = None,
        wing: str | None = None,
        room: str | None = None,
    ) -> dict:
        """Update a drawer's content/wing/room. Re-stamps caller_id on the row."""
        args = {
            "drawer_id": drawer_id,
            "content": content,
            "wing": wing,
            "room": room,
        }

        async def _impl(*, caller_id: str, drawer_id: str,
                        content: str | None, wing: str | None,
                        room: str | None) -> dict:
            existing = palace.drawers.get(
                ids=[drawer_id], include=["documents", "metadatas"]
            )
            if not existing["ids"]:
                return {"success": False, "drawer_id": drawer_id, "reason": "not_found"}
            old_doc = existing["documents"][0]
            old_meta = dict(existing["metadatas"][0])
            new_doc = content if content is not None else old_doc
            new_meta = dict(old_meta)
            if wing is not None:
                new_meta["wing"] = wing
            if room is not None:
                new_meta["room"] = room
            new_meta["caller_id"] = caller_id
            new_meta["filed_at"] = datetime.now(timezone.utc).isoformat()
            palace.drawers.update(
                ids=[drawer_id],
                documents=[new_doc] if content is not None else None,
                metadatas=[new_meta],
            )
            return {
                "success": True,
                "drawer_id": drawer_id,
                "changed": {
                    "content": content is not None,
                    "wing": wing is not None,
                    "room": room is not None,
                },
            }

        return await dispatch_write("mempalace_update_drawer", _impl, args, wal=wal)

    @mcp.tool()
    async def mempalace_delete_drawer(drawer_id: str) -> dict:
        """Delete a drawer by ID. Irreversible."""
        args = {"drawer_id": drawer_id}

        async def _impl(*, caller_id: str, drawer_id: str) -> dict:
            existing = palace.drawers.get(ids=[drawer_id], include=["metadatas"])
            if not existing["ids"]:
                return {"success": False, "drawer_id": drawer_id, "reason": "not_found"}
            palace.drawers.delete(ids=[drawer_id])
            return {
                "success": True,
                "drawer_id": drawer_id,
                "was_caller_id": existing["metadatas"][0].get("caller_id"),
            }

        return await dispatch_write("mempalace_delete_drawer", _impl, args, wal=wal)
