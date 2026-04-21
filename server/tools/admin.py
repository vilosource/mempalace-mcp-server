"""Admin / meta tools — status, reconnect, hook_settings, etc.

M1 ships `status` only. Others land as the PRD's admin-rescoping rules
require.
"""

from __future__ import annotations

from typing import Any

from server import __version__
from server.dispatch import dispatch_read
from server.storage.palace import Palace


def register(mcp, palace: Palace):
    @mcp.tool()
    async def mempalace_status() -> dict:
        async def _impl(**_kw: Any) -> dict:
            return {
                "version": __version__,
                "palace_root": str(palace.data_root),
                "collection": palace.drawers.name,
                "drawer_count": palace.drawers.count(),
                "embedding_model": palace.cfg.embedding.model,
                "embedding_dim": palace.cfg.embedding.dim,
            }
        return await dispatch_read("mempalace_status", _impl, {})

    @mcp.tool()
    async def mempalace_reconnect() -> dict:
        """No-op under the server (see PRD Architecture §Tool surface).

        The stdio version invalidated per-process Chroma caches.
        The shared server owns the client for its lifetime.
        """
        async def _impl(**_kw: Any) -> dict:
            return {"success": True, "noop": True}
        return await dispatch_read("mempalace_reconnect", _impl, {})
