# Implementation Plan

**Date:** 2026-04-21
**Status:** Draft — plan precedes any code.
**Related:**
- [`mempalace-server-PRD.md`](mempalace-server-PRD.md) — product requirements and design commitments
- [`00-current-mempalace.md`](00-current-mempalace.md) — verified understanding of MemPalace v3.3.0

---

## Overview

Three sequential phases: **spike → TDD → v1**. Each is a deliverable with explicit exit criteria. The spike de-risks unknowns (resource sizing, latency, embedding-model behavior across a migrated palace) before the TDD commits to numbers. The TDD then documents what the spike actually proved and fills in gaps (module layout, stack specifics, test strategy, threat model). v1 implements against the TDD.

This ordering exists because several PRD commitments are measurement-dependent — RAM budget, latency targets, whether embeddings should move client-side — and writing a TDD with placeholder numbers wastes effort.

---

## Phase 0 — Prototype spike

**Goal.** Validate the central design claims with a minimal running server, then measure. Not production code; thrown-away or promoted to v1 scaffolding depending on what we learn.

### Scope (in)

- **Transport:** streamable-HTTP MCP via FastAPI + Anthropic's `mcp` Python SDK (or plain ASGI if the SDK doesn't cleanly support server-side streamable-HTTP — confirm in the first hour).
- **Tools (5 only):** `mempalace_add_drawer`, `mempalace_get_drawer`, `mempalace_search`, `mempalace_kg_query`, `mempalace_status`. Enough to prove the write path, read path, KG path, and a meta endpoint.
- **Vendoring:** copy the minimum MemPalace code needed — `palace.py`, `backends/chroma.py`, `knowledge_graph.py`, `searcher.py`, `config.py`, `query_sanitizer.py`, `miner.py` for drawer ID logic. Strip `mcp_server.py`'s import-time init, stdio hijacking, and reconnect detection. Keep the `TOOLS` dict format.
- **One real palace.** Copy an existing palace's data root (10K+ drawers minimum). Do not write to the canonical copy — prototype against a snapshot.
- **Embedding pin:** configure ChromaDB embedding function explicitly (`all-MiniLM-L6-v2`, 384-dim). Confirm the migrated palace opens without dimension mismatch.
- **Single-client + two-client tests.** Scripted `curl` or Python harness hitting the 5 tools.

### Scope (out — explicit non-goals for the spike)

- Auth of any kind. `MEMPALACE_MCP_TOKEN` is checked against a static string if anything at all.
- `caller_id` / attribution chokepoint. Write the stamping code but don't exercise identity resolution.
- Docker packaging. Run from a venv. Docker can come in the TDD → v1 handoff.
- Launcher `ensure_mempalace` helper. Start the server by hand.
- Migration tooling. Use a one-off copy of an existing palace.
- Any of the 26 other tools.
- Error taxonomy. A generic `-32000` for everything that isn't `-32601` is fine.
- Tests beyond the measurement harness.
- Documentation other than a `spike-REPORT.md` describing what was measured and what broke.

### Deliverables

- `spike/` subdirectory in this repo with the minimum code to run the server.
- `spike/harness.py` — the measurement script that exercises the 5 tools.
- `spike-REPORT.md` — the numbers, surprises, and recommendations.
- List of decisions the spike proved or disproved (keep the PRD's open-questions list in mind).

### Success criteria

The spike succeeds if all of the following hold on a palace with 10K+ drawers:

| Metric | Target | Notes |
|---|---|---|
| Server boot time (process start → `/healthz` 200) | < 5s | Includes Chroma client init + HNSW load |
| Idle RAM (1 palace loaded, no traffic) | < 400 MB | Informs local-mode multi-palace viability |
| `mempalace_search` p50 | < 500 ms | Query through full hybrid rerank path |
| `mempalace_search` p99 | < 2 s | Same workload |
| `mempalace_add_drawer` p50 | < 300 ms | End-to-end including embedding |
| Two concurrent clients, 10 minutes mixed read/write | No corruption, no segfault | Success = `quarantine_stale_hnsw` never fires post-boot |
| Migrated palace opens without embedding-model mismatch | Yes | Validates the embedding-pin design |

If any target is missed, that's a finding — it either reshapes the design or gets accepted as a known constraint with explicit justification in the PRD.

### Time estimate

2–4 days of focused work. If it sprawls past a week, something is wrong — narrow the scope further.

---

## Phase 1 — Technical Design Document

**Goal.** A concrete engineering spec that someone other than the original author could implement from. Written after the spike, informed by measured numbers.

### Inputs

- The PRD (design commitments).
- `00-current-mempalace.md` (ground truth).
- `spike-REPORT.md` (measured numbers and real surprises).
- A rough module layout from the spike codebase.

### Required sections (not optional)

1. **Stack and dependencies.** Python version, FastAPI (or alternative from spike learnings), `mcp` SDK version, ChromaDB version, sentence-transformers version, exact embedding model + SHA. Dockerfile base image.
2. **Module layout.** Directory tree with one-line purpose per file. Boundary between vendored MemPalace code and new server code explicit.
3. **Config schema.** Full YAML/JSON schema for server config: data root path, embedding model + dim, token→identity map, network binding, log level. Defaults and env overrides.
4. **Data-model additions.**
   - Exact DDL for `caller_id` column on KG `triples` (indexed, nullable).
   - Exact Chroma metadata extension for `caller_id` on drawers.
   - WAL entry versioning (schema v1 vs v2 format; readers handle both).
5. **API spec.**
   - HTTP endpoints: `/healthz`, `/mcp`, `/metrics` (if any).
   - Health check response contract.
   - Error-code allocation table (e.g. `-32100` identity resolution, `-32110` embedding model mismatch, `-32120` disk full, ...).
6. **Write-path sequence diagram.** Request → auth → identity resolution → dispatch → handler → WAL → Chroma/KG → response. Labels on every arrow.
7. **Read-path sequence diagram.** Same shape, for `mempalace_search`.
8. **Logging format.** Structured JSON, required fields (`ts`, `level`, `request_id`, `caller_id`, `tool`, `duration_ms`, ...), rotation strategy.
9. **Threat model.** One page: assets (palace data), actors (legitimate client, leaked-token attacker, host-FS attacker, container-escape attacker), threats, mitigations, residual risk.
10. **Test strategy.** Unit (pure functions, sanitizers), integration (against a real palace copy), concurrent-writer stress (two clients × N mutations), regression against stdio (identical inputs → identical drawer IDs and KG triples). Scope each tier.
11. **Observability.** Metrics surface (pull `/metrics` vs push), what's exported.
12. **Deployment.** Image distribution (GHCR likely), tag conventions, signing/attestation decision, upgrade path.
13. **Migration tooling.** The runbook from the PRD's Migration section, turned into a scripted `mempalace-server migrate` subcommand.

### Exit criteria

- A second engineer could open the TDD and produce a reasonable implementation without needing clarification on data layout, API shape, or module boundaries.
- All "concrete gaps" from the review (stack choice, module layout, schemas, Dockerfile, error codes, health check, logging, tests, threat model) are resolved or consciously deferred with a reason.
- The spike's numbers appear in the doc as real targets, not placeholders.

### Time estimate

2–3 days.

---

## Phase 2 — v1 implementation

**Goal.** Production-ready server against one palace for one user, with migration tooling. Not multi-identity, not multi-palace-per-server, not remote-hosted — those are v2+.

### Scope (in)

- All 31 MCP tools from MemPalace, with admin-tool semantics as defined in the PRD (`reconnect` no-op, `hook_settings`/`memories_filed_away` server-scoped).
- Full attribution chokepoint with `caller_id = "default"` single-identity config. Write-path invariant enforced everywhere — including the tunnel ops that MemPalace's stdio skips.
- Embedding-model pinning with start-time mismatch refusal.
- `mempalace-server` CLI with `status | logs | stop | restart | upgrade | backup | shell` for one palace at a time.
- Docker image published to GHCR with versioned tags.
- Launcher `ensure_mempalace` helper shipped as a reference shell function (or separate tooling repo — decide in TDD).
- Migration runbook as a scripted `mempalace-server migrate <data-root>` subcommand that handles the `caller_id` DDL and embedding-model recording.
- Structured logging, basic `/metrics` endpoint.
- Test suites per the TDD's test strategy.

### Scope (out — deferred to v2)

- Multi-identity tokens (the map structure is present but only `default` is used).
- Identity-scoped read filters.
- Read-path auth policy decisions (v1 mirrors write auth).
- Token hot-reload (v1 = stop + restart).
- Atomic WAL+Chroma write (v1 preserves existing non-atomicity).
- Remote hosting / TLS story beyond "works if you front it with a reverse proxy."
- Per-palace eviction / lazy unload.
- Upstream library refactor (parallel work, tracked separately).
- Multi-palace-per-server (explicit non-goal).
- Clustering / HA.

### Milestones

1. **M1 — Write path complete.** All 6 palace-write tools plus tunnel ops routed through the chokepoint. WAL entries include `caller_id`. Migration DDL adds the column. Tests pass.
2. **M2 — Read path complete.** All 18 read tools wired up. Hybrid search with closet boost + BM25 rerank. KG query with temporal filtering. Admin tools rescoped.
3. **M3 — Ops.** CLI subcommands, Dockerfile, GHCR publishing, health + metrics, structured logs.
4. **M4 — Migration + pilot.** Scripted migration against one real palace with one user. One-week soak period as described in the PRD. Rollback exercised at least once against a test palace to prove the runbook.
5. **M5 — v1 release.** Tag, write release notes, close open questions that the v1 process resolved.

### Acceptance criteria

- One real palace migrated and in use by one real user for one week without regression.
- Rollback procedure executed successfully on a test palace.
- All spike-measured performance targets met or exceeded on the same palace post-migration.
- All 31 tools exercised at least once against the running server.
- Two-client concurrent workload runs for 1 hour without corruption.

### Time estimate

3–5 weeks depending on how much Phase 0/1 surfaced.

---

## Sequencing and dependencies

- Phase 0 blocks Phase 1 (TDD numbers come from the spike).
- Phase 1 blocks Phase 2 (implementation references the TDD).
- Upstream library refactor (PRD OQ 1) runs in parallel after Phase 0 — the vendored scope is fixed, so upstream work doesn't block v1.
- Shared-KG migration research (PRD OQ 2) needed before M4 — consumer audit of whether anyone actually shares the KG across palace paths today.

## Decisions needed before Phase 0 starts

1. **Who is the pilot user for M4?** Single real palace with one real user is the migration surface. Self is fine.
2. **Which palace for the spike?** Needs 10K+ drawers to make latency measurements meaningful. Can be a copy of an existing production palace.
3. **SDK vs plain ASGI for streamable-HTTP.** Confirm Anthropic's `mcp` Python SDK supports a server-side streamable-HTTP role. If not, plain FastAPI routes implementing the MCP envelope directly.
4. **Spike code fate.** Default: promote the spike into `server/` on success, delete on failure. Alternative: always delete and start fresh for v1. The former is faster; the latter keeps the v1 code cleaner.

## Out of scope for this plan

- Roadmap beyond v1.
- Cost or budget.
- Any timeline beyond the rough estimates above (these are "order of magnitude" weeks, not commitments).
