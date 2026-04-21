"""Admin / meta tools.

- `mempalace_status` — palace + server state.
- `mempalace_reconnect` — semantic no-op under the shared server.
- `mempalace_get_aaak_spec` — static AAAK dialect specification.
- `mempalace_hook_settings` — server-scoped (not per-client) hook config.
  Goes through dispatch_write: mutates palace config.json.
- `mempalace_memories_filed_away` — reads and clears the checkpoint
  marker, so it also mutates state (via dispatch_write).
"""

from __future__ import annotations

import json
from typing import Any

from server import __version__
from server.dispatch import dispatch_read, dispatch_write
from server.storage.palace import Palace
from server.wal import WalWriter


AAAK_SPEC = """AAAK is a compressed memory dialect that MemPalace uses for efficient storage.
It is designed to be readable by both humans and LLMs without decoding.

FORMAT:
  ENTITIES: 3-letter uppercase codes. ALC=Alice, JOR=Jordan, RIL=Riley, MAX=Max, BEN=Ben.
  EMOTIONS: *action markers* before/during text. *warm*=joy, *fierce*=determined, *raw*=vulnerable, *bloom*=tenderness.
  STRUCTURE: Pipe-separated fields. FAM: family | PROJ: projects | ⚠: warnings/reminders.
  DATES: ISO format (2026-03-31). COUNTS: Nx = N mentions (e.g., 570x).
  IMPORTANCE: ★ to ★★★★★ (1-5 scale).
  HALLS: hall_facts, hall_events, hall_discoveries, hall_preferences, hall_advice.
  WINGS: wing_user, wing_agent, wing_team, wing_code, wing_myproject, wing_hardware, wing_ue5, wing_ai_research.
  ROOMS: Hyphenated slugs representing named ideas (e.g., chromadb-setup, gpu-pricing).

EXAMPLE:
  FAM: ALC→♡JOR | 2D(kids): RIL(18,sports) MAX(11,chess+swimming) | BEN(contributor)

Read AAAK naturally — expand codes mentally, treat *markers* as emotional context.
When WRITING AAAK: use entity codes, mark emotions, keep structure tight."""


def register(mcp, palace: Palace, wal: WalWriter | None = None):
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
        """No-op under the server (see PRD Architecture §Tool surface)."""
        async def _impl(**_kw: Any) -> dict:
            return {"success": True, "noop": True}
        return await dispatch_read("mempalace_reconnect", _impl, {})

    @mcp.tool()
    async def mempalace_get_aaak_spec() -> dict:
        """Return the AAAK dialect specification (static)."""
        async def _impl(**_kw: Any) -> dict:
            return {"aaak_spec": AAAK_SPEC}
        return await dispatch_read("mempalace_get_aaak_spec", _impl, {})

    if wal is not None:
        _register_mutators(mcp, palace, wal)


def _register_mutators(mcp, palace: Palace, wal: WalWriter):

    @mcp.tool()
    async def mempalace_hook_settings(
        silent_save: bool | None = None,
        desktop_toast: bool | None = None,
    ) -> dict:
        """Get or set server-scoped hook settings in palace config.json.

        Under the shared server this is admin-level: a write affects every
        client that shares the palace. Call with no args to read current.
        """
        args = {"silent_save": silent_save, "desktop_toast": desktop_toast}

        async def _impl(*, caller_id: str, silent_save, desktop_toast) -> dict:
            path = palace.palace_config_path
            if path.exists():
                cfg = json.loads(path.read_text())
            else:
                cfg = {}
            hooks = cfg.setdefault("hooks", {})
            changed = []
            if silent_save is not None:
                hooks["silent_save"] = bool(silent_save)
                changed.append("silent_save")
            if desktop_toast is not None:
                hooks["desktop_toast"] = bool(desktop_toast)
                changed.append("desktop_toast")
            if changed:
                path.write_text(json.dumps(cfg, indent=2) + "\n")
            return {
                "success": True,
                "changed": changed,
                "hooks": hooks,
                "caller_id": caller_id,
            }

        return await dispatch_write("mempalace_hook_settings", _impl, args, wal=wal)

    @mcp.tool()
    async def mempalace_memories_filed_away() -> dict:
        """Acknowledge + clear the latest silent checkpoint marker."""
        args: dict = {}

        async def _impl(*, caller_id: str) -> dict:
            ack_file = palace.data_root / "hook_state" / "last_checkpoint"
            if not ack_file.is_file():
                return {
                    "status": "quiet",
                    "message": "No recent journal entry",
                    "count": 0,
                    "timestamp": None,
                }
            try:
                data = json.loads(ack_file.read_text(encoding="utf-8"))
                ack_file.unlink(missing_ok=True)
                return {
                    "status": "ok",
                    "message": f"✦ {data.get('msgs', 0)} messages tucked into drawers",
                    "count": data.get("msgs", 0),
                    "timestamp": data.get("ts"),
                }
            except (json.JSONDecodeError, OSError):
                ack_file.unlink(missing_ok=True)
                return {
                    "status": "error",
                    "message": "✦ Journal entry filed in the palace",
                    "count": 0,
                    "timestamp": None,
                }

        return await dispatch_write("mempalace_memories_filed_away", _impl, args, wal=wal)
