"""M1 KG writes: kg_add, kg_invalidate with caller_id stamping."""

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

TOKEN = "test-token-kg-m1"
TOKEN_SHA = hashlib.sha256(TOKEN.encode()).hexdigest()
TEST_ROOT = Path(tempfile.mkdtemp(prefix="mempalace-m1-kg-"))


def _prep_palace() -> None:
    (TEST_ROOT / "palace").mkdir(parents=True, exist_ok=True)
    c = chromadb.PersistentClient(path=str(TEST_ROOT / "palace"))
    c.get_or_create_collection(name="mempalace_drawers", metadata={"hnsw:space": "cosine"})
    kg = sqlite3.connect(TEST_ROOT / "knowledge_graph.sqlite3")
    # Full schema incl. RFC 002 provenance columns.
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
        auth=AuthConfig(tokens=[TokenEntry(token_sha256=TOKEN_SHA, identity="kg_tester")]),
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
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("server did not become ready")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def _hdrs() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _kg_rows(sql: str, params: tuple = ()) -> list[tuple]:
    c = sqlite3.connect(TEST_ROOT / "knowledge_graph.sqlite3")
    try:
        return list(c.execute(sql, params).fetchall())
    finally:
        c.close()


@pytest.mark.asyncio
async def test_kg_add_creates_triple_and_entities(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool("mempalace_kg_add", {
                "subject": "Alice",
                "predicate": "knows",
                "object": "Bob",
                "valid_from": "2026-01-01",
            })).content[0].text)
    assert res["created"] is True
    assert res["subject"] == "alice" and res["object"] == "bob"

    # Entities auto-created with display names preserved.
    entities = _kg_rows("SELECT id, name FROM entities ORDER BY id")
    names = {eid: name for eid, name in entities}
    assert names["alice"] == "Alice"
    assert names["bob"] == "Bob"

    # Triple carries caller_id.
    rows = _kg_rows(
        "SELECT subject, predicate, object, valid_from, caller_id "
        "FROM triples WHERE subject='alice'"
    )
    assert rows == [("alice", "knows", "bob", "2026-01-01", "kg_tester")]


@pytest.mark.asyncio
async def test_kg_add_is_idempotent_while_valid(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            args = {"subject": "Carol", "predicate": "owns", "object": "Dog"}
            r1 = json.loads((await s.call_tool("mempalace_kg_add", args)).content[0].text)
            r2 = json.loads((await s.call_tool("mempalace_kg_add", args)).content[0].text)
    assert r1["triple_id"] == r2["triple_id"]
    assert r1["created"] is True and r2["created"] is False


@pytest.mark.asyncio
async def test_kg_invalidate_sets_valid_to(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            await s.call_tool("mempalace_kg_add", {
                "subject": "Erin", "predicate": "works_at", "object": "Acme",
                "valid_from": "2025-01-01",
            })
            inv = json.loads((await s.call_tool("mempalace_kg_invalidate", {
                "subject": "Erin", "predicate": "works_at", "object": "Acme",
                "ended": "2026-03-01",
            })).content[0].text)
    assert inv["success"] and inv["rows_affected"] == 1

    rows = _kg_rows(
        "SELECT valid_from, valid_to FROM triples WHERE subject='erin'"
    )
    assert rows == [("2025-01-01", "2026-03-01")]


@pytest.mark.asyncio
async def test_kg_wal_includes_caller_id(server_url):
    import json as _json
    wal = TEST_ROOT / "wal" / "write_log.jsonl"
    entries = [_json.loads(line) for line in wal.read_text().splitlines() if line.strip()]
    ops = [e for e in entries if e["operation"] in ("mempalace_kg_add",
                                                    "mempalace_kg_invalidate")]
    assert ops, "no KG ops in WAL"
    for e in ops:
        assert e["caller_id"] == "kg_tester"
        assert "request_id" in e
