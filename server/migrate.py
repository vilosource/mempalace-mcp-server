"""Migration: additive DDL to prepare a palace for the server.

Per TDD §13. Idempotent: safe to re-run. Steps:
  1. Advisory lock (PID file).
  2. ALTER triples ADD COLUMN caller_id if absent; create index.
  3. Ensure palace config.json records embedding_model + embedding_dim.
  4. Write migration marker.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server import __version__


def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == col for row in cur.fetchall())


def migrate(
    data_root: Path,
    *,
    embedding_model: str,
    embedding_dim: int,
    snapshot_taken: bool,
) -> dict[str, Any]:
    data_root = Path(data_root)

    # --- Preflight ---------------------------------------------------------
    if not data_root.exists():
        return {"ok": False, "reason": f"data_root not found: {data_root}"}
    chroma_sqlite = data_root / "palace" / "chroma.sqlite3"
    if not chroma_sqlite.exists():
        return {"ok": False, "reason": f"palace/chroma.sqlite3 not found under {data_root}"}
    if (data_root / ".server-running").exists():
        return {"ok": False, "reason": "server appears to be running; stop it first"}
    if not snapshot_taken:
        return {
            "ok": False,
            "reason": "refusing to run without operator snapshot assertion "
                      "(pass --snapshot-taken after taking a backup)",
        }

    # --- Lock --------------------------------------------------------------
    lock = data_root / ".migration-in-progress"
    if lock.exists():
        return {"ok": False, "reason": "another migration is in progress"}
    lock.write_text(str(os.getpid()))

    steps: list[str] = []

    try:
        # --- DDL on KG -----------------------------------------------------
        kg_path = data_root / "knowledge_graph.sqlite3"
        if kg_path.exists():
            conn = sqlite3.connect(str(kg_path))
            try:
                if _column_exists(conn, "triples", "caller_id"):
                    steps.append("triples.caller_id already present")
                else:
                    conn.execute("ALTER TABLE triples ADD COLUMN caller_id TEXT")
                    steps.append("added triples.caller_id")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_triples_caller_id "
                    "ON triples(caller_id)"
                )
                steps.append("ensured idx_triples_caller_id")
                conn.commit()
            finally:
                conn.close()
        else:
            steps.append("no knowledge_graph.sqlite3; skipped KG DDL")

        # --- Palace config.json -------------------------------------------
        pconf_path = data_root / "config.json"
        if pconf_path.exists():
            pconf = json.loads(pconf_path.read_text())
        else:
            pconf = {}

        pconf_before = dict(pconf)
        existing_model = pconf.get("embedding_model")
        existing_dim = pconf.get("embedding_dim")
        if existing_model and existing_model != embedding_model:
            return {
                "ok": False,
                "reason": f"palace recorded model={existing_model} but server "
                          f"configured model={embedding_model}; refusing to "
                          f"silently change. Re-embed the palace manually.",
            }
        if existing_dim and existing_dim != embedding_dim:
            return {
                "ok": False,
                "reason": f"palace recorded dim={existing_dim} but server "
                          f"configured dim={embedding_dim}.",
            }

        pconf["embedding_model"] = embedding_model
        pconf["embedding_dim"] = embedding_dim
        if pconf != pconf_before:
            pconf_path.write_text(json.dumps(pconf, indent=2) + "\n")
            steps.append(f"wrote embedding_model/embedding_dim to {pconf_path.name}")
        else:
            steps.append("config.json already consistent")

        # --- Marker --------------------------------------------------------
        marker = data_root / ".server-migrated-at"
        marker.write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(),
            "server_version": __version__,
            "embedding_model": embedding_model,
            "embedding_dim": embedding_dim,
        }, indent=2) + "\n")
        steps.append("wrote .server-migrated-at marker")

    finally:
        try:
            lock.unlink()
        except OSError:
            pass

    return {"ok": True, "steps": steps, "data_root": str(data_root)}
