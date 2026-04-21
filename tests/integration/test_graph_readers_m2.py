"""M2 tunnel + graph readers."""

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

TOKEN = "test-token-graph-m2"
TOKEN_SHA = hashlib.sha256(TOKEN.encode()).hexdigest()
TEST_ROOT = Path(tempfile.mkdtemp(prefix="mempalace-m2-graph-"))


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
        auth=AuthConfig(tokens=[TokenEntry(token_sha256=TOKEN_SHA, identity="grapher")]),
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

    # Seed drawers whose (wing, room) metadata exercises both derived and
    # explicit graph views.
    import asyncio as _aio

    async def _seed():
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp",
                                         headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                seeds = [
                    # "auth" room appears in both 'code' and 'docs' -> derived edge
                    ("code", "auth", "auth handler impl"),
                    ("docs", "auth", "auth runbook"),
                    # "deploy" appears only in 'code' -> no cross-wing edge
                    ("code", "deploy", "deploy step"),
                    ("notes", "ideas", "vague idea"),
                ]
                for wing, room, content in seeds:
                    await s.call_tool("mempalace_add_drawer", {
                        "wing": wing, "room": room, "content": content,
                    })
                # Explicit tunnel between notes/ideas and code/auth.
                await s.call_tool("mempalace_create_tunnel", {
                    "source_wing": "notes", "source_room": "ideas",
                    "target_wing": "code", "target_room": "auth",
                    "label": "ideas link to auth",
                })

    _aio.run(_seed())

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def _hdrs() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.asyncio
async def test_list_tunnels_all_and_filtered(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            all_t = json.loads((await s.call_tool("mempalace_list_tunnels", {})).content[0].text)
            assert all_t["count"] == 1

            notes = json.loads((await s.call_tool("mempalace_list_tunnels", {
                "wing": "notes",
            })).content[0].text)
            assert notes["count"] == 1

            nobody = json.loads((await s.call_tool("mempalace_list_tunnels", {
                "wing": "nonexistent",
            })).content[0].text)
            assert nobody["count"] == 0


@pytest.mark.asyncio
async def test_follow_tunnels(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            # From notes/ideas, we should see code/auth as the other side.
            hit = json.loads((await s.call_tool("mempalace_follow_tunnels", {
                "wing": "notes", "room": "ideas",
            })).content[0].text)
    assert hit["count"] == 1
    assert hit["connections"][0]["other_wing"] == "code"
    assert hit["connections"][0]["other_room"] == "auth"


@pytest.mark.asyncio
async def test_find_tunnels_explicit_and_derived(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool("mempalace_find_tunnels", {})).content[0].text)
    # Explicit: the tunnel we created.
    assert len(res["explicit_tunnels"]) == 1
    # Derived: 'auth' room spans 'code' + 'docs'.
    derived_rooms = {d["room"] for d in res["derived_cross_wing_rooms"]}
    assert "auth" in derived_rooms
    assert "deploy" not in derived_rooms  # deploy exists only in 'code'


@pytest.mark.asyncio
async def test_traverse_from_shared_room(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool("mempalace_traverse", {
                "start_room": "auth", "max_hops": 2,
            })).content[0].text)
    rooms = {v["room"] for v in res["visited"]}
    # Must include start room + any rooms sharing a wing.
    assert "auth" in rooms
    # 'auth' in 'code' shares wing with 'deploy'; 'auth' in 'docs' has no other rooms.
    assert "deploy" in rooms


@pytest.mark.asyncio
async def test_graph_stats_includes_derived_and_explicit(server_url):
    async with streamablehttp_client(f"{server_url}/mcp", headers=_hdrs()) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool("mempalace_graph_stats", {})).content[0].text)
    d = res["derived"]
    # Nodes: (code,auth), (docs,auth), (code,deploy), (notes,ideas) = 4.
    assert d["node_count"] == 4
    # Cross-wing: 'auth' room in 2 wings → 1 edge.
    assert d["edge_count"] == 1
    assert res["explicit_tunnels"]["count"] == 1
