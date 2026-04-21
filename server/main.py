"""Server entrypoint + app factory.

Usage (with uvicorn factory mode):
    MEMPALACE_SERVER_CONFIG=./config.yaml \
        uvicorn --factory server.main:build_app --host 0.0.0.0 --port 8080

Tests build directly via build_app(explicit_cfg).
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.routing import Route

from server import __version__
from server.auth import AuthMiddleware, TokenResolver
from server.config import ServerConfig, load_config
from server.logging import configure_logging
from server.storage.palace import Palace
from server.tools import admin as admin_tools
from server.tools import diary as diary_tools
from server.tools import drawers as drawer_tools
from server.tools import kg as kg_tools
from server.tools import tunnels as tunnel_tools
from server.wal import WalWriter


def build_app(cfg: ServerConfig | None = None):
    """Construct the ASGI app. Eager palace load so /healthz only answers when ready."""
    if cfg is None:
        cfg = load_config()
    configure_logging(cfg.logging)

    palace = Palace(cfg)
    palace.open()

    wal_path = Path(cfg.data_root) / "wal" / "write_log.jsonl"
    wal = WalWriter(wal_path, redact_keys=cfg.wal.redact_keys)

    mcp = FastMCP("mempalace-server")
    admin_tools.register(mcp, palace)
    drawer_tools.register(mcp, palace, wal)
    diary_tools.register(mcp, palace, wal)
    kg_tools.register(mcp, palace, wal)
    tunnel_tools.register(mcp, palace, wal)

    async def healthz(request):
        try:
            count = palace.drawers.count()
            return JSONResponse({
                "status": "ok",
                "drawer_count": count,
                "embedding_model": cfg.embedding.model,
                "embedding_dim": cfg.embedding.dim,
                "version": __version__,
            })
        except Exception as e:
            return JSONResponse(
                {"status": "degraded", "reason": str(e)},
                status_code=503,
            )

    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))

    resolver = TokenResolver(cfg.auth)
    app = AuthMiddleware(app, resolver)

    # Stash references for diagnostics (not load-bearing).
    app.palace = palace  # type: ignore[attr-defined]
    app.wal = wal  # type: ignore[attr-defined]
    return app


