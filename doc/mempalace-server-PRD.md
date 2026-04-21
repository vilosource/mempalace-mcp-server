# MemPalace Server PRD

**Date:** 2026-04-21
**Status:** Draft — motivation verified against MemPalace source, open questions remain
**Related:** [MemPalace](https://github.com/vilosource/mempalace) (source repo for the existing stdio MCP server)

---

## Context

MemPalace ships today as a stdio MCP server that every client session launches as a short-lived subprocess. This PRD proposes replacing that with a long-lived HTTP server per palace. The motivation is grounded in failure modes observed in real use (see Problem section) and in design hints already present in the MemPalace repo (RFC 002 uses the vocabulary of a "long-lived durable daemon").

The PRD is written from the perspective of one consumer — a developer running multiple palaces across per-customer developer containers — but the design is intended to serve any consumer who wants correct concurrency and/or multi-device access to a MemPalace palace.

---

## Problem

MemPalace today is a stdio-only MCP server launched fresh by every client session. Each session's server process writes directly to a palace directory on disk:

- `knowledge_graph.sqlite3` — SQLite (WAL mode)
- `palace/chroma.sqlite3` + `palace/<uuid>/*.bin` — Chroma `PersistentClient` HNSW index
- `wal/write_log.jsonl` — append-only forensic audit log (not replayable)

Two concrete failures follow from this model.

### 1. Concurrent MCP processes corrupt the HNSW index

**Verified against MemPalace source.** MemPalace uses `chromadb.PersistentClient`, not `HttpClient` (`mempalace/backends/chroma.py:469,490`). The Chroma HNSW index is memory-loaded per process with no inter-process coordination. SQLite has WAL mode enabled (`mempalace/knowledge_graph.py:66`) but locking is a `threading.Lock` inside a single process (`:60`) — two MCP server processes open independent connections and share nothing.

The failure mode is documented in the MemPalace code itself, at `mempalace/backends/chroma.py:52-67`, as the workaround for Chroma Issue #823:

> When a ChromaDB 1.5.x PersistentClient opens a palace whose on-disk HNSW segment is significantly older than `chroma.sqlite3`, the Rust graph-walk can dereference dangling neighbor pointers for entries that exist in the metadata segment but not in the HNSW index, and segfault... On one fork palace (135K drawers), the drift caused a 65–85% crash rate on fresh-process opens.

That drift is exactly what two concurrent MCP processes produce: process A adds an entry to SQLite, process B's stale in-memory HNSW dereferences the new metadata, segfault. The existing `quarantine_stale_hnsw` workaround detects and sidelines corrupted segments on open but does not prevent the corruption.

### 2. File-based storage prevents multi-device use

A palace lives on one machine. Using the same palace from laptop + workstation + CI requires manual rsync or a shared filesystem — neither viable for live interactive use. This is a design limitation of stdio + local-file storage, not a bug.

### Secondary observations (not motivating on their own)

- The WAL log (`wal/write_log.jsonl`) is a forensic audit trail, not a replayable source of truth. It cannot be used as a cutover tool.
- MemPalace RFC 002 already uses the vocabulary of a "long-lived durable daemon" for adapters, suggesting the author has considered server-shaped deployment.

## Proposal

Replace the per-client stdio server with a **long-lived, per-palace server** that speaks the streamable-HTTP MCP transport. Clients become thin HTTP consumers. The server is the single writer and owns the underlying files.

Location-transparent: the same client works against a server running as a local docker container or a remote managed instance. The launcher (or whatever boots the client) decides local vs remote based on the `MEMPALACE_MCP_URL` it was given.

## Non-goals

- **Multi-tenant single server.** One server process serving multiple palaces with per-request `palace_id` routing is out of scope. Each palace gets its own server instance.
- **Stdio transport support.** The old transport is dropped once migration completes. Not worth carrying two code paths.
- **Clustering / HA.** Single-instance per palace is sufficient for the foreseeable usage pattern. Add replication later only if a specific palace outgrows it.
- **MCP tool contract changes unrelated to server topology.** Tool names, arguments, and semantics stay as they are. An exception is explicitly called out below for embedding placement, which may change the client-server payload shape — that decision is open.

## Architecture

### Per-palace server container

One container per palace, product-first naming so `docker ps` sorts cleanly: `mempalace-<palace-id>` (examples: `mempalace-og`, `mempalace-vf`, `mempalace-dr`, `mempalace-pi`).

Each owns its own data volume:

- Local docker: bind-mount the palace directory into the server container at a known path.
- Remote: data lives on the server host; no local dir (or a read-only mirror — see open question).

Restart policy: `--restart unless-stopped`. Survives host reboots without client involvement.

### Transport

Streamable-HTTP MCP (the current spec-preferred transport). No stdio fallback. No bespoke protocol.

### Client discovery

Clients read `MEMPALACE_MCP_URL` from their environment. MCP registration writes it into the client's MCP config at startup.

Examples:

- Local: `http://mempalace-og:8080/mcp`
- Remote: `https://mempalace.example.com/og/mcp`

### Lazy ensure-up (local mode only)

A proposed launcher helper `ensure_mempalace <palace-id>` runs before the client starts. The steps:

1. If `MEMPALACE_MCP_URL` is unset or resolves to a remote host, skip. Else treat as local ensure-up.
2. Ensure a shared MCP docker network exists (`docker network create <net> 2>/dev/null || true`).
3. `docker inspect -f '{{.State.Running}}' mempalace-<palace-id>` → if not running, `docker run -d --name mempalace-<palace-id> --network <net> --restart unless-stopped -v <palace-dir>:/data <image>`.
4. Poll `/healthz` for up to ~2s before handing off to the client. Prevents the first MCP call from racing server startup.

A second client starting in parallel finds the server running at step 3 and skips 3–4.

### Auth

**Local mode.** On first ensure-up, the launcher generates a random bearer token and writes it inside the palace directory as `.server-token` (mode 0600). The server reads this file on startup; the launcher reads it and forwards as `MEMPALACE_MCP_TOKEN` into the client. Subsequent ensure-ups reuse the existing file. Contract: file content = token, full stop; no JSON, no rotation metadata in v1.

**Remote mode.** Bearer token from an external secret store (vault, cloud secret manager, kubectl secret, etc.), forwarded into the client via env, never on disk. TLS required.

### Network placement

Local server binds to an internal network port only (no `-p` publish) unless the user explicitly wants host-side clients. Clients on the MCP network resolve by container name. Keeps the attack surface small and avoids port conflicts.

### Failure modes

The stdio model has no "server down" state — every session gets a fresh process. Under the new model, the server is a dependency that can be unreachable, crashing, or behind on version. The PRD commits to the following behaviors:

- **Server unreachable on client start.** Client MCP registration succeeds (URL is valid) but tool calls fail. Surface a clear error in the harness, not a hang. Target: first failing tool call returns within 5s.
- **Server crashes mid-session.** With `--restart unless-stopped` docker restarts it; clients see transient errors, retry, succeed after restart. Tool calls should be idempotent where possible on the server side (the existing write log helps here).
- **Schema mismatch between client and server.** Server returns a versioned error; client logs and continues with degraded capability rather than crashing. MCP capability negotiation covers protocol version; palace-schema version is a separate concern (see open question).
- **HNSW index corruption detected on server boot.** The existing `quarantine_stale_hnsw` workaround runs and quarantines the bad segment. Server comes up with reduced recall and logs loudly. Does **not** fail-closed.
- **Disk full on server host.** Writes fail; the server returns a specific error code so the client can surface it distinctly from a generic internal error.

No graceful-degradation-to-stdio fallback. Dropping the old transport is a non-goal; if the server is down, memory is unavailable for that session.

### Resource sizing (local mode)

Each palace's HNSW loads into memory on server boot. On a host running multiple palaces simultaneously, the always-resident footprint matters. No measurement yet. Open question below.

Under stdio, RAM cost scaled with *active* sessions and was released on session end. Under the server model, RAM cost scales with the *number of palaces ever started*. The tradeoff is warm caches + correctness against higher idle memory. Acceptable up to a threshold we need to establish empirically.

### Upgrade

Server image updates don't auto-pull into running containers. Proposed: ensure-up compares the running container's image digest against the tagged image and recreates on mismatch. Alternative: explicit `mempalace-<palace-id> upgrade` subcommand that pulls + recreates. Pick one; both coexisting is likely confusing.

### Operational commands

Management UX needs to exist. Proposed subcommands (as a CLI shipped with this server):

- `mempalace-server status | logs | stop | restart | upgrade <palace-id>`
- `mempalace-server backup <palace-id> <path>` — cold snapshot of the palace data dir
- `mempalace-server shell <palace-id>` — open a shell inside the server container

Without these, debugging a wedged server means remembering raw docker commands.

## Migration

The existing MemPalace WAL file is a forensic log, not a replayable source of truth, so the cutover cannot use log-replay. Proposed sequence per palace:

1. **Quiesce.** Close all client sessions that could write to the palace.
2. **Snapshot.** Copy the palace directory to a backup location. This is the rollback point.
3. **Launch server.** Start `mempalace-<palace-id>` against the live palace directory (bind-mounted). Server reads existing SQLite + Chroma files in place; schema is unchanged. If `quarantine_stale_hnsw` runs during boot, that's expected — the palace is already subject to the same drift today.
4. **Smoke test.** Run a scripted read + write + search against the server. Confirm it returns expected results.
5. **Switch clients.** Update client config to set `MEMPALACE_MCP_URL` + `MEMPALACE_MCP_TOKEN` for the palace. Restart client sessions.
6. **Watch.** Run for a week with the old stdio config commented out (not deleted). If anything regresses, stop the server, uncomment stdio config, restart clients, restore the snapshot if writes diverged.
7. **Delete.** Remove the stdio config and rotate the snapshot into normal backup retention.

Rollback during steps 4–6: stop the server, point clients back at stdio, restore the snapshot if writes during the trial must be discarded.

No in-flight write handling beyond step 1's quiesce. The old stdio server and the new HTTP server are not expected to be writing concurrently; if they are, the snapshot is the recovery.

## Open questions

1. **Relationship to the MemPalace repo.** Should this server eventually be absorbed into `vilosource/mempalace` as an additional entrypoint, or remain a separate repo that depends on MemPalace as a library?
2. **Local dir role after remote cutover.** If a palace moves to a remote server, does the client-side palace dir become (a) deleted, (b) read-only cache, (c) passive backup mirror? Decision affects disaster recovery.
3. **Backup ownership.** Today backups are whatever each user's laptop backup covers. Server-hosted palaces need server-side backup. Who owns that — the server operator, or is it an ops-by-convention responsibility we document?
4. **Remote latency.** Over WAN, each `mempalace_search` and `mempalace_kg_query` pays RTT. Acceptable for interactive use; may need server-side batch/multi-query endpoints if agents do bursty lookups. Measure against a realistic workload before adding complexity.
5. **Embedding model placement.** If the server handles embeddings, developer images can drop the embedding model — smaller images, faster container start, one codepath for embedding behavior. This **does** change the client-server payload (clients send raw text instead of pre-computed vectors, or both). Decide explicitly.
6. **Resource sizing.** Measure idle memory for a realistic palace (10K and 100K drawers) to determine the local-mode practical palace count. If many always-resident palaces push a host over limits, the ensure-up model needs an eviction story (stop idle servers after N minutes, re-ensure on next use).
7. **Observability.** Beyond `/healthz`, what metrics do we want — query count, HNSW size, write-log depth, segfault counter? Pull vs push? Which consumer uses them?
8. **In-server concurrency.** Single writer from the MCP perspective, but the server process itself can serialize writes with (a) an in-process async lock, (b) a write queue, (c) per-resource mutexes. Which is warranted, and does it matter for v1?
9. **Versioning.** MCP capability negotiation covers protocol version. Palace-schema compatibility is separate — how does a client tell if its expected schema matches what the server holds? Is this something v1 needs, or is "breaking schema changes require coordinated rollout" acceptable?
10. **Embedding model parity across clients.** Under stdio today, every client computes embeddings with whatever model it has. If clients currently disagree, the server transition is a forcing function to unify — or an explicit source of incompatibility we migrate palace-by-palace.

## Next steps

1. Read `mempalace/backends/chroma.py` `quarantine_stale_hnsw` and adjacent write path in detail to understand what the server inherits vs fixes.
2. Prototype the server against one real palace. Measure: boot time, idle RAM, HNSW load time, concurrent-client correctness, end-to-end latency on `mempalace_search`.
3. Decide embedding placement (question 5) based on prototype learnings.
4. Draft the ensure-up helper against the prototype.
5. Write the migration runbook as a checklist per palace; first-class migration of one palace with one user is the smallest possible pilot.

## References

- [`mempalace/mcp_server.py:1690`](https://github.com/vilosource/mempalace/blob/main/mempalace/mcp_server.py) — stdio MCP entrypoint
- [`mempalace/backends/chroma.py:52-67`](https://github.com/vilosource/mempalace/blob/main/mempalace/backends/chroma.py) — Issue #823 corruption mode
- [`mempalace/backends/chroma.py:469,490`](https://github.com/vilosource/mempalace/blob/main/mempalace/backends/chroma.py) — `PersistentClient` instantiation
- [`mempalace/knowledge_graph.py:60,66`](https://github.com/vilosource/mempalace/blob/main/mempalace/knowledge_graph.py) — threading.Lock + WAL pragma
- [`mempalace/mcp_server.py:117`](https://github.com/vilosource/mempalace/blob/main/mempalace/mcp_server.py) — WAL audit log path
- MemPalace RFC 002 — "long-lived durable daemon" adapter vocabulary
