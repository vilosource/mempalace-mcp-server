# Spike Report

**Date:** 2026-04-21
**Status:** Complete — all exit gates met or documented.
**Related:**
- [`doc/01-implementation-plan.md`](../doc/01-implementation-plan.md) — Phase 0 definition and success criteria.
- [`doc/mempalace-server-PRD.md`](../doc/mempalace-server-PRD.md) — design commitments this spike tested.

---

## Summary

The spike ran a minimal streamable-HTTP MCP server (5 tools) against a freshly-seeded 10K-drawer Wikipedia palace. All latency and correctness criteria passed with significant headroom. Boot time marginally exceeded the 5 s target (+0.076 s) due to Python import cost — not load cost — and is fine in steady state. Two-client concurrent stress ran for the full 10 min with zero errors.

Recommendation: **promote the spike scaffold to `server/` for v1**.

## Setup

- **Palace:** 10 000 drawers from 2 126 random Wikipedia articles, chunked at 800-char word boundaries. Created fresh, not migrated from an existing stdio palace.
- **Size on disk:** 83 MB (palace data root incl. Chroma + KG skeleton).
- **Embedding:** ChromaDB default (`all-MiniLM-L6-v2`, 384-dim), pinned explicitly in `config.json` on the server.
- **Stack:** Python 3.12, `mcp` 1.27.0 (`FastMCP` + `streamable_http_app`), ChromaDB 1.5.8, FastAPI/Starlette via uvicorn 0.45.0, `requests` 2.33.1.
- **Server:** 5 tools — `status`, `add_drawer`, `get_drawer`, `search`, `kg_query`. No auth. `caller_id` stamping present but always `"default"`. Single `asyncio.Lock` on writes.
- **Host:** WSL2 Linux, Python venv, no containerization.

Everything in `spike/`:

- `seed.py` — Wikipedia ingest, writes to `/tmp/mempalace-spike/` (outside repo).
- `server.py` — the 5-tool spike server.
- `harness.py` — measurement script (p50/p95/p99 for search and add_drawer; two-client concurrent stress).

## Results vs. Phase 0 exit criteria

| Metric | Target | Result | Verdict |
|---|---|---|---|
| Server boot time (process start → `/healthz` 200) | < 5 s | **5.076 s** (wall), 0.067 s (palace load only) | ⚠️ **marginal miss** |
| Idle RAM (1 palace loaded, no traffic) | < 400 MB | **134 MB** pre-query, **220 MB** after HNSW load | ✅ pass (well under) |
| `mempalace_search` p50 | < 500 ms | **165 ms** | ✅ pass (3× better) |
| `mempalace_search` p99 | < 2 s | **223 ms** | ✅ pass (~9× better) |
| `mempalace_add_drawer` p50 | < 300 ms | **187 ms** | ✅ pass |
| Two-client mixed read/write, 10 min | No corruption, no segfault | **3 079 ops, 0 errors** over 600 s, HNSW clean post-run | ✅ pass |
| Migrated palace opens without embedding mismatch | Yes | **Not tested** — spike palace was fresh, not migrated | ⊘ deferred to v1 |

## Detailed measurements

### Search latency (n=200, post-warm)

```
min=146.1  mean=169.5  p50=165.3  p95=199.3  p99=223.0  max=229.6 (all ms)
```

### Write latency (n=100, fresh drawers)

```
min=171.5  mean=191.8  p50=187.1  p95=230.8  p99=247.5  max=265.0 (all ms)
```

### Two-client stress (10 min)

| Metric | Value |
|---|---|
| Total ops | 3 079 |
| Client A searches | 770 |
| Client A writes | 770 |
| Client B searches | 770 |
| Client B writes | 769 |
| Errors | 0 |
| Drawers added during stress | 1 539 |
| RSS mid-stress | 221 MB |
| RSS end of stress | 215 MB |
| Drawer count start | 10 261 (incl. short-stress adds) |
| Drawer count end | 11 800 |

No `quarantine_stale_hnsw` event. No Chroma segfault. No drift. Single-writer serialization (one `asyncio.Lock`) was sufficient, as the PRD predicted based on deterministic content-addressed drawer IDs.

### RAM profile

- **Pre-query idle:** 134 MB. Chroma client is constructed but HNSW is not yet loaded — the collection's vector index stays cold until the first query.
- **Post-warm, no traffic:** ~194 MB. First search loaded HNSW into memory.
- **Post-10-min stress:** 215 MB. ~80 MB of growth across 1539 new drawers + lifetime HNSW presence. Still well under the 400 MB target.
- **`VmPeak`:** 6.7 GB — virtual address space reserved, not RSS. Expected for a Python process with multiple arena-style allocators; not a memory concern.

### Boot time breakdown

Wall-clock boot was 5.076 s from `uvicorn` invocation to `/healthz` 200. The server reports `boot_time_s: 0.067` — that's the time from Python module-level `_load_palace()` start to finish (ChromaDB `PersistentClient` init + empty collection fetch). The other ~5 s is before `_load_palace()` runs: Python interpreter startup, uvicorn + FastAPI + MCP SDK imports, ChromaDB import (pulls in `onnxruntime` and `sentence-transformers`).

**Implication.** The 5-s budget was import-bound, not palace-bound. A larger palace won't push this up; HNSW loads lazily on first query. The boot number is a constant overhead of this stack. In steady-state server operation the number is irrelevant.

If the 5 s becomes a real constraint (frequent restarts, multi-palace laptop), options include: stripping heavy imports behind lazy paths, pre-warming with PyInstaller/pex, or switching to a lighter web framework. Not worth doing in v1 unless numbers change.

## What the spike proved

- **Per-palace HTTP server is viable.** 10 K drawers, two clients, 10 min mixed workload, zero corruption. The central PRD claim — that single-writer serialization plus content-addressed drawer IDs removes the HNSW corruption class — holds in practice.
- **Latency is comfortably under budget.** Even with full hybrid search absent from the spike (we used plain vector query, not MemPalace's closet-boost + BM25 rerank), 165 ms p50 leaves plenty of headroom. Adding the rerank stage will cost more but the spike shows the network + transport overhead is trivial.
- **RAM scales predictably.** HNSW cost is ~60 MB for 10 K drawers at 384-dim — extrapolates to roughly 600 MB at 100 K, comfortably multi-palace on a laptop.
- **FastMCP + `streamable_http_app` works** as a transport layer. Health endpoint coexists with the MCP endpoint via Starlette route injection. No custom transport needed.
- **The embedding model pin approach works.** `config.json` declares `embedding_model` and `embedding_dim`; server reads them on boot and can reject mismatches (the full rejection logic lands in v1 — spike only warns).

## What the spike did not prove (gaps feeding Phase 1 TDD)

1. **Migration path.** The spike palace was created fresh; migrating an existing stdio palace (with pre-spike drawers lacking `caller_id`, existing embedded vectors from the exact same model) is untested. Phase 1 must specify the migration DDL and test against a real migrated palace.
2. **Full hybrid search parity.** Spike `search` is Chroma vector query only. The real MemPalace `searcher.py` adds closet boost + BM25 rerank + sanitization. Latency under full search needs to be re-measured in v1 scaffold, not in the spike.
3. **Admin tools under shared-server semantics.** `tool_reconnect` as no-op, `hook_settings` / `memories_filed_away` as server-scoped — PRD decisions, not exercised in spike.
4. **Auth + `caller_id` chokepoint.** Code path stubs `caller_id = "default"`. Identity-resolution failure, token-map hot-reload, and the TLS/remote-auth path are all for v1.
5. **Tunnel ops.** `create_tunnel` / `delete_tunnel` not in the spike's 5 tools. PRD commits to WAL-logging them; v1 must.
6. **KG concurrency under load.** Spike's `kg_query` was not exercised under concurrent stress (no KG content). Need writes + queries mixed in v1 stress.
7. **Observability.** Spike exposes `/healthz` only. `/metrics` shape, logging format (structured JSON vs plain), correlation IDs are TDD work.
8. **Boot time under docker.** Spike ran bare-metal venv. Boot inside a container (image pull, startup, potentially an embedding-model download on first start) is untested.

## Pre-Phase-1 recommendations

- **Promote the spike into `server/`** rather than delete + rewrite. The structure is clean, the FastMCP integration works, the seed script is reusable for test palettes.
- **Add the real `searcher.py` port** early in Phase 1 so the "hybrid search latency" number lands in the TDD.
- **Do a separate migration-focused mini-spike** against a snapshot of the production `~/.mempalace/` palace (even though only 9 drawers) to prove the additive column + `caller_id=NULL` legacy handling works end-to-end.
- **Explicitly accept the boot-time finding.** The 5 s target was import-heavy, not load-heavy — document the actual budget in the TDD (something like "≤ 6 s wall including Python imports; ≤ 0.5 s palace-load work on top of that").
- **Keep `/tmp/mempalace-spike/`** around as a reusable fixture for v1 CI or manual regression — re-seeding takes 35 min over the Wikipedia API.

## Artifacts

- `seed.py` — 162 LOC, deps: `chromadb`, `requests`.
- `server.py` — 230 LOC, deps: `mcp`, `chromadb`, Starlette-via-`mcp`.
- `harness.py` — 210 LOC, deps: `mcp[client]`, `requests`.
- Palace at `/tmp/mempalace-spike/` — 83 MB, 11 800 drawers post-stress. Outside the repo by design.
- Server log `/tmp/spike-server.log` — clean, no warnings.

## Appendix: raw harness output

```json
{
  "health": {
    "status": "ok",
    "drawer_count": 10000,
    "boot_time_s": 0.067
  },
  "idle_rss_mb": 134.4,
  "search_ms": {
    "n": 200, "p50": 165.25, "p95": 199.27, "p99": 222.95,
    "min": 146.13, "max": 229.64, "mean": 169.51
  },
  "add_drawer_ms": {
    "n": 100, "p50": 187.13, "p95": 230.76, "p99": 247.51,
    "min": 171.51, "max": 264.96, "mean": 191.78
  },
  "stress_60s": {
    "counts": {"a_search": 81, "a_write": 81, "b_search": 81, "b_write": 80},
    "errors": 0
  },
  "stress_600s": {
    "counts": {"a_search": 770, "a_write": 770, "b_search": 770, "b_write": 769},
    "errors": 0
  },
  "post_stress_rss_mb": 215.4
}
```
