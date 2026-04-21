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

    # ── Readers ────────────────────────────────────────────────────────────

    @mcp.tool()
    async def mempalace_get_drawer(drawer_id: str) -> dict:
        """Fetch a drawer by ID."""
        args = {"drawer_id": drawer_id}

        async def _impl(*, caller_id: str, drawer_id: str) -> dict:
            got = palace.drawers.get(
                ids=[drawer_id], include=["documents", "metadatas"]
            )
            if not got["ids"]:
                return {"found": False, "drawer_id": drawer_id}
            return {
                "found": True,
                "drawer_id": drawer_id,
                "content": got["documents"][0],
                "metadata": got["metadatas"][0],
            }

        from server.dispatch import dispatch_read
        return await dispatch_read("mempalace_get_drawer", _impl, args)

    @mcp.tool()
    async def mempalace_list_drawers(
        wing: str | None = None,
        room: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Paginated list of drawers, optionally filtered by wing/room."""
        args = {"wing": wing, "room": room, "limit": limit, "offset": offset}

        async def _impl(*, caller_id: str, wing, room, limit, offset) -> dict:
            limit = max(1, min(int(limit), 200))
            offset = max(0, int(offset))
            where = _build_where(wing, room)
            got = palace.drawers.get(
                where=where,
                include=["metadatas"],
                limit=limit,
                offset=offset,
            )
            return {
                "drawers": [
                    {"drawer_id": did, "wing": m.get("wing"), "room": m.get("room"),
                     "source_file": m.get("source_file"), "filed_at": m.get("filed_at"),
                     "added_by": m.get("added_by"), "caller_id": m.get("caller_id")}
                    for did, m in zip(got["ids"], got["metadatas"])
                ],
                "limit": limit,
                "offset": offset,
                "total_in_palace": palace.drawers.count(),
            }

        from server.dispatch import dispatch_read
        return await dispatch_read("mempalace_list_drawers", _impl, args)

    @mcp.tool()
    async def mempalace_search(
        query: str,
        limit: int = 5,
        wing: str | None = None,
        room: str | None = None,
        max_distance: float = 1.5,
    ) -> dict:
        """Vector search. Hybrid rerank (closet boost + BM25) lands in a
        follow-up; the read shape is stable so callers don't change."""
        args = {"query": query, "limit": limit, "wing": wing,
                "room": room, "max_distance": max_distance}

        async def _impl(*, caller_id: str, query, limit, wing, room,
                        max_distance) -> dict:
            where = _build_where(wing, room)
            raw = palace.drawers.query(
                query_texts=[query],
                n_results=max(1, min(int(limit), 50)),
                where=where,
            )
            results = []
            for i, did in enumerate(raw["ids"][0]):
                dist = raw["distances"][0][i]
                if dist > max_distance:
                    continue
                meta = raw["metadatas"][0][i]
                results.append({
                    "drawer_id": did,
                    "text": raw["documents"][0][i],
                    "wing": meta.get("wing"),
                    "room": meta.get("room"),
                    "source_file": meta.get("source_file"),
                    "filed_at": meta.get("filed_at"),
                    "caller_id": meta.get("caller_id"),
                    "distance": dist,
                    "similarity": round(1 - dist, 3),
                })
            return {"query": query, "results": results, "count": len(results),
                    "filters": {"wing": wing, "room": room}}

        from server.dispatch import dispatch_read
        return await dispatch_read("mempalace_search", _impl, args)

    @mcp.tool()
    async def mempalace_check_duplicate(
        content: str,
        threshold: float = 0.95,
    ) -> dict:
        """Probe for near-duplicate drawers of `content`."""
        args = {"content": content, "threshold": threshold}

        async def _impl(*, caller_id: str, content, threshold) -> dict:
            raw = palace.drawers.query(query_texts=[content], n_results=3)
            hits = []
            for i, did in enumerate(raw["ids"][0]):
                sim = 1 - raw["distances"][0][i]
                if sim >= threshold:
                    hits.append({
                        "drawer_id": did,
                        "similarity": round(sim, 3),
                        "wing": raw["metadatas"][0][i].get("wing"),
                        "room": raw["metadatas"][0][i].get("room"),
                    })
            return {"has_duplicate": bool(hits), "matches": hits,
                    "threshold": threshold}

        from server.dispatch import dispatch_read
        return await dispatch_read("mempalace_check_duplicate", _impl, args)


def _build_where(wing: str | None, room: str | None):
    if wing and room:
        return {"$and": [{"wing": wing}, {"room": room}]}
    if wing:
        return {"wing": wing}
    if room:
        return {"room": room}
    return None
