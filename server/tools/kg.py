"""Knowledge-graph tools — kg_add, kg_invalidate (M1 writes).

kg_query / kg_timeline / kg_stats land in M2.
"""

from __future__ import annotations

from server.dispatch import dispatch_read, dispatch_write
from server.storage import kg as kg_store
from server.storage.palace import Palace
from server.wal import WalWriter


def register(mcp, palace: Palace, wal: WalWriter):

    # ── Readers ────────────────────────────────────────────────────────────

    @mcp.tool()
    async def mempalace_kg_query(
        entity: str,
        as_of: str | None = None,
        direction: str = "both",
    ) -> dict:
        """Facts about `entity` (out / in / both) with optional temporal filter."""
        args = {"entity": entity, "as_of": as_of, "direction": direction}

        async def _impl(*, caller_id: str, entity, as_of, direction) -> dict:
            facts = kg_store.query_entity(
                palace.kg, entity=entity, as_of=as_of, direction=direction
            )
            return {"entity": kg_store.entity_id(entity),
                    "fact_count": len(facts), "facts": facts,
                    "as_of": as_of, "direction": direction}

        return await dispatch_read("mempalace_kg_query", _impl, args)

    @mcp.tool()
    async def mempalace_kg_timeline(entity: str | None = None) -> dict:
        """Chronological facts (optionally scoped to one entity)."""
        args = {"entity": entity}

        async def _impl(*, caller_id: str, entity) -> dict:
            tl = kg_store.timeline(palace.kg, entity=entity)
            return {"entity_filter": entity, "fact_count": len(tl), "timeline": tl}

        return await dispatch_read("mempalace_kg_timeline", _impl, args)

    @mcp.tool()
    async def mempalace_kg_stats() -> dict:
        """Summary counts over entities and triples."""
        async def _impl(*, caller_id: str) -> dict:
            return kg_store.stats(palace.kg)
        return await dispatch_read("mempalace_kg_stats", _impl, {})

    # ── Writers ────────────────────────────────────────────────────────────

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
