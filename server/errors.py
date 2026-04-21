"""JSON-RPC error codes and exception classes.

Code allocation per TDD §5.3:
  -32601  unknown tool (MCP standard)
  -32602  invalid param (MCP standard)
  -32000  generic internal tool error (MCP standard)
  -32100  auth: missing or malformed bearer token
  -32101  auth: token not in identity map
  -32110  embedding model mismatch
  -32120  storage full
  -32130  palace locked (migration in progress)
  -32140  HNSW segment quarantined
"""

from __future__ import annotations


class MempalaceError(Exception):
    """Base class for server errors that map to JSON-RPC error codes."""

    code: int = -32000
    retriable: bool = False

    def __init__(self, message: str, *, data: dict | None = None):
        super().__init__(message)
        self.data = data or {}


class AuthMissing(MempalaceError):
    code = -32100


class AuthUnmapped(MempalaceError):
    code = -32101


class EmbeddingModelMismatch(MempalaceError):
    code = -32110


class StorageFull(MempalaceError):
    code = -32120
    retriable = True


class PalaceLocked(MempalaceError):
    code = -32130
    retriable = True


class HnswQuarantined(MempalaceError):
    code = -32140
    retriable = True


def to_json_rpc_error(exc: MempalaceError) -> dict:
    """Shape exception into JSON-RPC error object."""
    data = dict(exc.data)
    if exc.retriable:
        data["retriable"] = True
    return {"code": exc.code, "message": str(exc), "data": data}
