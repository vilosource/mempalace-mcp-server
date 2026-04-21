"""Palace lifecycle — open the data root, validate embedding model, hold clients.

Replaces MemPalace's import-time singleton pattern (mcp_server.py:92-123)
with explicit construction owned by the server's lifespan.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import chromadb

from server.config import ServerConfig
from server.errors import EmbeddingModelMismatch


class Palace:
    """Holds Chroma client + KG connection for one data root."""

    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.data_root: Path = cfg.data_root
        self.chroma_path = self.data_root / "palace"
        self.kg_path = self.data_root / "knowledge_graph.sqlite3"
        self.palace_config_path = self.data_root / "config.json"
        self._chroma_client: chromadb.ClientAPI | None = None
        self._drawers_col: chromadb.Collection | None = None
        self._kg_conn: sqlite3.Connection | None = None
        self._palace_config: dict = {}

    def open(self) -> None:
        """Boot: validate palace config, open Chroma, open KG."""
        if self.palace_config_path.exists():
            self._palace_config = json.loads(self.palace_config_path.read_text())
        else:
            self._palace_config = {}

        if self.cfg.embedding.enforce_match:
            p_model = self._palace_config.get("embedding_model")
            p_dim = self._palace_config.get("embedding_dim")
            if p_model and p_model != self.cfg.embedding.model:
                raise EmbeddingModelMismatch(
                    f"palace uses model {p_model}; server configured with "
                    f"{self.cfg.embedding.model}; refusing to write mixed vectors"
                )
            if p_dim and p_dim != self.cfg.embedding.dim:
                raise EmbeddingModelMismatch(
                    f"palace uses dim {p_dim}; server configured with "
                    f"{self.cfg.embedding.dim}"
                )

        self._chroma_client = chromadb.PersistentClient(path=str(self.chroma_path))
        self._drawers_col = self._chroma_client.get_or_create_collection(
            name="mempalace_drawers",
            metadata={"hnsw:space": "cosine"},
        )
        self._kg_conn = sqlite3.connect(str(self.kg_path), check_same_thread=False)
        self._kg_conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        if self._kg_conn is not None:
            self._kg_conn.close()

    @property
    def drawers(self) -> chromadb.Collection:
        assert self._drawers_col is not None, "Palace not opened"
        return self._drawers_col

    @property
    def kg(self) -> sqlite3.Connection:
        assert self._kg_conn is not None, "Palace not opened"
        return self._kg_conn

    @property
    def palace_config(self) -> dict:
        return self._palace_config
