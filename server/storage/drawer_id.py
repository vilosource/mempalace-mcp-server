"""Deterministic content-addressed drawer IDs.

Mirrors MemPalace v3.3.0 miner.py:548 so new writes produce IDs byte-equal
to the stdio server for the same (wing, room, content) triple. This is
load-bearing for idempotency and for the multi-writer correctness
argument in the PRD.
"""

from __future__ import annotations

import hashlib


def drawer_id(wing: str, room: str, content: str) -> str:
    h = hashlib.sha256(f"{wing}{room}{content}".encode("utf-8")).hexdigest()[:24]
    return f"drawer_{wing}_{room}_{h}"
