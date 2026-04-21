"""M1 write-path coverage: update/delete drawer, diary_write.

Shares the fixture shape from test_add_drawer_m1.py but with its own
disposable palace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import socket
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import chromadb
import pytest
import sqlite3
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from server.config import (
    AuthConfig, BindConfig, EmbeddingConfig, LoggingConfig,
    ServerConfig, TokenEntry, WalConfig,
)
from server.main import build_app
from server.migrate import migrate

TOKEN = "test-token-m1-writes"
TOKEN_SHA = hashlib.sha256(TOKEN.encode()).hexdigest()
TEST_ROOT = Path(tempfile.mkdtemp(prefix="mempalace-m1-writes-"))


def _prep_palace() -> None:
    (TEST_ROOT / "palace").mkdir(parents=True, exist_ok=True)
    c = chromadb.PersistentClient(path=str(TEST_ROOT / "palace"))
    c.get_or_create_collection(name="mempalace_drawers", metadata={"hnsw:space": "cosine"})
    kg = sqlite3.connect(TEST_ROOT / "knowledge_graph.sqlite3")
    kg.execute("""CREATE TABLE triples (
        id TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT
    )""")
    kg.commit()
    kg.close()
    migrate(TEST_ROOT, embedding_model="all-MiniLM-L6-v2",
            embedding_dim=384, snapshot_taken=True)


def _cfg() -> ServerConfig:
    return ServerConfig(
        data_root=TEST_ROOT,
        bind=BindConfig(host="127.0.0.1", port=0, metrics_path=None),
        embedding=EmbeddingConfig(),
        auth=AuthConfig(tokens=[TokenEntry(token_sha256=TOKEN_SHA, identity="tester")]),
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


def _wal_lines() -> list[dict]:
    p = TEST_ROOT / "wal" / "write_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_update_then_delete_roundtrip(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()

            add = json.loads((await s.call_tool("mempalace_add_drawer", {
                "wing": "test", "room": "updates",
                "content": "original content for update+delete test",
            })).content[0].text)
            did = add["drawer_id"]
            assert add["success"]

            upd = json.loads((await s.call_tool("mempalace_update_drawer", {
                "drawer_id": did,
                "content": "rewritten body for update test",
            })).content[0].text)
            assert upd["success"] and upd["changed"]["content"]

            get = json.loads((await s.call_tool("mempalace_status", {})).content[0].text)
            assert get["drawer_count"] >= 1

            delete = json.loads((await s.call_tool("mempalace_delete_drawer", {
                "drawer_id": did,
            })).content[0].text)
            assert delete["success"]
            assert delete["was_caller_id"] == "tester"

    # Verify all three ops show up in WAL with caller_id.
    wal = _wal_lines()
    ops = {e["operation"] for e in wal}
    assert {"mempalace_add_drawer", "mempalace_update_drawer",
            "mempalace_delete_drawer"}.issubset(ops)
    for e in wal:
        assert e["caller_id"] == "tester", f"missing caller_id on {e['operation']}"
        assert "request_id" in e


@pytest.mark.asyncio
async def test_update_preserves_unmodified_fields(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            add = json.loads((await s.call_tool("mempalace_add_drawer", {
                "wing": "orig", "room": "orig",
                "content": "preserve-fields test body",
                "source_file": "spec.md",
                "added_by": "pytest",
            })).content[0].text)
            did = add["drawer_id"]

            # Update only wing; room + added_by + source_file must persist.
            await s.call_tool("mempalace_update_drawer", {
                "drawer_id": did, "wing": "new_wing",
            })

    c = chromadb.PersistentClient(path=str(TEST_ROOT / "palace"))
    col = c.get_or_create_collection(name="mempalace_drawers")
    meta = col.get(ids=[did], include=["metadatas"])["metadatas"][0]
    assert meta["wing"] == "new_wing"
    assert meta["room"] == "orig"
    assert meta["source_file"] == "spec.md"
    assert meta["added_by"] == "pytest"
    assert meta["caller_id"] == "tester"  # re-stamped on update


@pytest.mark.asyncio
async def test_delete_missing_drawer(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool("mempalace_delete_drawer", {
                "drawer_id": "drawer_nonexistent_room_0123456789abcdef01234567",
            })).content[0].text)
    assert res["success"] is False
    assert res["reason"] == "not_found"


@pytest.mark.asyncio
async def test_diary_write(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool("mempalace_diary_write", {
                "agent_name": "TestAgent",
                "entry": "today I wrote integration tests",
                "topic": "testing",
            })).content[0].text)
    assert res["success"]
    assert res["wing"] == "wing_testagent"
    assert res["room"] == "diary"

    c = chromadb.PersistentClient(path=str(TEST_ROOT / "palace"))
    col = c.get_or_create_collection(name="mempalace_drawers")
    meta = col.get(ids=[res["entry_id"]], include=["metadatas"])["metadatas"][0]
    assert meta["agent_name"] == "TestAgent"
    assert meta["topic"] == "testing"
    assert meta["caller_id"] == "tester"
    assert meta["ingest_mode"] == "diary"
