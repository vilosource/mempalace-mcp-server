"""Spike server — 5 MCP tools over streamable-HTTP.

Tools: mempalace_add_drawer, mempalace_get_drawer, mempalace_search,
       mempalace_kg_query, mempalace_status.

Deliberately minimal:
- No auth.
- No caller_id / attribution chokepoint (stamp code present, identity = "default").
- No WAL redaction, no sanitizers (spike-only).
- Single write lock (asyncio) to preserve MemPalace's single-writer invariant.

The palace data root is expected at $MEMPALACE_SPIKE_ROOT (default:
/tmp/mempalace-spike/).

Run with:
    .venv/bin/uvicorn server:app --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb
from mcp.server.fastmcp import FastMCP

PALACE_ROOT = Path(os.environ.get("MEMPALACE_SPIKE_ROOT", "/tmp/mempalace-spike"))
CHROMA_PATH = PALACE_ROOT / "palace"
KG_PATH = PALACE_ROOT / "knowledge_graph.sqlite3"
CONFIG_PATH = PALACE_ROOT / "config.json"

# --- Palace state (owned by server process) --------------------------------

_chroma_client: chromadb.ClientAPI | None = None
_drawers_col: chromadb.Collection | None = None
_kg_conn: sqlite3.Connection | None = None
_config: dict[str, Any] = {}
_write_lock = asyncio.Lock()
_boot_time_s: float = 0.0


def _load_palace():
    global _chroma_client, _drawers_col, _kg_conn, _config, _boot_time_s
    t0 = time.time()
    _config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    # Embedding-model pin check (simplified — v1 will reject on mismatch).
    configured = _config.get("embedding_model", "all-MiniLM-L6-v2")
    if configured != "all-MiniLM-L6-v2":
        raise RuntimeError(
            f"spike supports only all-MiniLM-L6-v2; palace configured with {configured}"
        )
    _chroma_client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    _drawers_col = _chroma_client.get_or_create_collection(
        name=_config.get("collection_name", "mempalace_drawers"),
        metadata={"hnsw:space": "cosine"},
    )
    _kg_conn = sqlite3.connect(KG_PATH, check_same_thread=False)
    _kg_conn.execute("PRAGMA journal_mode=WAL")
    _boot_time_s = time.time() - t0


# --- MCP server definition --------------------------------------------------

mcp = FastMCP("mempalace-spike")


def _drawer_id(wing: str, room: str, content: str) -> str:
    h = hashlib.sha256(f"{wing}{room}{content}".encode()).hexdigest()[:24]
    return f"drawer_{wing}_{room}_{h}"


def _stamp_caller_id(meta: dict) -> dict:
    """v1 chokepoint stub — always 'default' in the spike."""
    meta["caller_id"] = "default"
    return meta


@mcp.tool()
async def mempalace_status() -> dict:
    """Return palace status: drawer count, collections, boot time."""
    assert _drawers_col is not None
    return {
        "palace_root": str(PALACE_ROOT),
        "collection": _drawers_col.name,
        "drawer_count": _drawers_col.count(),
        "embedding_model": _config.get("embedding_model"),
        "embedding_dim": _config.get("embedding_dim"),
        "boot_time_s": round(_boot_time_s, 3),
    }


@mcp.tool()
async def mempalace_add_drawer(
    wing: str,
    room: str,
    content: str,
    source_file: str | None = None,
    added_by: str = "mcp",
) -> dict:
    """Add a drawer. Deterministic ID; idempotent on identical content."""
    assert _drawers_col is not None
    did = _drawer_id(wing, room, content)
    async with _write_lock:
        existing = _drawers_col.get(ids=[did])
        if existing["ids"]:
            return {"success": True, "drawer_id": did, "reason": "already_exists"}
        meta = _stamp_caller_id({
            "wing": wing,
            "room": room,
            "source_file": source_file or "",
            "added_by": added_by,
            "filed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_index": 0,
            "normalize_version": 2,
        })
        _drawers_col.upsert(documents=[content], ids=[did], metadatas=[meta])
        return {"success": True, "drawer_id": did, "wing": wing, "room": room}


@mcp.tool()
async def mempalace_get_drawer(drawer_id: str) -> dict:
    """Fetch a drawer by ID."""
    assert _drawers_col is not None
    result = _drawers_col.get(ids=[drawer_id], include=["documents", "metadatas"])
    if not result["ids"]:
        return {"found": False, "drawer_id": drawer_id}
    return {
        "found": True,
        "drawer_id": drawer_id,
        "content": result["documents"][0],
        "metadata": result["metadatas"][0],
    }


@mcp.tool()
async def mempalace_search(
    query: str,
    limit: int = 5,
    wing: str | None = None,
    room: str | None = None,
    max_distance: float = 1.5,
) -> dict:
    """Vector-only search (spike). v1 will add closet boost + BM25 rerank."""
    assert _drawers_col is not None
    where: dict[str, Any] | None = None
    if wing and room:
        where = {"$and": [{"wing": wing}, {"room": room}]}
    elif wing:
        where = {"wing": wing}
    elif room:
        where = {"room": room}

    raw = _drawers_col.query(
        query_texts=[query],
        n_results=limit,
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
            "distance": dist,
            "similarity": round(1 - dist, 3),
        })
    return {"query": query, "results": results, "count": len(results)}


@mcp.tool()
async def mempalace_kg_query(
    entity: str,
    as_of: str | None = None,
    direction: str = "both",
) -> dict:
    """Query the knowledge graph for facts about an entity."""
    assert _kg_conn is not None
    eid = entity.lower().replace(" ", "_").replace("'", "")
    cur = _kg_conn.cursor()

    def _filter_time(q: str, args: list) -> tuple[str, list]:
        if as_of:
            q += " AND (valid_from IS NULL OR valid_from <= ?)"
            q += " AND (valid_to IS NULL OR valid_to >= ?)"
            args.extend([as_of, as_of])
        return q, args

    facts: list[dict] = []
    if direction in ("both", "out"):
        q, args = "SELECT subject, predicate, object, valid_from, valid_to, confidence, " \
                  "source_closet, source_drawer_id FROM triples WHERE subject = ?", [eid]
        q, args = _filter_time(q, args)
        for row in cur.execute(q, args):
            facts.append({
                "direction": "out",
                "subject": row[0], "predicate": row[1], "object": row[2],
                "valid_from": row[3], "valid_to": row[4], "confidence": row[5],
                "source_closet": row[6], "source_drawer_id": row[7],
            })
    if direction in ("both", "in"):
        q, args = "SELECT subject, predicate, object, valid_from, valid_to, confidence, " \
                  "source_closet, source_drawer_id FROM triples WHERE object = ?", [eid]
        q, args = _filter_time(q, args)
        for row in cur.execute(q, args):
            facts.append({
                "direction": "in",
                "subject": row[0], "predicate": row[1], "object": row[2],
                "valid_from": row[3], "valid_to": row[4], "confidence": row[5],
                "source_closet": row[6], "source_drawer_id": row[7],
            })
    return {"entity": eid, "fact_count": len(facts), "facts": facts}


# --- ASGI app ---------------------------------------------------------------

from starlette.responses import JSONResponse
from starlette.routing import Route


async def healthz(request):
    return JSONResponse({
        "status": "ok",
        "drawer_count": _drawers_col.count() if _drawers_col else 0,
        "boot_time_s": round(_boot_time_s, 3),
    })


_load_palace()
app = mcp.streamable_http_app()
app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))
