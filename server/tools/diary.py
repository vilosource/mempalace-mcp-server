"""Diary tools — specialized drawers keyed by agent + date.

Follows MemPalace's convention: wing = f"wing_{agent_name.lower()}",
room = "diary", entry stored as a drawer. The diary_id embeds the
agent_name + timestamp to support multiple entries per day.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from server.dispatch import dispatch_write
from server.storage.palace import Palace
from server.wal import WalWriter


def register(mcp, palace: Palace, wal: WalWriter):

    @mcp.tool()
    async def mempalace_diary_write(
        agent_name: str,
        entry: str,
        topic: str | None = None,
    ) -> dict:
        """Append a diary entry for `agent_name`."""
        args = {"agent_name": agent_name, "entry": entry, "topic": topic}

        async def _impl(*, caller_id: str, agent_name: str, entry: str,
                        topic: str | None) -> dict:
            wing = f"wing_{agent_name.lower()}"
            room = "diary"
            ts = datetime.now(timezone.utc).isoformat()
            h = hashlib.sha256(
                f"{agent_name}{ts}{entry}".encode("utf-8")
            ).hexdigest()[:24]
            entry_id = f"drawer_{wing}_{room}_{h}"
            meta: dict[str, Any] = {
                "wing": wing,
                "room": room,
                "agent_name": agent_name,
                "topic": topic or "",
                "filed_at": ts,
                "chunk_index": 0,
                "normalize_version": 2,
                "ingest_mode": "diary",
                "caller_id": caller_id,
            }
            palace.drawers.upsert(
                documents=[entry],
                ids=[entry_id],
                metadatas=[meta],
            )
            return {
                "success": True,
                "entry_id": entry_id,
                "wing": wing,
                "room": room,
            }

        return await dispatch_write("mempalace_diary_write", _impl, args, wal=wal)
