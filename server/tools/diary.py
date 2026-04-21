"""Diary tools — specialized drawers keyed by agent + date.

Follows MemPalace's convention: wing = f"wing_{agent_name.lower()}",
room = "diary", entry stored as a drawer. The diary_id embeds the
agent_name + timestamp to support multiple entries per day.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from server.dispatch import dispatch_read, dispatch_write
from server.storage.palace import Palace
from server.wal import WalWriter


def register(mcp, palace: Palace, wal: WalWriter):

    @mcp.tool()
    async def mempalace_diary_read(
        agent_name: str,
        last_n: int = 10,
    ) -> dict:
        """Return the most recent diary entries for `agent_name`."""
        args = {"agent_name": agent_name, "last_n": last_n}

        async def _impl(*, caller_id: str, agent_name, last_n) -> dict:
            wing = f"wing_{agent_name.lower()}"
            got = palace.drawers.get(
                where={"$and": [{"wing": wing}, {"room": "diary"}]},
                include=["documents", "metadatas"],
            )
            entries = [
                {
                    "entry_id": did,
                    "entry": doc,
                    "topic": m.get("topic", ""),
                    "filed_at": m.get("filed_at", ""),
                    "caller_id": m.get("caller_id"),
                }
                for did, doc, m in zip(got["ids"], got["documents"], got["metadatas"])
            ]
            entries.sort(key=lambda e: e["filed_at"], reverse=True)
            last_n = max(1, min(int(last_n), 100))
            return {
                "agent_name": agent_name,
                "wing": wing,
                "entries": entries[:last_n],
                "total": len(entries),
            }

        return await dispatch_read("mempalace_diary_read", _impl, args)

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
