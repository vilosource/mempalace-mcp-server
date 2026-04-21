# MemPalace Server PRD

**Date:** 2026-04-21
**Status:** Draft v3 — rewritten after reading MemPalace v3.3.0 source; key decisions resolved, open questions remain.
**Related:**
- [`doc/00-current-mempalace.md`](00-current-mempalace.md) — grounding document: how MemPalace actually works today (cited throughout this PRD).
- [`vilosource/mempalace`](https://github.com/vilosource/mempalace) — source repo.

---

## Context

MemPalace v3.3.0 ships as a stdio MCP server that every client session launches as a short-lived subprocess. This PRD proposes replacing that with a long-lived HTTP server per palace.

This document is grounded in a systematic read of the MemPalace source. Terminology, storage layout, the 31-tool MCP surface, the end-to-end write/read paths, and the embedding-model details are cataloged in [`00-current-mempalace.md`](00-current-mempalace.md). The PRD references that doc rather than restating it.

The design is written from the perspective of a consumer running multiple palaces across per-customer developer containers, but targets any consumer who wants correct concurrency and/or multi-device access.

---

## Problem

MemPalace's stdio + file-based storage has two concrete failure modes that block the use cases above.

### 1. Concurrent MCP processes corrupt the HNSW index

**Verified against source.** MemPalace uses `chromadb.PersistentClient`, not `HttpClient` (`mempalace/backends/chroma.py:469,490`). The Chroma HNSW index is memory-loaded per process with no inter-process coordination. SQLite KG has WAL mode enabled (`knowledge_graph.py:66`) but locking is a `threading.Lock` inside a single process (`:60`).

The failure mode is documented in-tree at `backends/chroma.py:52-67` as the Chroma Issue #823 workaround:

> When a ChromaDB 1.5.x PersistentClient opens a palace whose on-disk HNSW segment is significantly older than `chroma.sqlite3`, the Rust graph-walk can dereference dangling neighbor pointers for entries that exist in the metadata segment but not in the HNSW index, and segfault... On one fork palace (135K drawers), the drift caused a 65–85% crash rate on fresh-process opens.

Two concurrent MCP processes produce exactly this drift: process A adds to SQLite, process B's stale in-memory HNSW dereferences the new metadata, segfault. The existing `quarantine_stale_hnsw` workaround detects corrupted segments on open but does not prevent corruption.

**Supporting evidence:** `mempalace_reconnect` exists as an admin tool (`mcp_server.py:1119-1126`) specifically to invalidate `_collection_cache` and force a fresh ChromaDB client. The stdio code already knows its caches drift under external writes and ships a manual escape hatch.

### 2. File-based storage prevents multi-device use

A palace's data root is a single machine's filesystem. Sharing across laptop + workstation + CI requires manual rsync or a shared filesystem — neither viable for live interactive use.

### Pre-existing conditions (not new with the server)

These are landmines MemPalace already has; the server preserves them or fixes them, but doesn't introduce them:

- **Write path has no cross-store transaction.** `_wal_log()` and `col.upsert()` are independent (`mcp_server.py:625-659`). A crash between the WAL write and the Chroma upsert leaves a WAL entry with no corresponding drawer. No recovery code reconciles this.
- **Forensic WAL is partial.** `create_tunnel` / `delete_tunnel` in `palace_graph.py` do not call `_wal_log`. Tunnel mutations are not logged.
- **Embedding model is implicit and unconfigured.** MemPalace relies on ChromaDB's default embedding function (`all-MiniLM-L6-v2`, 384-dim). No model name in `config.json`, no version tracking, no dimension-mismatch guard. Upgrading ChromaDB can silently change the model and produce dimension-mismatch errors against existing palaces.

See [`00-current-mempalace.md`](00-current-mempalace.md) §9 for the full observation list.

## Proposal

Replace the per-client stdio server with a **long-lived, per-palace HTTP server** using the streamable-HTTP MCP transport. Clients become thin HTTP consumers. The server is the single writer and owns the data root.

Location-transparent: the same client works against a server running as a local docker container or a remote managed instance, decided by the `MEMPALACE_MCP_URL` clients receive.

### Scope: fork/absorb, not library-import

**Decision:** for v1 the server repo (`mempalace-mcp-server`) vendors MemPalace code rather than importing `mempalace` as a library.

**Why:** per [`00-current-mempalace.md`](00-current-mempalace.md) §9 observation 1, MemPalace is not currently library-safe:

- `mempalace/__init__.py:27` exports only `__version__`. No `Palace` facade class.
- `mcp_server.py:92-123` does heavy import-time initialization (config singleton, KG singleton, five module-level caches, stdio hijacking). Any process that imports `mempalace.mcp_server` inherits all of it, including `sys.stdout` override — which breaks any HTTP server host.
- Single-palace-per-process is baked in via the `_config` and `_kg` singletons.

**What this means in practice:**

- v1 server repo contains a copy/fork of the subset of MemPalace code it needs (write path, read path, palace loading, knowledge graph, Chroma backend). Contents are explicit in the repo, updated as upstream moves.
- Parallel upstream work: push refactors into `vilosource/mempalace` that extract a pure `Palace` facade class with no import-time side effects. When that lands, v2 of the server switches from fork to library import. The v1 fork is a means to not block on upstream.
- Upstream refactor is tracked in an open question below so the fork doesn't become permanent.

## Non-goals

- **Multi-tenant single server.** One process serving multiple palaces with per-request `palace_id` routing is out of scope. Each palace gets its own server instance.
- **Stdio transport support.** The old transport is dropped once migration completes. Not worth carrying two code paths.
- **Clustering / HA.** Single-instance per palace suffices for foreseeable usage.
- **MCP tool-contract changes unrelated to server topology.** The 31 tools documented in [`00-current-mempalace.md`](00-current-mempalace.md) §4 stay functionally equivalent under the server. Admin-tool semantics change (see Tool Surface below); the rest preserve arguments and return shapes. The embedding-placement question is explicitly deferred (see Open Questions).
- **New data model concepts in v1.** No new drawer kinds, no cross-palace tunnels, no federated search.

## Architecture

### Per-palace server container

One container per palace, product-first naming so `docker ps` sorts cleanly: `mempalace-<palace-id>` (examples: `mempalace-og`, `mempalace-vf`, `mempalace-dr`).

#### Data root, not "palace dir"

MemPalace's "palace" is not a single directory. Per [`00-current-mempalace.md`](00-current-mempalace.md) §3:

- **Global state** lives at `~/.mempalace/` — `knowledge_graph.sqlite3`, `config.json`, `people_map.json`, `entity_registry.json`, `identity.txt`, `state/`, `hook_state/`, `wal/write_log.jsonl`.
- **Chroma data** lives at `~/.mempalace/palace/` — `chroma.sqlite3` + HNSW segment directories.

The server therefore owns a **data root** — a full `~/.mempalace/`-shaped directory tree, where "palace" is everything inside it. v1 treats one data root as one palace served by one server instance. Two palaces = two separate data roots + two server containers.

This resolves a potential confusion: the default stdio behavior uses a single shared KG at `~/.mempalace/knowledge_graph.sqlite3` across multiple `MEMPALACE_PALACE_PATH` values. The server breaks that implicit sharing — each server instance gets its own KG scoped to its own data root. If downstream consumers rely on the cross-palace KG behavior, that's a migration concern they need to understand.

#### Volume and lifecycle

- Local docker: bind-mount the data root into the server at `/data`. Container reads/writes everything inside `/data/palace/` and `/data/` (for KG, config, etc.).
- Remote: data root lives on the server host.
- Restart policy: `--restart unless-stopped`. Survives host reboots.

### Transport

Streamable-HTTP MCP (the current spec-preferred transport). No stdio fallback. No bespoke protocol.

### Client discovery

Clients read `MEMPALACE_MCP_URL` from their environment. MCP registration writes it into the client's MCP config at startup.

- Local: `http://mempalace-og:8080/mcp`
- Remote: `https://mempalace.example.com/og/mcp`

### Lazy ensure-up (local mode only)

Proposed launcher helper `ensure_mempalace <palace-id>` runs before the client starts:

1. If `MEMPALACE_MCP_URL` is unset or resolves to a remote host, skip. Else treat as local ensure-up.
2. Ensure a shared MCP docker network exists (`docker network create <net> 2>/dev/null || true`).
3. `docker inspect -f '{{.State.Running}}' mempalace-<palace-id>` → if not running, `docker run -d --name mempalace-<palace-id> --network <net> --restart unless-stopped -v <data-root>:/data <image>`.
4. Poll `/healthz` for up to ~2s before handing off.

A second client in parallel finds the server running and skips steps 3–4.

### Tool surface and admin operations

The server exposes MemPalace's 31 MCP tools (see [`00-current-mempalace.md`](00-current-mempalace.md) §4 for the full catalog). Handler semantics are preserved for the 23 read tools and the 6 palace-write tools (`add_drawer`, `update_drawer`, `delete_drawer`, `diary_write`, `kg_add`, `kg_invalidate`). Two pre-existing gaps are resolved explicitly:

- **`create_tunnel` / `delete_tunnel` gain WAL entries.** The server routes all mutations through the attribution chokepoint (below), which writes WAL. This fixes the pre-existing forensic gap in `palace_graph.py`.
- **Admin tools are rescoped for a shared server:**
  - `mempalace_reconnect` becomes a no-op (returns success). The server owns the Chroma client; there is no per-caller cache to invalidate. Kept in the tool surface for client-side compatibility but semantically neutral.
  - `mempalace_hook_settings` and `mempalace_memories_filed_away` mutate global `~/.mempalace/config.json` / hook state. Under a shared server these are admin-level operations that affect all clients. Treated as server-scoped config, returning current values on read and applying globally on write. Emits a WAL entry with `caller_id`.

### Auth

**Local mode.** On first ensure-up, the launcher generates a random bearer token and writes it inside the data root as `.server-token` (mode 0600, owned by the container UID). The server reads this file on startup; the launcher reads it and forwards as `MEMPALACE_MCP_TOKEN` into the client. Subsequent ensure-ups reuse the existing file. Contract: file content = token, full stop; no JSON, no rotation metadata in v1.

**Remote mode.** Bearer token from an external secret store (vault, cloud secret manager, kubectl secret, etc.), forwarded into the client via env, never on disk. TLS required.

Bearer tokens are also the hook for the attribution model — each token maps to a caller identity the server stamps onto writes. See Attribution and provenance.

### Attribution and provenance

**Design commitment: v1 is provenance-ready, provenance-off.** Every write path carries authenticated caller identity end-to-end, even though v1 ships with a single identity (`"default"`). Later versions enable real per-client identities without a schema change or tool-contract break.

#### Relationship to MemPalace's existing provenance

MemPalace already carries **drawer-level provenance back-references** via the KG's `source_drawer_id` and `adapter_name` columns (RFC 002, `knowledge_graph.py:76,113`). Drawers themselves carry a caller-supplied `added_by` string (default `"mcp"`, `mcp_server.py:606`). The server's `caller_id` sits *alongside* `added_by`, answering a different question:

- **`source_drawer_id` / `adapter_name`** (on KG triples) — provenance pointer: which drawer/adapter produced this fact.
- **`added_by`** (on drawers, caller-supplied free string) — semantic label ("convo-miner", "manual-entry"). Not authenticated.
- **`caller_id`** (on drawers, server-set, new) — authenticated identity of the MCP client that made the call. Never spoofable.

These three co-exist. The server never modifies `added_by` on behalf of the client. `caller_id` is added to drawer metadata and to the WAL log entry for every mutation — including mutations that the KG triples also carry, at which point the triple's `source_drawer_id` links through to a drawer that carries `caller_id`.

#### Two fields, not one

The server keeps `added_by` and adds a separate field:

- **`caller_id`** — server-set from the authenticated bearer token via a token→identity map. Never client-supplied. Server of record for "who wrote this."
- **`added_by`** — unchanged: client-declared label for semantic tagging.

Conflating them is the v1 mistake that kills v2 — once `added_by` is both "free label" and "trust boundary," it can't be either.

#### Write-path invariant

Every server-side write (the 6 palace-write tools, the 2 tunnel tools, and any future mutator) routes through a single chokepoint that:

1. Resolves the caller identity from the authenticated token. Identity-resolution failure → write fails with a specific error (not a silent `"default"` fallback).
2. Stamps `caller_id` onto persisted drawer/tunnel metadata and (for KG mutations) onto the triple row.
3. Stamps `caller_id` onto the WAL log entry.

v1 resolution always returns `"default"`. v2+ returns the token's mapped identity. The chokepoint signature is stable across versions; only the resolver's configuration changes.

#### WAL schema extension

Current (`mcp_server.py:139-153`):

```json
{"timestamp": "...", "operation": "...", "params": {...}, "result": {...}}
```

Server-written entries add `caller_id`:

```json
{"timestamp": "...", "operation": "...", "caller_id": "default", "params": {...}, "result": {...}}
```

Missing on pre-server entries — readers treat `caller_id is NULL` as `"legacy-stdio"`.

#### Token-to-identity map

Server config (not code) defines the map:

```yaml
# v1 — single default identity
tokens:
  - token_sha256: "<hash>"
    identity: "default"

# v2+ — multiple identities
tokens:
  - token_sha256: "<hash-a>"
    identity: "host-a"
  - token_sha256: "<hash-b>"
    identity: "ci-pipeline"
```

Tokens stored hashed, matched on presentation. Adding a client = config entry + bounce.

#### Read surface

v1 does not add identity filters to `mempalace_search` / `mempalace_kg_query`. To avoid backfill later, the Chroma metadata schema writes `caller_id` on drawers from day 1, and the KG `triples` table gains a `caller_id TEXT` column (indexed, nullable).

#### Multi-writer semantics (inside the server)

MemPalace uses **deterministic, content-addressed drawer IDs** — `drawer_{wing}_{room}_{sha256(wing+room+content)[:24]}` (`miner.py:548`). Two clients writing identical content to the same wing/room produce identical IDs, so the upsert is idempotent by construction. Different content yields different IDs. The hard concurrency case (two clients writing the same "slot" with different content) does not exist.

Therefore the server's concurrency story is simple: serialize mutating tool calls behind a single in-process write lock. Reads proceed in parallel. No queue, no per-resource mutexes. In-server serialization is sufficient because the MCP transport is request-scoped and we're not optimizing for write throughput.

#### Explicit v1 non-goals for attribution

- Cryptographic signing of writes (non-repudiation).
- Per-identity rate limits or quotas.
- Identity-scoped read filters at the tool surface.
- Revocation lists (v1 revokes by removing from config + bounce).
- External identity providers (OIDC, SSO).

### Embedding model

**Design commitment: the server pins the embedding model explicitly.**

Per [`00-current-mempalace.md`](00-current-mempalace.md) §7, MemPalace today uses ChromaDB's default embedding function (`all-MiniLM-L6-v2`, 384-dim via sentence-transformers) with no model name in config and no version tracking. This is fragile — a ChromaDB upgrade can silently change the default model and produce dimension-mismatch or garbage distances against existing palaces.

The server:

- **Pins a specific embedding model and dimension** in server config (`config.json` extended, or dedicated server config section). Default: `all-MiniLM-L6-v2`, 384-dim, matching the current MemPalace behavior so existing palaces migrate cleanly.
- **Records embedding model metadata on the palace** on first startup — writes `{"embedding_model": "...", "embedding_dim": 384}` into `config.json` if absent.
- **Refuses to start** if the configured model disagrees with what the palace was created with. Error is explicit: "palace uses model X, server configured with model Y; refusing to write mixed vectors." No silent fallback, no auto-upgrade.
- **Embedding happens in-process on the server**, consistent with today's behavior. Whether to move embeddings to the client (shrinking the client image) or to an external embedding service is deferred to an open question.

### Network placement

Local server binds to an internal network port only (no `-p` publish) unless the user explicitly wants host-side clients. Clients on the MCP network resolve by container name.

### Failure modes

The stdio model has no "server down" state — every session gets a fresh process. Under the new model, the server is a dependency that can be unreachable, crashing, or behind on version.

- **Server unreachable on client start.** Client MCP registration succeeds but tool calls fail. Surface a clear error, not a hang. Target: first failing tool call returns within 5s.
- **Server crashes mid-session.** `--restart unless-stopped` brings it back; clients see transient errors, retry, succeed. Tool calls should be idempotent where possible; MemPalace's deterministic drawer IDs make `add_drawer` naturally idempotent.
- **Schema mismatch between client and server.** MCP capability negotiation covers protocol version; palace-schema version is separate (see open question).
- **HNSW index corruption detected on boot.** The existing `quarantine_stale_hnsw` workaround runs and quarantines the bad segment. Server comes up with reduced recall and logs loudly. Does **not** fail-closed.
- **Embedding model mismatch.** Server refuses to start against a palace created with a different model (see Embedding model above).
- **Disk full on server host.** Writes fail with a specific error code.
- **Caller identity resolution fails.** Bearer token present but not in the map, or map unreadable. Server rejects the write with a specific error (not a silent `"default"`) so the attribution invariant is never quietly violated. Reads may still succeed depending on read-auth policy (open question).
- **Partial write (pre-existing).** WAL log + Chroma upsert are not atomic in MemPalace today (`mcp_server.py:625-659`); the server preserves this behavior in v1. A crash between WAL write and Chroma upsert leaves the WAL with a record of a drawer that was never persisted. This is a regression only if the server makes it more likely; single-writer serialization actually reduces the window. An "atomic write" improvement is v2 work (see open question).

No graceful-degradation-to-stdio fallback. If the server is down, memory is unavailable for that session.

### Resource sizing (local mode)

Each palace loads its HNSW index into memory on server boot. Under stdio, RAM cost scaled with active sessions and was released on exit. Under the server, RAM cost scales with the *number of palaces ever started*. The tradeoff: warm caches + correctness versus higher idle memory. Threshold to be established empirically (open question).

An eviction story — stop idle servers after N minutes, re-ensure on next use — is deferred to an open question pending real measurement.

### Upgrade

Server image updates don't auto-pull into running containers. Proposed: ensure-up compares the running container's image digest against the tagged image and recreates on mismatch. Alternative: explicit `mempalace-<palace-id> upgrade` subcommand.

### Operational commands

Management UX (as a CLI shipped with this server):

- `mempalace-server status | logs | stop | restart | upgrade <palace-id>`
- `mempalace-server backup <palace-id> <path>` — cold snapshot of the data root.
- `mempalace-server shell <palace-id>` — open a shell inside the server container.

## Migration

The existing MemPalace WAL file is forensic, not replayable (`mcp_server.py:139-153`, content redacted per `_WAL_REDACT_KEYS`), so the cutover cannot use log-replay. Proposed sequence per palace:

1. **Quiesce.** Close all client sessions that could write to the palace.
2. **Snapshot.** Copy the full data root (`~/.mempalace/` or the equivalent `MEMPAL_*`-pointed tree) to a backup location. This is the rollback point.
3. **Server boot against live data root.** Start `mempalace-<palace-id>` bind-mounting the data root. Server:
   - Reads existing SQLite KG + Chroma files in place.
   - Adds `caller_id` column to KG `triples` (indexed, nullable; existing rows stay `NULL`, treated as `"legacy-stdio"`). Additive.
   - Writes `{embedding_model, embedding_dim}` into `config.json` if absent, matching current ChromaDB default.
   - `quarantine_stale_hnsw` runs if drift is detected — same behavior as today.
4. **Smoke test.** Scripted read + write + search via `curl` or test client.
5. **Switch clients.** Update client config with `MEMPALACE_MCP_URL` + `MEMPALACE_MCP_TOKEN`. Restart sessions.
6. **Watch.** Run for a week with stdio config commented out (not deleted). If anything regresses, stop the server, revert client config, restore the snapshot if writes diverged.
7. **Delete stdio config.** Rotate the snapshot into normal backup retention.

Rollback during steps 4–6: stop the server, point clients back at stdio, restore the snapshot if writes during trial must be discarded.

### Migration consideration: shared KG across palaces

Because `knowledge_graph.sqlite3` lives at the data-root top level (`~/.mempalace/`), a deployment that today shares one KG across multiple `MEMPALACE_PALACE_PATH` values will break that sharing when each palace gets its own server (each server owns its own data root). If this cross-palace KG sharing is in use, the migration must either:

- Split the shared KG into per-palace KGs (each server starts with its slice of the facts), or
- Consolidate palaces onto a single server instance until multi-palace-per-server becomes a supported mode (explicit non-goal for v1).

Open question below.

## Open questions

1. **Upstream library refactor.** v1 vendors MemPalace code. What's the path to pushing a clean `Palace` facade upstream so v2 can switch from fork to library import? Who drives, what's the scope of the refactor?
2. **Shared-KG deployments.** Do any current consumers share `~/.mempalace/knowledge_graph.sqlite3` across multiple palace paths? If yes, migration has to address this (split or consolidate).
3. **Local dir role after remote cutover.** If a palace moves to a remote server, does the client-side data root become (a) deleted, (b) read-only cache, (c) passive backup mirror?
4. **Backup ownership.** Server-hosted palaces need server-side backup. Who owns it — server operator, ops-by-convention, documented responsibility?
5. **Remote latency.** Over WAN, each `mempalace_search` / `mempalace_kg_query` pays RTT. Acceptable for interactive use; may need server-side batch endpoints for bursty agent workloads. Measure against realistic traffic before adding complexity.
6. **Client-side embeddings (image size).** Keeping embeddings on the server simplifies consistency; moving them client-side shrinks the client image but reintroduces model-version parity risk. Deferred pending prototype data.
7. **Resource sizing + eviction.** Measure idle memory for 10K and 100K drawer palaces. If many always-resident palaces push a host over limits, add an eviction story (stop idle servers after N minutes, re-ensure on next use).
8. **Observability.** Beyond `/healthz`, what metrics: query count, HNSW size, write-log depth, segfault counter? Pull vs push? Consumer?
9. **Palace-schema versioning.** MemPalace has no global palace version (only `normalize_version=2` as a drawer-rebuild gate, `palace.py:50`). Does the server introduce one? What does that imply for migrations going forward?
10. **Atomic write (v2 work).** WAL log + Chroma upsert are independent today. v2 could make them atomic (write log after Chroma commit succeeds, or use a sidecar transaction). v1 preserves existing semantics and documents the regression-that-isn't.
11. **Read-path auth policy.** Writes always require an authenticated, mapped identity. For reads — does v1 require the same, or does a valid-but-unmapped token get read-only access?
12. **Token rotation.** v1 revokes by removing from config + bouncing the server. Acceptable for single-token local, or does multi-identity remote need hot-reload (SIGHUP, config-watcher, admin endpoint)?

## Next steps

1. **Prototype the server against one palace.** Vendor the minimum MemPalace subset, wire up streamable-HTTP MCP transport, implement the 6 palace-write tools + key reads (`search`, `get_drawer`, `list_drawers`, `kg_query`, `status`). Measure boot time, idle RAM, HNSW load time, concurrent-client correctness, `mempalace_search` p50/p99 latency.
2. **Harden the write chokepoint.** Route all mutations (including tunnel ops) through the attribution chokepoint so `caller_id` is never skipped. Add WAL entries for tunnel ops to close the pre-existing gap.
3. **Decide client-side embeddings (question 6)** based on prototype resource numbers.
4. **Draft the ensure-up helper** against the prototype.
5. **Write the migration runbook** as a per-palace checklist; pilot with one real palace and one user.
6. **Kick off the upstream library refactor** so v2 can drop the fork.

## References

All citations above point at `github.com/vilosource/mempalace` at `v3.3.0`. For the comprehensive, cited catalog of MemPalace internals, see [`00-current-mempalace.md`](00-current-mempalace.md). Representative anchors used directly in this PRD:

- [`mempalace/mcp_server.py:52-67`](https://github.com/vilosource/mempalace/blob/main/mempalace/backends/chroma.py) — Issue #823 corruption mode
- [`mempalace/mcp_server.py:139-153`](https://github.com/vilosource/mempalace/blob/main/mempalace/mcp_server.py) — WAL entry schema
- [`mempalace/mcp_server.py:606,1410`](https://github.com/vilosource/mempalace/blob/main/mempalace/mcp_server.py) — `added_by` parameter and default
- [`mempalace/mcp_server.py:1119-1126`](https://github.com/vilosource/mempalace/blob/main/mempalace/mcp_server.py) — `tool_reconnect` cache-invalidation escape hatch
- [`mempalace/mcp_server.py:625-659`](https://github.com/vilosource/mempalace/blob/main/mempalace/mcp_server.py) — non-atomic WAL + Chroma write
- [`mempalace/backends/chroma.py:469,490`](https://github.com/vilosource/mempalace/blob/main/mempalace/backends/chroma.py) — `PersistentClient` instantiation
- [`mempalace/knowledge_graph.py:60,66,76,113`](https://github.com/vilosource/mempalace/blob/main/mempalace/knowledge_graph.py) — threading.Lock, WAL pragma, RFC 002 schema
- [`mempalace/miner.py:548`](https://github.com/vilosource/mempalace/blob/main/mempalace/miner.py) — deterministic drawer ID
- [`mempalace/palace.py:50`](https://github.com/vilosource/mempalace/blob/main/mempalace/palace.py) — `normalize_version = 2`
- [`mempalace/__init__.py:27`](https://github.com/vilosource/mempalace/blob/main/mempalace/__init__.py) — minimal public API
- [`pyproject.toml:40-43`](https://github.com/vilosource/mempalace/blob/main/pyproject.toml) — entry points
