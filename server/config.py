"""Server YAML config loader.

Schema per TDD §3. Env override via MEMPALACE_SERVER_* prefix.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class BindConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    metrics_path: str | None = "/metrics"


class EmbeddingConfig(BaseModel):
    model: str = "all-MiniLM-L6-v2"
    dim: int = 384
    enforce_match: bool = True


class TokenEntry(BaseModel):
    token_sha256: str = Field(..., pattern=r"^[0-9a-fA-F]{64}$")
    identity: str


class AuthConfig(BaseModel):
    tokens: list[TokenEntry] = Field(default_factory=list)
    read_policy: Literal["required", "mapped_or_none"] = "required"


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"


class WalConfig(BaseModel):
    redact_keys: list[str] = Field(default_factory=lambda: [
        "content", "content_preview", "document", "entry",
        "entry_preview", "query", "text",
    ])


class ServerConfig(BaseModel):
    data_root: Path
    bind: BindConfig = Field(default_factory=BindConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    wal: WalConfig = Field(default_factory=WalConfig)


DEFAULT_PATHS = [
    Path("/etc/mempalace-server/config.yaml"),
    Path.home() / ".config" / "mempalace-server" / "config.yaml",
]


def load_config(path: str | Path | None = None) -> ServerConfig:
    """Load config from `path`, $MEMPALACE_SERVER_CONFIG, or a default location."""
    if path is None:
        env_path = os.environ.get("MEMPALACE_SERVER_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            for p in DEFAULT_PATHS:
                if p.exists():
                    path = p
                    break
    if path is None:
        raise FileNotFoundError(
            "no config found; pass --config, set MEMPALACE_SERVER_CONFIG, "
            f"or create one at {DEFAULT_PATHS[0]} or {DEFAULT_PATHS[1]}"
        )
    with open(path) as f:
        data = yaml.safe_load(f)
    return ServerConfig(**data)
