"""Knowledge-graph helpers — minimal port of MemPalace's KG write path.

Lifts the semantics of `add_triple` / `invalidate` / `_entity_id` from
`mempalace/knowledge_graph.py` (v3.3.0), adapted for:
  - the connection lifecycle owned by server.storage.palace.Palace
  - caller_id as an additional column (per TDD §4.2)
  - no per-instance threading.Lock (server dispatch handles write serialization)

Read operations (query_entity, kg_timeline, kg_stats) land in M2.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone


def entity_id(name: str) -> str:
    """Normalize a human name into the canonical entity id used by MemPalace."""
    return name.lower().replace(" ", "_").replace("'", "")


def _triple_id(sub_id: str, pred: str, obj_id: str, valid_from: str | None) -> str:
    now = datetime.now(timezone.utc).isoformat()
    suffix = hashlib.sha256(f"{valid_from}{now}".encode("utf-8")).hexdigest()[:12]
    return f"t_{sub_id}_{pred}_{obj_id}_{suffix}"


def add_triple(
    kg: sqlite3.Connection,
    *,
    subject: str,
    predicate: str,
    obj: str,
    valid_from: str | None,
    source_closet: str | None,
    caller_id: str,
    confidence: float = 1.0,
    source_file: str | None = None,
    source_drawer_id: str | None = None,
    adapter_name: str | None = None,
) -> dict:
    """Insert or reuse a triple. Auto-creates subject/object entity rows.

    Returns the triple id and whether a new row was inserted. Idempotent on
    (subject, predicate, object) while a row with valid_to IS NULL exists.
    """
    sub_id = entity_id(subject)
    obj_id = entity_id(obj)
    pred = predicate.lower().replace(" ", "_")

    with kg:
        kg.execute(
            "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
            (sub_id, subject),
        )
        kg.execute(
            "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
            (obj_id, obj),
        )

        existing = kg.execute(
            "SELECT id FROM triples "
            "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
            (sub_id, pred, obj_id),
        ).fetchone()
        if existing:
            return {"triple_id": existing[0], "created": False,
                    "subject": sub_id, "predicate": pred, "object": obj_id}

        tid = _triple_id(sub_id, pred, obj_id, valid_from)
        kg.execute(
            """INSERT INTO triples (
                id, subject, predicate, object,
                valid_from, valid_to, confidence,
                source_closet, source_file,
                source_drawer_id, adapter_name,
                caller_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tid, sub_id, pred, obj_id,
                valid_from, None, confidence,
                source_closet, source_file,
                source_drawer_id, adapter_name,
                caller_id,
            ),
        )
    return {"triple_id": tid, "created": True,
            "subject": sub_id, "predicate": pred, "object": obj_id}


def invalidate(
    kg: sqlite3.Connection,
    *,
    subject: str,
    predicate: str,
    obj: str,
    ended: str | None,
) -> dict:
    """Mark a relationship as no longer valid by setting valid_to."""
    sub_id = entity_id(subject)
    obj_id = entity_id(obj)
    pred = predicate.lower().replace(" ", "_")
    ended = ended or date.today().isoformat()

    with kg:
        cur = kg.execute(
            "UPDATE triples SET valid_to=? "
            "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
            (ended, sub_id, pred, obj_id),
        )
        affected = cur.rowcount
    return {
        "success": True,
        "subject": sub_id, "predicate": pred, "object": obj_id,
        "ended": ended, "rows_affected": affected,
    }
