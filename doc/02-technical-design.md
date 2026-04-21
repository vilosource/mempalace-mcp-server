# Technical Design Document

**Date:** 2026-04-21
**Status:** Draft — written after Phase 0 spike; numbers in this doc are measured, not estimated.
**Related:**
- [`mempalace-server-PRD.md`](mempalace-server-PRD.md) — requirements and design commitments.
- [`00-current-mempalace.md`](00-current-mempalace.md) — verified MemPalace v3.3.0 internals.
- [`01-implementation-plan.md`](01-implementation-plan.md) — phase sequencing and exit criteria.
- [`../spike/spike-REPORT.md`](../spike/spike-REPORT.md) — measured numbers this TDD inherits.

This doc is the implementable spec. A fresh implementer (human or agent) should be able to produce v1 from it without needing clarification on layout, API shape, or schemas. Where v1 consciously defers a decision, the deferral and its owner are called out.

---

## 1. Stack and dependencies

Python 3.12 venv. All runtime deps pinned to the versions validated in Phase 0.

| Package | Version | Role |
|---|---|---|
| `mcp` | `1.27.0` | Streamable-HTTP transport (`FastMCP` + `streamable_http_app`) |
| `chromadb` | `1.5.8` | Palace vector store; `PersistentClient` on bind-mounted data root |
| `sentence-transformers` | (ChromaDB transitive) | Embedding model loader |
| `uvicorn` | `0.45.0` | ASGI server for the Starlette app produced by MCP SDK |
| `starlette` | (MCP transitive) | App framework; used directly for `/healthz` and `/metrics` routes |
| `pydantic` | `>=2.0` | Config schema + request validation |
| `pyyaml` | `>=6.0` | Server config file parsing |
| `structlog` | `>=24.0` | Structured JSON logging |
| `typer` | `>=0.12` | `mempalace-server` CLI |

**Embedding model.** Pinned to `all-MiniLM-L6-v2` (384-dim). Matches MemPalace's current implicit default. Model weights ship baked into the docker image (see §12 Deployment), not downloaded at first boot.

**Base image.** `python:3.12-slim`. No GPU deps; embeddings are CPU-only and fast enough per spike measurements.

**No direct dependency on `mempalace` package.** Per PRD Scope decision, MemPalace modules are vendored under `server/storage/` (see §2). The `pyproject.toml` does not list `mempalace` as a dep.

## 2. Module layout

```
mempalace-mcp-server/
├── server/                          # the runtime package
│   ├── __init__.py                  # exports app factory, __version__
│   ├── main.py                      # FastMCP app, route wiring, app factory
│   ├── config.py                    # server YAML config loader (pydantic)
│   ├── auth.py                      # bearer-token → caller_id resolver
│   ├── dispatch.py                  # identity-resolution chokepoint; wraps handlers
│   ├── errors.py                    # JSON-RPC error codes and exception classes
│   ├── wal.py                       # WAL log writer; lifts redaction from mempalace
│   ├── logging.py                   # structlog setup; request-id middleware
│   ├── metrics.py                   # minimal /metrics exposition (deferred counters)
│   ├── health.py                    # /healthz endpoint impl
│   ├── tools/                       # 31 tool handlers, grouped by domain
│   │   ├── __init__.py              # TOOLS dict (matches mempalace pattern)
│   │   ├── drawers.py               # add/update/delete/get/list/search/check_duplicate
│   │   ├── kg.py                    # kg_add / kg_invalidate / kg_query / kg_timeline / kg_stats
│   │   ├── tunnels.py               # create / delete / list / follow / traverse / find / graph_stats
│   │   ├── diary.py                 # diary_write / diary_read
│   │   └── admin.py                 # status / list_wings / list_rooms / taxonomy / aaak / hook_settings / memories_filed_away / reconnect
│   ├── storage/                     # vendored MemPalace modules (minimally modified)
│   │   ├── __init__.py
│   │   ├── palace.py                # collection accessors (drawers + closets)
│   │   ├── knowledge_graph.py       # KG class; schema + migrations
│   │   ├── palace_graph.py          # tunnels
│   │   ├── searcher.py              # hybrid search + BM25 rerank
│   │   ├── query_sanitizer.py       # query sanitization
│   │   ├── config.py                # palace-level config and sanitizers (renamed import)
│   │   ├── miner.py                 # drawer-ID generator; chunk helpers
│   │   └── backends/
│   │       ├── __init__.py
│   │       └── chroma.py            # ChromaBackend wrapper (stripped of reconnect logic)
│   └── migrate.py                   # migration subcommand impl
├── cli/                             # mempalace-server CLI
│   └── __main__.py                  # typer app: status / logs / stop / restart / upgrade / backup / shell / migrate
├── tests/
│   ├── unit/                        # pure-function tests (sanitizers, drawer_id, WAL format)
│   ├── integration/                 # end-to-end against a seeded palace
│   ├── stress/                      # two-client concurrent workload
│   └── regression/                  # stdio-reference comparison (same input → same drawer_id + KG triple)
├── Dockerfile
├── pyproject.toml
└── doc/                             # PRD, TDD, grounding (already present)
```

**Boundary between new and vendored code.** Anything under `server/storage/` is a copy from `vilosource/mempalace` at the pinned v3.3.0 commit, with these stripping operations:

- `storage/palace.py`: drop `mine_lock` filesystem locking (server is the single writer).
- `storage/backends/chroma.py`: drop `_get_client()` inode/mtime reconnect detection; construct the client once in `server/main.py` startup and pass it in.
- `storage/config.py`: drop `MempalaceConfig` env-reading singleton pattern; become a plain data object the server passes its values into.
- `storage/knowledge_graph.py`: remove the auto-init on import-time; keep the class, construct it explicitly.

**No stdio hijacking.** The existing `mcp_server.py` stdio-framing code is not vendored; `server/tools/` reimplements tool registration against the `FastMCP` pattern proven in the spike.

## 3. Config schema

Single YAML file, path supplied via `--config` flag or `MEMPALACE_SERVER_CONFIG` env var. Default: `/etc/mempalace-server/config.yaml` in the container, `~/.config/mempalace-server/config.yaml` on host.

```yaml
# Server config. Distinct from the palace's own config.json.
data_root: /data                       # bind-mount path in container; the palace data root

bind:
  host: 0.0.0.0
  port: 8080
  # /metrics exposed on the same port; set to null to disable
  metrics_path: /metrics

embedding:
  model: all-MiniLM-L6-v2
  dim: 384
  # on mismatch against palace config.json, refuse to start
  enforce_match: true

auth:
  # List of accepted bearer tokens; v1 ships single-identity "default"
  tokens:
    - token_sha256: "<64-hex-chars>"
      identity: "default"
  # Read-path auth policy: writes always require mapped identity.
  # For reads: 'required' (same as writes) or 'mapped_or_none' (valid token okay).
  read_policy: required                # PRD OQ 11; v1 default = required

logging:
  level: INFO                          # DEBUG | INFO | WARNING | ERROR
  format: json                         # json | console

wal:
  redact_keys:                         # lifted from mempalace; extend here, don't re-hardcode
    - content
    - content_preview
    - document
    - entry
    - entry_preview
    - query
    - text
```

Precedence: env vars (prefix `MEMPALACE_SERVER_`) override YAML, which overrides code defaults. Token map is config-only (never env) to avoid accidental log leakage.

## 4. Data-model additions

### 4.1 Chroma drawer metadata

Existing metadata fields (per [`00-current-mempalace.md §3`](00-current-mempalace.md)) are preserved unchanged. One new key added on every drawer `upsert`:

| Key | Type | Nullable | Semantics |
|---|---|---|---|
| `caller_id` | `str` | No (server-set) | Server-authenticated identity from token map; `"default"` in v1 |

ChromaDB metadata value types allow `str | int | float | bool`. `caller_id: str` is native. No migration DDL for Chroma — the field simply starts appearing on new upserts. Drawers pre-dating the server are unmodified and have no `caller_id` key; readers treat that as `"legacy-stdio"`.

### 4.2 SQLite KG `triples` table

Two additive changes, both applied by `server/migrate.py` (see §13):

```sql
-- 1. Add caller_id column
ALTER TABLE triples ADD COLUMN caller_id TEXT;

-- 2. Index it (for future identity-scoped queries)
CREATE INDEX IF NOT EXISTS idx_triples_caller_id ON triples(caller_id);
```

SQLite `ALTER TABLE ADD COLUMN` is O(1) — no row rewrite. Existing rows have `caller_id = NULL`. Queries filtering on identity treat `NULL` as `"legacy-stdio"` at the application layer.

No schema version bump (MemPalace doesn't have one — see PRD OQ 9). Migration idempotency relies on the `IF NOT EXISTS` guard on the index and on introspecting `PRAGMA table_info(triples)` before running `ALTER TABLE`.

### 4.3 WAL entries

Current schema ([`mcp_server.py:139-153`](https://github.com/vilosource/mempalace/blob/main/mempalace/mcp_server.py)):

```json
{"timestamp": "2026-04-21T13:45:12+00:00", "operation": "add_drawer", "params": {...}, "result": {...}}
```

Server schema adds two fields:

```json
{
  "timestamp": "2026-04-21T13:45:12+00:00",
  "operation": "add_drawer",
  "caller_id": "default",
  "request_id": "01HQZ...",
  "params": {...},
  "result": {...}
}
```

`request_id` is a ULID generated at dispatch time, also stamped into logs for correlation (see §8).

No WAL-file versioning (single file, readers handle missing keys). Redaction keys come from config (§3) so the set is operator-tunable without code change.

## 5. API specification

### 5.1 HTTP endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/mcp` | Bearer | MCP streamable-HTTP transport (provided by `FastMCP.streamable_http_app()`) |
| `GET` | `/healthz` | None | Liveness + readiness probe |
| `GET` | `/metrics` | None | Prometheus text exposition (v1: minimal counters; v2: full set) |

### 5.2 Health check contract

`GET /healthz` returns `200` with:

```json
{
  "status": "ok",
  "drawer_count": 10000,
  "boot_time_s": 0.067,
  "embedding_model": "all-MiniLM-L6-v2",
  "embedding_dim": 384,
  "version": "0.1.0"
}
```

Returns `503` with `{"status": "degraded", "reason": "..."}` if the Chroma client failed to initialize or the palace's `config.json` was unreadable. Never returns `200` while still booting — the route handler is only registered after `_load_palace()` succeeds.

### 5.3 Error-code allocation

JSON-RPC error `code` values. `message` is a human-readable string; `data` may carry structured detail.

| Code | Meaning | Retriable |
|---|---|---|
| `-32601` | Unknown tool | No |
| `-32602` | Invalid param (type, missing required, etc.) | No |
| `-32000` | Generic internal tool error | Caller decides |
| `-32100` | Auth: missing or malformed bearer token | No |
| `-32101` | Auth: token not in identity map | No |
| `-32110` | Embedding model mismatch (server vs palace) | No |
| `-32120` | Storage full (disk or Chroma quota) | Retry later |
| `-32130` | Palace locked (migration in progress) | Retry after delay |
| `-32140` | HNSW segment quarantined during this request's span | Retry |

Codes `-32100` through `-32199` are the server's identity-reserved range; future adds stay in that block. Clients should treat unrecognized codes as fatal unless the SDK surfaces a retriable hint in `data.retriable`.

### 5.4 MCP tool surface

All 31 tools from MemPalace v3.3.0 (see [`00-current-mempalace.md §4`](00-current-mempalace.md)). Argument shapes and return shapes preserved verbatim. Server-specific changes:

- `caller_id` written to drawer metadata and KG rows (never accepted as a caller-supplied arg; stripped at the whitelisting step).
- `mempalace_reconnect` — semantic no-op; returns `{"success": true, "noop": true}`.
- `mempalace_hook_settings` / `mempalace_memories_filed_away` — treated as server-scoped admin; emit WAL entries with `caller_id`.
- `mempalace_create_tunnel` / `mempalace_delete_tunnel` — routed through the write chokepoint; WAL entries emitted (closing the pre-existing gap in `palace_graph.py`).

## 6. Write-path sequence

```
client                  /mcp (FastMCP)          dispatch            handler                  storage
  │                         │                     │                    │                        │
  │──POST JSON-RPC──────────▶│                     │                    │                        │
  │                         │──extract Bearer────▶│                    │                        │
  │                         │                     │──auth.resolve─────▶│ auth.py                 │
  │                         │                     │◀─caller_id─────────│                        │
  │                         │                     │──whitelist args ───┤ (inspect.signature;    │
  │                         │                     │   drop caller_id,  │  drops client-supplied │
  │                         │                     │   coerce types)    │  caller_id silently)   │
  │                         │                     │                    │                        │
  │                         │                     │──acquire write lock (asyncio.Lock)───────▶  │
  │                         │                     │                    │                        │
  │                         │                     │──wal.log──────────▶│                        │
  │                         │                     │   {caller_id,      │                        │
  │                         │                     │    request_id,...} │                        │
  │                         │                     │                    │                        │
  │                         │                     │──call handler─────▶│──compute drawer_id    │
  │                         │                     │                    │   (sha256 deterministic)│
  │                         │                     │                    │──Chroma.get(id) ──────▶│
  │                         │                     │                    │◀─exists? ──────────────│
  │                         │                     │                    │   if yes: return       │
  │                         │                     │                    │     already_exists     │
  │                         │                     │                    │──Chroma.upsert───────▶│
  │                         │                     │                    │   (metadata gains      │
  │                         │                     │                    │    caller_id)          │
  │                         │                     │                    │──KG.add_triple───────▶│
  │                         │                     │                    │   (row gains caller_id)│
  │                         │                     │◀─result────────────│                        │
  │                         │                     │──release lock                               │
  │                         │◀─JSON-RPC result────│                                             │
  │◀─HTTP 200───────────────│                                                                   │
  │                         │                                                                   │
  │   (on any handler       │                                                                   │
  │    exception: rollback  │                                                                   │
  │    lock, log error,     │                                                                   │
  │    return -32000)       │                                                                   │
```

**Atomicity.** v1 preserves MemPalace's pre-existing non-atomicity: WAL log and Chroma upsert are independent. A crash between them leaves a WAL entry with no drawer. Documented in PRD OQ 10; atomic-write work is v2.

**Concurrency.** Single `asyncio.Lock` serializes mutating calls across all clients. Reads proceed in parallel. Justified in PRD by deterministic content-addressed drawer IDs — the "two writers same slot" conflict cannot arise.

## 7. Read-path sequence (`mempalace_search`)

```
client           /mcp (FastMCP)      dispatch           handler (tools/drawers.py)    storage
  │                  │                 │                       │                         │
  │──POST ──────────▶│                 │                       │                         │
  │                  │──auth resolve──▶│                       │                         │
  │                  │                 │──whitelist/coerce────▶│                         │
  │                  │                 │──call handler───────▶ │                         │
  │                  │                 │                       │──sanitize_query ───────▶│
  │                  │                 │                       │   (query_sanitizer.py)  │
  │                  │                 │                       │                         │
  │                  │                 │                       │──Chroma.query ─────────▶│
  │                  │                 │                       │   (drawers, n*3, where)│
  │                  │                 │                       │◀─raw hits──────────────│
  │                  │                 │                       │                         │
  │                  │                 │                       │──Chroma.query ─────────▶│
  │                  │                 │                       │   (closets, for boost) │
  │                  │                 │                       │◀─closet hits───────────│
  │                  │                 │                       │                         │
  │                  │                 │                       │──apply boost + filter   │
  │                  │                 │                       │   (searcher.py)         │
  │                  │                 │                       │──BM25 rerank            │
  │                  │                 │                       │   (corpus-relative IDF) │
  │                  │                 │                       │──enrich metadata        │
  │                  │                 │◀──result──────────────│                         │
  │                  │◀─JSON-RPC result│                                                 │
  │◀─HTTP 200────────│                                                                   │
```

No write lock acquired. Read path never mutates palace state. Read-path auth policy per config (§3 `auth.read_policy`): v1 default is `required`, rejecting unmapped tokens the same way writes do.

## 8. Logging format

Structured JSON lines on stdout (container-native), one record per log event.

```json
{
  "ts": "2026-04-21T13:45:12.039Z",
  "level": "info",
  "logger": "server.dispatch",
  "event": "tool.call",
  "request_id": "01HQZ7X9C3FB2V4WTQ5RAYK5GE",
  "caller_id": "default",
  "tool": "mempalace_add_drawer",
  "duration_ms": 187,
  "status": "ok"
}
```

Required fields on every record: `ts` (ISO-8601 UTC with Z), `level`, `logger`, `event`.
Contextual fields (added by middleware when applicable): `request_id`, `caller_id`, `tool`, `duration_ms`, `status`, `error` (on failure).

Levels: `debug` (verbose tracing — lock acquire/release, Chroma query timing), `info` (tool call boundaries, lifecycle events), `warning` (retries, quarantine, config deviations), `error` (tool exceptions, auth failures, storage errors).

Docker logging driver handles rotation; server does not rotate its own stdout.

## 9. Threat model

### 9.1 Assets

1. **Palace data** (Chroma drawers + KG triples) — includes potentially sensitive user content (memories, personal notes, project info).
2. **Token map** — bearer tokens grant palace-level RW access; a leaked token is a full compromise for that identity.
3. **WAL log** — redacted for content but retains metadata and shape of operations; useful to an attacker for timing analysis.

### 9.2 Actors and threats

| Actor | Capability | Primary threats | Mitigations |
|---|---|---|---|
| Legitimate client | Holds a valid token | Over-permissioned reads/writes, accidental misuse | Per-identity scoping (v2), audit via WAL + logs (v1) |
| Leaked-token attacker | Has a valid token via theft | Full palace RW as the identity | Token rotation, `/etc/mempalace-server/` file-mode discipline, TLS on remote (v1 requires reverse proxy) |
| Host-FS attacker | Has read access to `data_root` on the host | Bypasses auth entirely — reads Chroma/KG files directly | Out of scope for v1; document "palace dir confidentiality = host-FS confidentiality" |
| Container-escape attacker | Root inside container | Can read `.server-token` file, write to the palace directly | Container hardening (drop caps, read-only root where possible); the server container does not run as root |
| Supply-chain attacker | Compromised package in deps | Arbitrary code execution in the server process | Pinned versions in `pyproject.toml`; SBOM at build time; no `:latest` in prod |
| Malicious palace content | Poisoned drawers influencing a downstream reader | Prompt-injection, misinformation | Out of scope for the server — this is a consumer concern; the server attributes writes to a `caller_id` which makes origin traceable |

### 9.3 Residual risk

- `-32100`/`-32101` errors do not rate-limit in v1; brute-forcing the token map is possible over a long enough window. Mitigation: tokens are 256-bit high-entropy strings; log loudly on repeated auth failures.
- v1 has no TLS termination; requires a reverse proxy (caddy, nginx, traefik) for any non-loopback exposure. Documented in deployment section.
- Disk-full behavior degrades writes but not reads; operators must monitor data-root capacity externally.

## 10. Test strategy

Four tiers, each with its own invocation target and coverage goal.

### 10.1 Unit

Pure functions, no I/O:

- `server/storage/config.py` sanitizers (`sanitize_name`, `sanitize_content`, `sanitize_query`).
- `server/storage/miner.py` drawer-ID generation — deterministic, idempotent on identical inputs.
- `server/wal.py` redaction logic — configured keys replaced, others pass through.
- `server/auth.py` token-SHA256 lookup against a stubbed map.
- `server/errors.py` exception → JSON-RPC response mapping.

Tooling: pytest. Run under every commit.

### 10.2 Integration

Against a seeded palace fixture (the spike's `/tmp/mempalace-spike/` is reusable; copy into a test-local path for isolation):

- Round-trip every mutator: `add_drawer`, `update_drawer`, `delete_drawer`, `diary_write`, `kg_add`, `kg_invalidate`, `create_tunnel`, `delete_tunnel`. Verify each produces a WAL entry with `caller_id`.
- Every reader: correctness of search results, kg_query temporal filtering, tunnel traversal.
- Admin tools: `reconnect` returns noop, `hook_settings` rejects client-supplied `caller_id`.
- Migration: run `mempalace-server migrate` on a copy of a pre-server palace; verify DDL applied, existing rows readable, new writes carry `caller_id`.

Tooling: pytest with a session-scoped fixture that starts the server in-process.

### 10.3 Concurrent-writer stress

The spike's `harness.stress_two_clients` is the v1 basis. Extensions:

- Ratio modes: 100% writes, 80/20 read/write, 20/80 read/write.
- Client counts: 2 (baseline), 4, 8.
- Duration: 10 min baseline; 2 hour soak for acceptance.
- Assert no `quarantine_stale_hnsw` events post-run, zero errors, drawer count matches expected.

### 10.4 Regression against stdio

Prove server handlers produce identical data-plane outputs to MemPalace's stdio server for identical inputs:

- Seed two palaces identically using both servers (fresh `add_drawer` calls). Compare resulting drawer IDs and metadata. Must match byte-for-byte except for the new `caller_id` key.
- Same input `mempalace_search` → same result order and scores.
- Same `mempalace_kg_add` → same KG row (except `caller_id` + `extracted_at`).

Tooling: a regression harness that keeps a minimal MemPalace v3.3.0 stdio installation available as a reference; shell out to it for the comparison.

## 11. Observability

### 11.1 v1 scope

- Structured JSON logs (§8).
- `/healthz` (§5.2).
- `/metrics` exposing a minimal Prometheus set:
  - `mempalace_server_tool_calls_total{tool, status}` counter
  - `mempalace_server_tool_duration_seconds{tool}` histogram
  - `mempalace_server_drawer_count` gauge
  - `mempalace_server_write_lock_wait_seconds` histogram
  - `mempalace_server_auth_failures_total{reason}` counter

### 11.2 Deferred to v2

HNSW-size gauge, write-log-depth gauge, per-identity request attribution in metrics, OpenTelemetry traces. All tracked in PRD OQ 7.

## 12. Deployment

### 12.1 Image

- Base: `python:3.12-slim`.
- Build stages: (1) deps install via `pip install --no-cache-dir`; (2) vendored `server/` + `cli/` copy; (3) model weights pre-downloaded with `from sentence_transformers import SentenceTransformer; SentenceTransformer("all-MiniLM-L6-v2")` so first boot doesn't hit the network.
- Final size target: < 2 GB (ChromaDB + onnxruntime dominates).
- Registry: `ghcr.io/vilosource/mempalace-mcp-server`.
- Tags: `:v1.0.0` (semver), `:v1` (moving major), `:latest` (moving), `:sha-abc1234` (immutable).

### 12.2 Runtime

- User: non-root UID `1001`.
- Workdir: `/app`.
- Bind-mount: host palace data root at `/data`.
- Config mount: `/etc/mempalace-server/config.yaml` read-only.
- Ports: `8080` internal; operators publish via reverse proxy for TLS.
- Restart policy: `unless-stopped`.

### 12.3 Launcher integration

`ensure_mempalace <palace-id>` shell helper (sketched in PRD §Architecture). Full implementation ships in a separate tooling repo or as an example in `examples/` here — decide in M3 of the implementation plan based on where it's actually called from.

## 13. Migration tooling

`mempalace-server migrate <data-root>` is the scripted version of the PRD migration runbook. It is the cutover operation, not a reversible config toggle.

### 13.1 Preflight

- `data-root` exists and is readable.
- `data-root/palace/chroma.sqlite3` exists (a non-empty palace).
- `data-root/config.json` either absent (legacy) or matches the server's pinned embedding model.
- No server is running against `data-root` (advisory lock file `data-root/.server-running` absent).
- Snapshot exists (operator asserted via `--snapshot-taken` flag — the tool does not snapshot on the user's behalf).

Exit code non-zero with human-readable explanation on any preflight failure.

### 13.2 Steps

1. Acquire advisory lock: write `data-root/.migration-in-progress` with PID.
2. Open SQLite KG; `PRAGMA table_info(triples)`. If `caller_id` column absent, run `ALTER TABLE triples ADD COLUMN caller_id TEXT` and `CREATE INDEX IF NOT EXISTS idx_triples_caller_id ON triples(caller_id)`.
3. Open palace `config.json` (create if absent). Ensure `embedding_model` and `embedding_dim` match the server's pinned values. Reject if the palace was created with a different model (requires a separate, manual re-embed operation).
4. Write a marker file `data-root/.server-migrated-at` with the migration timestamp and server version.
5. Release advisory lock.

Idempotent: re-running against a migrated palace is a no-op.

### 13.3 Rollback

Removing `data-root/.server-migrated-at` does not roll back the schema change (the `caller_id` column is benign to stdio MemPalace — it ignores unknown columns). Rollback = stop the server, revert clients to stdio, optionally restore the operator's pre-migration snapshot if writes during the trial period are unwanted.

The `caller_id` column itself is safe to leave in place under stdio; no data rollback needed unless cross-period writes must be discarded.

## 14. v1 exit criteria (echo of implementation-plan)

- One real palace migrated and in active use without regression across a representative set of sessions covering all 31 tools.
- Rollback procedure executed successfully on a test palace.
- All spike-measured performance targets met or exceeded on the same palace post-migration — specifically: `search` p50 ≤ 165 ms, `add_drawer` p50 ≤ 190 ms, idle RSS ≤ 220 MB with 10 K drawers.
- All 31 tools exercised at least once against the running server.
- Two-client concurrent workload runs for ≥ 2 hours without corruption.

## 15. Deferrals and open questions

Carried from the PRD (see [`mempalace-server-PRD.md §Open questions`](mempalace-server-PRD.md)); nothing in this TDD collapses them:

- **OQ 1** Upstream library refactor — parallel work, doesn't block v1.
- **OQ 2** Shared-KG deployments — audit needed before M4.
- **OQ 5** Client-side embeddings — spike provides numbers but decision is v2.
- **OQ 7** Resource sizing + eviction — v1 ships without eviction; revisit at M4 based on real-use idle RAM.
- **OQ 10** Atomic write — explicitly v2.
- **OQ 11** Read-path auth policy — TDD default is `required`, config-tunable; no code path split.
- **OQ 12** Token rotation — v1 stop+restart; hot-reload v2.

## 16. Changelog

- **2026-04-21:** initial TDD after Phase 0 spike.
