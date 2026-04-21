"""M1 tunnel writes — closes the pre-existing WAL gap."""

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

TOKEN = "test-token-tunnels-m1"
TOKEN_SHA = hashlib.sha256(TOKEN.encode()).hexdigest()
TEST_ROOT = Path(tempfile.mkdtemp(prefix="mempalace-m1-tunnels-"))


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
        auth=AuthConfig(tokens=[TokenEntry(token_sha256=TOKEN_SHA, identity="tun_tester")]),
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


@pytest.mark.asyncio
async def test_create_tunnel_is_symmetric(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            ab = json.loads((await s.call_tool("mempalace_create_tunnel", {
                "source_wing": "auth", "source_room": "oauth",
                "target_wing": "users", "target_room": "profiles",
                "label": "auth↔users",
            })).content[0].text)
            # Same endpoints reversed — should resolve to the same tunnel ID.
            ba = json.loads((await s.call_tool("mempalace_create_tunnel", {
                "source_wing": "users", "source_room": "profiles",
                "target_wing": "auth", "target_room": "oauth",
                "label": "updated label",
            })).content[0].text)
    assert ab["tunnel"]["id"] == ba["tunnel"]["id"]
    assert ab["created"] is True
    assert ba["created"] is False  # updated existing
    assert ba["tunnel"]["label"] == "updated label"
    assert ba["tunnel"]["caller_id"] == "tun_tester"


@pytest.mark.asyncio
async def test_delete_tunnel(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            created = json.loads((await s.call_tool("mempalace_create_tunnel", {
                "source_wing": "x", "source_room": "y",
                "target_wing": "p", "target_room": "q",
                "label": "ephemeral",
            })).content[0].text)
            tid = created["tunnel"]["id"]
            gone = json.loads((await s.call_tool("mempalace_delete_tunnel", {
                "tunnel_id": tid,
            })).content[0].text)
    assert gone["deleted"] is True
    assert gone["tunnel_id"] == tid

    # Verify gone from disk.
    import json as _json
    tunnels = _json.loads((TEST_ROOT / "tunnels.json").read_text())
    assert all(t["id"] != tid for t in tunnels)


@pytest.mark.asyncio
async def test_tunnel_ops_are_wal_logged(server_url):
    """MemPalace stdio never logged tunnel mutations. Server must."""
    import json as _json
    wal = TEST_ROOT / "wal" / "write_log.jsonl"
    entries = [_json.loads(line) for line in wal.read_text().splitlines() if line.strip()]
    ops = {e["operation"] for e in entries}
    assert "mempalace_create_tunnel" in ops
    assert "mempalace_delete_tunnel" in ops
    for e in entries:
        if e["operation"].startswith("mempalace_"):
            assert e["caller_id"] == "tun_tester"
            assert "request_id" in e
