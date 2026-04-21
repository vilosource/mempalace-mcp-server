"""Bearer-token → caller_id resolver and ASGI middleware.

Per TDD §5.3 and §9. The middleware validates bearer tokens against the
configured token map, rejects missing/unmapped tokens, and sets the
caller_id contextvar so downstream tool handlers can stamp it onto WAL +
storage without ever trusting client-supplied values.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
from typing import Awaitable, Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from server.config import AuthConfig
from server.errors import AuthMissing, AuthUnmapped, to_json_rpc_error

# Contextvar holding the resolved identity for the duration of a request.
_caller_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "caller_id", default=None
)

# Routes that bypass auth entirely.
UNAUTH_PATHS = {"/healthz", "/metrics"}


def get_caller_id() -> str:
    """Return the caller_id for the current request context.

    Raises AuthMissing if the middleware didn't set it — this is a
    programming error (middleware misconfiguration), not a client error.
    """
    cid = _caller_id.get()
    if cid is None:
        raise RuntimeError(
            "get_caller_id() called outside an authenticated request; "
            "middleware misconfigured"
        )
    return cid


class TokenResolver:
    """Maps bearer tokens (by SHA-256 hash) to caller identities."""

    def __init__(self, auth_cfg: AuthConfig):
        self.read_policy = auth_cfg.read_policy
        self._map: dict[str, str] = {
            entry.token_sha256.lower(): entry.identity
            for entry in auth_cfg.tokens
        }

    def resolve(self, bearer: str) -> str | None:
        """Return identity for `bearer`, or None if not mapped."""
        h = hashlib.sha256(bearer.encode("utf-8")).hexdigest()
        return self._map.get(h)


class AuthMiddleware:
    """ASGI middleware that validates bearer and sets caller_id contextvar."""

    def __init__(self, app: ASGIApp, resolver: TokenResolver):
        self.app = app
        self.resolver = resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in UNAUTH_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        auth_hdr = headers.get("authorization", "")
        if not auth_hdr.startswith("Bearer "):
            await _json_error(send, AuthMissing("missing Bearer token"))
            return
        bearer = auth_hdr[7:].strip()
        identity = self.resolver.resolve(bearer)
        if identity is None:
            await _json_error(send, AuthUnmapped("token not in identity map"))
            return

        token_reset = _caller_id.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _caller_id.reset(token_reset)


async def _json_error(send: Send, exc: "AuthMissing | AuthUnmapped") -> None:
    body = json.dumps({"jsonrpc": "2.0", "error": to_json_rpc_error(exc), "id": None})
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body.encode("utf-8")})
