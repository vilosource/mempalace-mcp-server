"""M2 admin tools: get_aaak_spec, hook_settings, memories_filed_away."""

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

TOKEN = "test-token-admin-m2"
TOKEN_SHA = hashlib.sha256(TOKEN.encode()).hexdigest()
TEST_ROOT = Path(tempfile.mkdtemp(prefix="mempalace-m2-admin-"))


def _prep_palace() -> None:
    (TEST_ROOT / "palace").mkdir(parents=True, exist_ok=True)
    c = chromadb.PersistentClient(path=str(TEST_ROOT / "palace"))
    c.get_or_create_collection(name="mempalace_drawers", metadata={"hnsw:space": "cosine"})
    kg = sqlite3.connect(TEST_ROOT / "knowledge_graph.sqlite3")
    kg.execute(
        "CREATE TABLE triples (id TEXT PRIMARY KEY, subject TEXT, "
        "predicate TEXT, object TEXT)"
    )
    kg.commit()
    kg.close()
    migrate(TEST_ROOT, embedding_model="all-MiniLM-L6-v2",
            embedding_dim=384, snapshot_taken=True)


def _cfg() -> ServerConfig:
    return ServerConfig(
        data_root=TEST_ROOT,
        bind=BindConfig(host="127.0.0.1", port=0, metrics_path=None),
        embedding=EmbeddingConfig(),
        auth=AuthConfig(tokens=[TokenEntry(token_sha256=TOKEN_SHA, identity="admin")]),
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
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def _hdrs() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.asyncio
async def test_aaak_spec_is_static(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            r1 = json.loads((await s.call_tool("mempalace_get_aaak_spec", {})).content[0].text)
            r2 = json.loads((await s.call_tool("mempalace_get_aaak_spec", {})).content[0].text)
    assert r1 == r2
    assert "AAAK" in r1["aaak_spec"]


@pytest.mark.asyncio
async def test_reconnect_is_noop(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool("mempalace_reconnect", {})).content[0].text)
    assert res == {"success": True, "noop": True}


@pytest.mark.asyncio
async def test_hook_settings_round_trip(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            # Read current (no mutation args) — changed list empty.
            read = json.loads((await s.call_tool("mempalace_hook_settings", {})).content[0].text)
            assert read["changed"] == []

            # Set silent_save true.
            wrote = json.loads((await s.call_tool("mempalace_hook_settings", {
                "silent_save": True,
            })).content[0].text)
            assert "silent_save" in wrote["changed"]
            assert wrote["hooks"]["silent_save"] is True
            assert wrote["caller_id"] == "admin"

    # Verify persisted in palace config.json.
    cfg = json.loads((TEST_ROOT / "config.json").read_text())
    assert cfg["hooks"]["silent_save"] is True


@pytest.mark.asyncio
async def test_hook_settings_emits_wal_entry(server_url):
    wal = [json.loads(ln) for ln in
           (TEST_ROOT / "wal" / "write_log.jsonl").read_text().splitlines() if ln]
    hooks_entries = [e for e in wal if e["operation"] == "mempalace_hook_settings"]
    assert hooks_entries, "hook_settings must produce a WAL entry"
    for e in hooks_entries:
        assert e["caller_id"] == "admin"
        assert "request_id" in e


@pytest.mark.asyncio
async def test_memories_filed_away_quiet_when_no_checkpoint(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool(
                "mempalace_memories_filed_away", {}
            )).content[0].text)
    assert res["status"] == "quiet"
    assert res["count"] == 0


@pytest.mark.asyncio
async def test_memories_filed_away_consumes_checkpoint(server_url):
    state = TEST_ROOT / "hook_state"
    state.mkdir(exist_ok=True)
    (state / "last_checkpoint").write_text(json.dumps({"msgs": 7, "ts": "2026-04-21T00:00:00Z"}))
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool(
                "mempalace_memories_filed_away", {}
            )).content[0].text)
    assert res["status"] == "ok"
    assert res["count"] == 7
    # File must be gone after ack.
    assert not (state / "last_checkpoint").exists()
