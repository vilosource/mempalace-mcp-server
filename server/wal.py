"""WAL writer. Append-only JSONL with caller_id and request_id.

Per TDD §4.3. Preserves MemPalace's redaction behavior
(mcp_server.py:133-160) but sources redact keys from config so the set is
operator-tunable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WalWriter:
    def __init__(self, wal_path: Path, redact_keys: list[str]):
        self.path = wal_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restricted perms if missing (matches mempalace semantics).
        if not self.path.exists():
            fd = os.open(str(self.path), os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        self._redact = set(redact_keys)

    def log(
        self,
        operation: str,
        params: dict[str, Any],
        caller_id: str,
        request_id: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        safe_params: dict[str, Any] = {}
        for k, v in params.items():
            if k in self._redact:
                if isinstance(v, str):
                    safe_params[k] = f"[REDACTED {len(v)} chars]"
                else:
                    safe_params[k] = "[REDACTED]"
            else:
                safe_params[k] = v
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "caller_id": caller_id,
            "request_id": request_id,
            "params": safe_params,
            "result": result,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
