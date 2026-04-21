"""Knowledge-graph tools — kg_add, kg_invalidate (M1 writes).

kg_query / kg_timeline / kg_stats land in M2.
"""

from __future__ import annotations

from server.dispatch import dispatch_write
from server.storage import kg as kg_store
from server.storage.palace import Palace
from server.wal import WalWriter


def register(mcp, palace: Palace, wal: WalWriter):

    @mcp.tool()
    async def mempalace_kg_add(
        subject: str,
        predicate: str,
        object: str,
        valid_from: str | None = None,
        source_closet: str | None = None,
    ) -> dict:
        """Add a `subject predicate object` triple with optional provenance."""
        args = {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "valid_from": valid_from,
            "source_closet": source_closet,
        }

        async def _impl(*, caller_id: str, subject: str, predicate: str,
                        object: str, valid_from: str | None,
                        source_closet: str | None) -> dict:
            return kg_store.add_triple(
                palace.kg,
                subject=subject,
                predicate=predicate,
                obj=object,
                valid_from=valid_from,
                source_closet=source_closet,
                caller_id=caller_id,
            )

        return await dispatch_write("mempalace_kg_add", _impl, args, wal=wal)

    @mcp.tool()
    async def mempalace_kg_invalidate(
        subject: str,
        predicate: str,
        object: str,
        ended: str | None = None,
    ) -> dict:
        """Mark a triple as no longer valid by setting valid_to."""
        args = {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "ended": ended,
        }

        async def _impl(*, caller_id: str, subject: str, predicate: str,
                        object: str, ended: str | None) -> dict:
            return kg_store.invalidate(
                palace.kg,
                subject=subject,
                predicate=predicate,
                obj=object,
                ended=ended,
            )

        return await dispatch_write("mempalace_kg_invalidate", _impl, args, wal=wal)
