"""M2 readers: drawers, metadata, KG, diary. Vector-only search; hybrid TBD."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import chromadb
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from server.config import (
    AuthConfig, BindConfig, EmbeddingConfig, LoggingConfig,
    ServerConfig, TokenEntry, WalConfig,
)
from server.main import build_app
from server.migrate import migrate

TOKEN = "test-token-readers-m2"
TOKEN_SHA = hashlib.sha256(TOKEN.encode()).hexdigest()
TEST_ROOT = Path(tempfile.mkdtemp(prefix="mempalace-m2-readers-"))


def _prep_palace() -> None:
    (TEST_ROOT / "palace").mkdir(parents=True, exist_ok=True)
    c = chromadb.PersistentClient(path=str(TEST_ROOT / "palace"))
    c.get_or_create_collection(name="mempalace_drawers", metadata={"hnsw:space": "cosine"})
    kg = sqlite3.connect(TEST_ROOT / "knowledge_graph.sqlite3")
    kg.executescript("""
        CREATE TABLE entities (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            type TEXT DEFAULT 'unknown', properties TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE triples (
            id TEXT PRIMARY KEY,
            subject TEXT, predicate TEXT, object TEXT,
            valid_from TEXT, valid_to TEXT,
            confidence REAL DEFAULT 1.0,
            source_closet TEXT, source_file TEXT,
            source_drawer_id TEXT, adapter_name TEXT,
            extracted_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    kg.commit()
    kg.close()
    migrate(TEST_ROOT, embedding_model="all-MiniLM-L6-v2",
            embedding_dim=384, snapshot_taken=True)


def _cfg() -> ServerConfig:
    return ServerConfig(
        data_root=TEST_ROOT,
        bind=BindConfig(host="127.0.0.1", port=0, metrics_path=None),
        embedding=EmbeddingConfig(),
        auth=AuthConfig(tokens=[TokenEntry(token_sha256=TOKEN_SHA, identity="reader")]),
        logging=LoggingConfig(format="console"),
        wal=WalConfig(),
    )


@pytest.fixture(scope="module")
def server_url():
    _prep_palace()
    app = build_app(_cfg())
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    t.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("server did not become ready")

    # Seed a varied fixture: 3 wings × 2-3 rooms × a few drawers each.
    import asyncio as _aio

    async def _seed():
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp",
                                         headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                seeds = [
                    ("code", "backend", "fastapi route handlers for auth"),
                    ("code", "backend", "postgres connection pool tuning"),
                    ("code", "frontend", "react component state management"),
                    ("code", "frontend", "tailwind dark-mode strategy"),
                    ("docs", "runbook", "how to roll a new database role"),
                    ("docs", "runbook", "oncall escalation procedure for the weekend"),
                    ("docs", "adr", "why we chose SQLite for the knowledge graph"),
                    ("notes", "journal", "reflections on the quarter's migration work"),
                ]
                for wing, room, content in seeds:
                    await s.call_tool("mempalace_add_drawer", {
                        "wing": wing, "room": room, "content": content,
                    })
                await s.call_tool("mempalace_diary_write", {
                    "agent_name": "ReaderAgent",
                    "entry": "logged in for reader smoke tests",
                    "topic": "smoke",
                })
                await s.call_tool("mempalace_diary_write", {
                    "agent_name": "ReaderAgent",
                    "entry": "second diary entry for pagination",
                })
                await s.call_tool("mempalace_kg_add", {
                    "subject": "Alice", "predicate": "knows", "object": "Bob",
                    "valid_from": "2025-06-01",
                })
                await s.call_tool("mempalace_kg_add", {
                    "subject": "Alice", "predicate": "owns", "object": "Cat",
                })
                await s.call_tool("mempalace_kg_add", {
                    "subject": "Bob", "predicate": "works_at", "object": "Acme",
                    "valid_from": "2024-01-01",
                })

    _aio.run(_seed())

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def _hdrs() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.asyncio
async def test_get_drawer_and_list_drawers(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            listed = json.loads((await s.call_tool("mempalace_list_drawers", {
                "wing": "code", "room": "backend",
            })).content[0].text)
            assert {d["room"] for d in listed["drawers"]} == {"backend"}
            first = listed["drawers"][0]["drawer_id"]

            got = json.loads((await s.call_tool("mempalace_get_drawer", {
                "drawer_id": first,
            })).content[0].text)
            assert got["found"] is True
            assert got["metadata"]["wing"] == "code"
            assert got["metadata"]["caller_id"] == "reader"


@pytest.mark.asyncio
async def test_list_wings_and_rooms_and_taxonomy(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            wings = json.loads((await s.call_tool("mempalace_list_wings", {})).content[0].text)
            names = {w["wing"] for w in wings["wings"]}
            assert {"code", "docs", "notes"}.issubset(names)

            code_rooms = json.loads((await s.call_tool("mempalace_list_rooms", {
                "wing": "code",
            })).content[0].text)
            rooms = {r["room"] for r in code_rooms["rooms"]}
            assert rooms == {"backend", "frontend"}

            tax = json.loads((await s.call_tool("mempalace_get_taxonomy", {})).content[0].text)
            assert "code" in tax["taxonomy"]
            assert tax["taxonomy"]["code"]["drawer_count"] == 4


@pytest.mark.asyncio
async def test_search_vector_only(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            r1 = json.loads((await s.call_tool("mempalace_search", {
                "query": "database connection settings", "limit": 3,
            })).content[0].text)
            # The postgres pool drawer should rank near the top.
            assert any("postgres" in hit["text"] for hit in r1["results"])

            # Filter by wing — docs results only.
            r2 = json.loads((await s.call_tool("mempalace_search", {
                "query": "escalation for the weekend", "wing": "docs", "limit": 5,
            })).content[0].text)
            assert all(hit["wing"] == "docs" for hit in r2["results"])


@pytest.mark.asyncio
async def test_check_duplicate(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            # The exact text of one of the seeded drawers should hit at ~1.0 similarity.
            r1 = json.loads((await s.call_tool("mempalace_check_duplicate", {
                "content": "postgres connection pool tuning",
                "threshold": 0.95,
            })).content[0].text)
            assert r1["has_duplicate"] is True
            # Far-off text should not match.
            r2 = json.loads((await s.call_tool("mempalace_check_duplicate", {
                "content": "a recipe for chocolate brownies that won a bake-off",
                "threshold": 0.95,
            })).content[0].text)
            assert r2["has_duplicate"] is False


@pytest.mark.asyncio
async def test_diary_read(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool("mempalace_diary_read", {
                "agent_name": "ReaderAgent", "last_n": 5,
            })).content[0].text)
    assert res["wing"] == "wing_readeragent"
    assert res["total"] == 2
    assert res["entries"][0]["filed_at"] >= res["entries"][1]["filed_at"]


@pytest.mark.asyncio
async def test_kg_query_kg_timeline_kg_stats(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            q = json.loads((await s.call_tool("mempalace_kg_query", {
                "entity": "Alice", "direction": "both",
            })).content[0].text)
            preds_out = {f["predicate"] for f in q["facts"] if f["direction"] == "out"}
            assert preds_out == {"knows", "owns"}

            q_as_of = json.loads((await s.call_tool("mempalace_kg_query", {
                "entity": "Alice", "direction": "out", "as_of": "2024-12-31",
            })).content[0].text)
            # Alice→knows→Bob has valid_from 2025-06-01, so should not appear for 2024-12-31.
            preds_then = {f["predicate"] for f in q_as_of["facts"]}
            assert "knows" not in preds_then

            tl = json.loads((await s.call_tool("mempalace_kg_timeline", {
                "entity": "Bob",
            })).content[0].text)
            assert tl["fact_count"] >= 2  # knows(alice,bob) + works_at(bob,acme)

            stats = json.loads((await s.call_tool("mempalace_kg_stats", {})).content[0].text)
            assert stats["triples_total"] == 3
            assert stats["entities"] >= 4  # alice, bob, cat, acme


@pytest.mark.asyncio
async def test_readers_produce_no_wal_entries(server_url):
    """Sanity: readers go through dispatch_read, not dispatch_write, so no WAL."""
    import json as _json
    wal_path = TEST_ROOT / "wal" / "write_log.jsonl"
    wal_entries = [
        _json.loads(line) for line in wal_path.read_text().splitlines() if line.strip()
    ]
    reader_ops = {
        "mempalace_get_drawer", "mempalace_list_drawers", "mempalace_search",
        "mempalace_check_duplicate", "mempalace_list_wings",
        "mempalace_list_rooms", "mempalace_get_taxonomy",
        "mempalace_diary_read", "mempalace_kg_query",
        "mempalace_kg_timeline", "mempalace_kg_stats", "mempalace_status",
    }
    assert not any(e["operation"] in reader_ops for e in wal_entries), \
        "readers must not produce WAL entries"
