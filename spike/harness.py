"""Measurement harness for the spike.

Runs against a server started elsewhere (uvicorn server:app --port 8765).
Measures:
  - Server boot time (read from /healthz response).
  - Idle RAM (read from /proc/<pid>/status when PID is supplied).
  - mempalace_search p50/p99 over N queries.
  - mempalace_add_drawer p50/p99 over M writes.
  - Two-client concurrent stress: mixed read/write, report errors + final count.

Usage:
    .venv/bin/python harness.py --url http://localhost:8765 --pid <server-pid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import string
import sys
import time
import uuid
from typing import Any

import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Realistic-looking queries against a Wikipedia-seeded palace.
SEARCH_QUERIES = [
    "history of ancient Rome",
    "photosynthesis in plants",
    "quantum mechanics",
    "industrial revolution",
    "human migration patterns",
    "classical music composers",
    "theory of relativity",
    "medieval architecture",
    "volcanoes and earthquakes",
    "famous battles of world war two",
    "origin of the universe",
    "cellular biology",
    "mathematical proofs",
    "Renaissance art",
    "climate change",
    "evolution of species",
    "computer algorithms",
    "Pacific Ocean trade routes",
    "nineteenth century literature",
    "ancient philosophy",
]


def percentile(values: list[float], p: int) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    k = (len(values) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


def read_rss_kb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        return None
    return None


async def run_searches(session: ClientSession, n: int) -> list[float]:
    latencies = []
    for i in range(n):
        q = SEARCH_QUERIES[i % len(SEARCH_QUERIES)]
        t0 = time.perf_counter()
        await session.call_tool("mempalace_search", {"query": q, "limit": 5})
        latencies.append((time.perf_counter() - t0) * 1000)
    return latencies


async def run_writes(session: ClientSession, n: int, prefix: str) -> list[float]:
    latencies = []
    for i in range(n):
        content = "spike test drawer " + prefix + "-" + str(i) + " " + \
                  "".join(random.choices(string.ascii_lowercase + " ", k=700))
        t0 = time.perf_counter()
        await session.call_tool("mempalace_add_drawer", {
            "wing": "spike",
            "room": prefix,
            "content": content,
        })
        latencies.append((time.perf_counter() - t0) * 1000)
    return latencies


async def client_session_for(url: str):
    """Return a context-managed MCP session."""
    return streamablehttp_client(url)


async def measure_search(url: str, n: int = 200):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            # Warm one call (first embedding can be slow).
            await s.call_tool("mempalace_search", {"query": SEARCH_QUERIES[0], "limit": 5})
            return await run_searches(s, n)


async def measure_writes(url: str, n: int = 100):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await run_writes(s, n, "writes-" + uuid.uuid4().hex[:6])


async def stress_two_clients(url: str, seconds: int = 60) -> dict:
    """Two concurrent clients, each doing alternating search/add for N seconds."""
    errors = []
    counts = {"a_search": 0, "a_write": 0, "b_search": 0, "b_write": 0}

    async def worker(label: str):
        async with streamablehttp_client(url) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                deadline = time.time() + seconds
                i = 0
                while time.time() < deadline:
                    try:
                        if i % 2 == 0:
                            await s.call_tool("mempalace_search", {
                                "query": SEARCH_QUERIES[i % len(SEARCH_QUERIES)], "limit": 5,
                            })
                            counts[f"{label}_search"] += 1
                        else:
                            content = f"stress drawer {label}-{i} " + \
                                      "".join(random.choices(string.ascii_lowercase + " ", k=600))
                            await s.call_tool("mempalace_add_drawer", {
                                "wing": "stress",
                                "room": label,
                                "content": content,
                            })
                            counts[f"{label}_write"] += 1
                    except Exception as e:
                        errors.append(f"{label}: {type(e).__name__}: {e}")
                    i += 1

    await asyncio.gather(worker("a"), worker("b"))
    return {"counts": counts, "errors_sample": errors[:10], "error_count": len(errors)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8765/mcp")
    ap.add_argument("--health-url", default="http://localhost:8765/healthz")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--searches", type=int, default=200)
    ap.add_argument("--writes", type=int, default=100)
    ap.add_argument("--stress-seconds", type=int, default=60)
    args = ap.parse_args()

    report: dict[str, Any] = {}

    # Health + boot.
    h = requests.get(args.health_url, timeout=5).json()
    report["health"] = h

    # RAM.
    if args.pid:
        rss_kb = read_rss_kb(args.pid)
        if rss_kb is not None:
            report["idle_rss_mb"] = round(rss_kb / 1024, 1)

    # Search latency.
    print("measuring search...")
    s_ms = await measure_search(args.url, args.searches)
    report["search_ms"] = {
        "n": len(s_ms),
        "p50": round(percentile(s_ms, 50), 2),
        "p95": round(percentile(s_ms, 95), 2),
        "p99": round(percentile(s_ms, 99), 2),
        "min": round(min(s_ms), 2),
        "max": round(max(s_ms), 2),
        "mean": round(statistics.mean(s_ms), 2),
    }

    # Write latency.
    print("measuring writes...")
    w_ms = await measure_writes(args.url, args.writes)
    report["add_drawer_ms"] = {
        "n": len(w_ms),
        "p50": round(percentile(w_ms, 50), 2),
        "p95": round(percentile(w_ms, 95), 2),
        "p99": round(percentile(w_ms, 99), 2),
        "min": round(min(w_ms), 2),
        "max": round(max(w_ms), 2),
        "mean": round(statistics.mean(w_ms), 2),
    }

    # Concurrent stress.
    print(f"running {args.stress_seconds}s two-client stress...")
    stress = await stress_two_clients(args.url, args.stress_seconds)
    report["stress"] = stress

    # Post-stress RAM.
    if args.pid:
        rss_kb = read_rss_kb(args.pid)
        if rss_kb is not None:
            report["post_stress_rss_mb"] = round(rss_kb / 1024, 1)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
