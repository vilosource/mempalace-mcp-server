# How MemPalace Works Today

**Status:** Grounding document — established before the server design is finalized.
**Date:** 2026-04-21
**Source read:** `github.com/vilosource/mempalace` at `v3.3.0`.

This doc catalogs the current MemPalace implementation so that the server design in [`mempalace-server-PRD.md`](mempalace-server-PRD.md) can stand on ground truth rather than speculation. Every claim below is cited with `file:line`.

---

## 1. Overview

MemPalace is a local-first, stdio MCP server that stores verbatim content chunks ("drawers") in a ChromaDB PersistentClient, with a separate SQLite knowledge graph for structured facts. It is organized around the *memory palace* metaphor: **wings** contain **rooms**, rooms contain **drawers**, and **tunnels** link rooms across wings via **halls**. A third storage layer (filesystem) holds config, state, and an append-only forensic WAL log.

Three tightly integrated, conceptually distinct storage tiers:

- **ChromaDB (`chroma.sqlite3` + HNSW bin files)** — verbatim drawer text + embeddings; 2 collections.
- **SQLite KG (`knowledge_graph.sqlite3`)** — structured facts as temporal triples.
- **Filesystem state (`~/.mempalace/`)** — config, entity registry, ingest cursors, forensic WAL.

Version: 3.3.0, development status "beta" (`pyproject.toml:3,16`). No stable Python API — `__all__ = ["__version__"]` (`mempalace/__init__.py:27`).

---

## 2. Terminology and data model

| Concept | What it is | Storage | Defined at |
|---|---|---|---|
| **Drawer** | Atomic verbatim content chunk (~800 chars, never summarized) | ChromaDB `mempalace_drawers` collection | `miner.py:545,548`; ID fmt `drawer_{wing}_{room}_{sha256(wing+room+content)[:24]}` |
| **Closet** | Compressed topic/entity index pointing to drawers (ranking signal, not a gate) | ChromaDB `mempalace_closets` collection | `palace.py:234,209`; line format `topic\|entity;list\|→drawer_id,drawer_id` |
| **Room** | Named topic within a wing (e.g. `authentication`) | Drawer metadata field `room` | String; no schema table |
| **Wing** | Subject/project boundary (e.g. `code`, `personal_diary`) | Drawer metadata field `wing` | Sanitized via `config.py:22` |
| **Hall** | Thematic edge type connecting rooms across wings | Drawer metadata field `hall` | `miner.py:565` via `detect_hall()` |
| **Tunnel** | Graph edge between rooms, stored explicitly or computed from shared halls | `tunnels.json` (explicit) + derived (implicit) | `palace_graph.py:77,249,388` |
| **Diary/Entry** | Daily per-agent drawer at `wing=wing_{agent_name}`, `room="diary"` | ChromaDB drawer | `diary_ingest.py:63-67` |
| **Entity** | Person/project/tool extracted from drawer content | (1) Drawer metadata `entities`; (2) SQLite `entities` table | `miner.py:501`; `knowledge_graph.py:68` |
| **Fact (Triple)** | `subject predicate object` with optional `valid_from`/`valid_to` | SQLite `triples` table | `knowledge_graph.py:76` |
| **Source** | Origin (file path, conversation, diary) | Drawer metadata `source_file` + `source_mtime` | `miner.py` |

Key design principle (`CLAUDE.md`): **verbatim always, incremental only, local-first, byte-preservation**. Drawers are never summarized — this has implications for how the server handles content mutation.

---

## 3. Storage layout

Palace directory is at `~/.mempalace/palace/` (default) — this is distinct from `~/.mempalace/` which holds global config and cross-palace state.

### `~/.mempalace/palace/` (palace data — authoritative)

| Path | Purpose | Authority |
|---|---|---|
| `chroma.sqlite3` | ChromaDB metadata + embeddings (both collections) | **Authoritative** |
| `<uuid>-<uuid>/` | HNSW vector index segments | **Authoritative** for search; rebuildable with effort |
| `.drift-*` | Quarantined stale HNSW segments | Forensic; safe to delete |

### `~/.mempalace/` (global state — outside the palace)

| Path | Purpose | Authority |
|---|---|---|
| `knowledge_graph.sqlite3` | KG entities and triples | **Authoritative** |
| `knowledge_graph.sqlite3-wal/-shm` | SQLite WAL journal files | Derived |
| `config.json` | User settings (entity langs, hooks, wings) | Config (user-editable) |
| `people_map.json` | Entity alias mappings | Config |
| `entity_registry.json` | Entity classification cache | Derived/rebuildable |
| `identity.txt` | Layer-0 user self-description | Config |
| `state/diary_ingest_*.json` | Per-diary incremental ingest cursors | State |
| `locks/<hash>.lock` | Cross-process mine lock (prevents concurrent mining of same source) | Transient |
| `hook_state/*.json` | Claude Code hook state | State |
| `wal/write_log.jsonl` | Forensic audit log (append-only, content-redacted) | Forensic |

**Implication for the server PRD:** the "palace dir" isn't a single bind-mount. The server must decide what to own: just `palace/`, or all of `~/.mempalace/`? The knowledge graph sits at the root, not inside the palace — that's a problem for per-palace isolation.

### SQLite KG schema (`knowledge_graph.py:65-97`)

```sql
CREATE TABLE entities (
    id TEXT PRIMARY KEY,     -- normalized: lower+underscore
    name TEXT NOT NULL,
    type TEXT DEFAULT 'unknown',
    properties TEXT DEFAULT '{}',  -- JSON
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE triples (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT,         -- ISO or NULL
    valid_to TEXT,           -- ISO or NULL
    confidence REAL DEFAULT 1.0,
    source_closet TEXT,      -- provenance
    source_file TEXT,        -- provenance
    source_drawer_id TEXT,   -- RFC 002, link back to drawer
    adapter_name TEXT,       -- RFC 002, which adapter created this
    extracted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (subject) REFERENCES entities(id),
    FOREIGN KEY (object) REFERENCES entities(id)
);
```

Indexes: `idx_triples_subject`, `idx_triples_object`, `idx_triples_predicate`, `idx_triples_valid`.

Schema migration via column introspection, no version field (`knowledge_graph.py:101-115`) — `_migrate_schema()` checks for missing columns and adds them. **`source_drawer_id` and `adapter_name` are retrofitted on older palaces**. WAL mode enabled via `PRAGMA journal_mode=WAL` (`:66`); concurrency inside a single process uses `threading.Lock` (`:60`).

### Chroma collections

Two collections per palace, both in the same `chroma.sqlite3`:

1. **`mempalace_drawers`** (`palace.py:53`) — primary verbatim corpus.
   - Document ID: `drawer_{wing}_{room}_{sha256(source_file+chunk_index)[:24]}` (deterministic, content-addressed; `miner.py:548`).
   - Metadata: `wing`, `room`, `source_file`, `chunk_index`, `added_by`, `filed_at`, `source_mtime`, `hall`, `entities` (semicolon-separated, max 25 per `miner.py:408`), `normalize_version` (currently 2 per `palace.py:50`), `ingest_mode`, `extract_mode`.
   - Distance metric: cosine (`backends/chroma.py:532`).

2. **`mempalace_closets`** (`palace.py:66`) — compressed index, derived from drawers.
   - Used as a ranking boost signal in search (`searcher.py:6-9`: "a ranking signal, never a gate").

**Deterministic IDs** are important: same content at same wing/room always produces the same drawer_id. This gives write idempotency without explicit dedup logic (`mcp_server.py:636-643` — handler returns `"already_exists"` if ID present).

---

## 4. MCP tool surface

**31 tools total** declared in `mcp_server.py:1144-1560` (TOOLS dict). Dispatch is pure dict lookup (`mcp_server.py:1609-1653`): JSON-RPC method name → TOOLS entry → `handler(**whitelisted_args)`. Arg whitelisting at `:1632` prevents spoofing of internal fields. Schemas are inline JSON Schema.

### Writes (mutate palace — 6)

| Tool | Params (caller-supplied) | Storage | WAL logged |
|---|---|---|---|
| `mempalace_add_drawer` | `wing*`, `room*`, `content*`, `source_file`, `added_by` | ChromaDB drawers | Yes (`"add_drawer"`) |
| `mempalace_update_drawer` | `drawer_id*`, `content`, `wing`, `room` | ChromaDB drawers | Yes (`"update_drawer"`) |
| `mempalace_delete_drawer` | `drawer_id*` | ChromaDB drawers | Yes (`"delete_drawer"`) |
| `mempalace_diary_write` | `agent_name*`, `entry*`, `topic` | ChromaDB drawers (diary) | Yes (`"diary_write"`) |
| `mempalace_kg_add` | `subject*`, `predicate*`, `object*`, `valid_from`, `source_closet` | SQLite KG | Yes (`"kg_add"`) |
| `mempalace_kg_invalidate` | `subject*`, `predicate*`, `object*`, `ended` | SQLite KG | Yes (`"kg_invalidate"`) |

### Writes (mutate graph — 2, **not WAL-logged**)

| Tool | Storage | WAL logged |
|---|---|---|
| `mempalace_create_tunnel` | `tunnels.json` | **No** — `palace_graph.py` does not call `_wal_log` |
| `mempalace_delete_tunnel` | `tunnels.json` | **No** — same |

**This is a pre-existing forensic gap.** The server design should either preserve and explicitly acknowledge it, or fix it (add WAL entries for tunnel ops).

### Reads (18)

`mempalace_status`, `mempalace_list_wings`, `mempalace_list_rooms`, `mempalace_get_taxonomy`, `mempalace_get_aaak_spec`, `mempalace_search`, `mempalace_check_duplicate`, `mempalace_traverse`, `mempalace_find_tunnels`, `mempalace_graph_stats`, `mempalace_list_tunnels`, `mempalace_follow_tunnels`, `mempalace_kg_query`, `mempalace_kg_timeline`, `mempalace_kg_stats`, `mempalace_get_drawer`, `mempalace_list_drawers`, `mempalace_diary_read`.

### Admin/meta (5)

`mempalace_hook_settings`, `mempalace_memories_filed_away`, `mempalace_reconnect`.

`mempalace_reconnect` is noteworthy: it invalidates the module-level `_collection_cache` so a fresh ChromaDB client is built on next call (`mcp_server.py:1119-1126`). **This is an existing escape hatch for cross-process drift** — evidence that the stdio code already knows its caches drift when external writes happen.

### Attribution-adjacent caller-supplied params

- `added_by` (default `"mcp"`) on `mempalace_add_drawer` (`:606,1410`)
- `source_file` on `mempalace_add_drawer` (`:606`)
- `source_closet` on `mempalace_kg_add` (`:856`)
- `agent_name` on `mempalace_diary_write` — drives the wing name (`wing_{agent_name.lower()}`) (`:921,935`)

All are free strings today. None are authenticated.

---

## 5. Write path (end-to-end, for `add_drawer`)

From `mcp_server.py:1608` (dispatch) → `tool_add_drawer` at `:605`:

1. **Input sanitization.** `sanitize_name(wing, "wing")` / `sanitize_name(room, "room")` (`config.py:22-47`, alphanumeric + space/hyphen, ≤128 chars). `sanitize_content(content)` (`config.py:74-82`, ≤100KB, no null bytes).
2. **Deterministic ID.** `drawer_id = f"drawer_{wing}_{room}_{sha256(wing+room+content)[:24]}"` (`:621-622`).
3. **Idempotency check.** `col.get(ids=[drawer_id])` — if present, early return `{"success": True, "reason": "already_exists"}` (`:636-643`).
4. **WAL log.** `_wal_log("add_drawer", {drawer_id, wing, room, added_by, content_length, content_preview})` (`:625-635`). Content is redacted to a preview. Appended via `os.O_WRONLY | os.O_APPEND | os.O_CREAT` (`:155`).
5. **Chroma upsert.** `col.upsert(documents=[content], ids=[drawer_id], metadatas=[{...}])` (`:646-659`). **Embedding happens here, inside the call, via ChromaDB's default embedding function.**
6. **Cache invalidation.** `_metadata_cache = None` (`:660-661`) — the module-level palace metadata cache.
7. **Response.** `{"success": True, "drawer_id": "...", "wing": "...", "room": "..."}`.

**No cross-store transaction.** Step 4 (WAL write) and step 5 (Chroma upsert) are independent. A crash between them leaves a WAL entry with no corresponding drawer. There is no recovery code that reconciles this. The forensic log drifts from truth on partial failure.

Tunnel writes (`create_tunnel`, `delete_tunnel`) follow a different path through `palace_graph.py` and do not touch the WAL.

---

## 6. Read path (end-to-end, for `mempalace_search`)

From `mcp_server.py:1349` (dispatch) → `tool_search` at `:428`:

1. **Query sanitization.** `sanitize_query(query)` (`query_sanitizer.py:39-188`). If ≤200 chars: passthrough. If >200: extract a question-mark sentence, tail sentence, or fallback to last 250 chars.
2. **Vector query.** `drawers_col.query(query_texts=[clean], n_results=n_results*3, where=filter)` — over-fetch by 3× for reranking. `where` filter built from `wing`/`room` args (`searcher.py:150-158`). **Embedding of the query happens here, inside ChromaDB.**
3. **Closet boost.** Secondary query against `mempalace_closets` collection (`searcher.py:357-381`). Rank-based boost: `[0.40, 0.25, 0.15, 0.08, 0.04]` (`:386`). Applied if closet distance ≤ 1.5.
4. **Distance filter.** Drop results where `distance > max_distance` (`:396`).
5. **BM25 hybrid rerank.** `_hybrid_rank(hits, query, vector_weight=0.6, bm25_weight=0.4)` (`:494`). BM25 computed corpus-relative over the candidate set.
6. **Metadata enrichment.** Each result gets `{wing, room, source_file (basename), created_at, similarity, distance, effective_distance, closet_boost, matched_via, bm25_score}`.

No caching layer between MCP and backend (module-level caches invalidate on every write).

`mempalace_kg_query` is a separate path entirely — pure SQLite relational lookup with temporal filtering, no embeddings involved (`knowledge_graph.py:240-295`).

---

## 7. Embeddings

**Who produces them:** ChromaDB's built-in embedding function, invoked automatically inside `col.upsert(documents=...)` and `col.query(query_texts=...)`. MemPalace has no explicit embedding code.

**Default model:** ChromaDB v1.5.x default is `all-MiniLM-L6-v2` via sentence-transformers (384-dim).

**Configuration:** None. Model name/version is not in `config.json` and not referenced in MemPalace code. Whatever ChromaDB ships as default is what gets used.

**Loading:** Eager on first `upsert` or `query` call (ChromaDB lazy-init pattern).

**Process placement:** Same process as the MCP server. Runs in-process; not outsourced.

**Version-mismatch detection:** **None.** If ChromaDB upgrades and changes its default model, stored embeddings keep the old dimensionality and new queries use the new model. Potential silent dimension mismatch or garbage distance scores. No migration tooling.

This is a real landmine for the server design — the server must pin the embedding model explicitly or inherit this risk.

---

## 8. Package structure and configuration

### Entry points (`pyproject.toml:40-43`)

- CLI: `mempalace` → `mempalace/cli.py` with subcommands `init | split | mine | search | mcp | wake-up | status` (`cli.py:12-20`).
- MCP server: `python -m mempalace.mcp_server` — runs the full process with global state.
- Hooks: `hooks/mempal_save_hook.sh`, `hooks/mempal_precompact_hook.sh` (Claude Code shell hooks).
- No library entry point.

### Library surface

Exports from `mempalace/__init__.py:27`: `__all__ = ["__version__"]` — literally just the version string. No `Palace` facade class.

Public-ish classes: `KnowledgeGraph` (`knowledge_graph.py:50`), `PalaceContext` (`sources/context.py:49`). The latter explicitly warns that adapters "MUST NOT import `mempalace.palace` directly" (`sources/context.py:6`).

### Module-level init in `mcp_server.py`

Triggered at import time:

- Lines 34-42: stdio hijacking (overrides `sys.stdout` for MCP protocol safety).
- Line 92: `_args = _parse_args()` — CLI args parsed at import, not construction.
- Line 97: `_config = MempalaceConfig()` — singleton config loaded.
- Lines 101-103: `_kg = KnowledgeGraph()` — singleton KG.
- Lines 106-109, 117-123, 263-265: five module-level caches (client, collection, metadata, WAL dir, WAL file).

**Implication: library-mode reuse requires refactoring.** A new server process cannot cleanly `from mempalace.mcp_server import tool_add_drawer` without inheriting all of the above, including stdio hijacking that breaks any host process.

### Plugin surface

- **Source adapters** via `importlib.metadata.entry_points()` under group `mempalace.sources` (`sources/registry.py:68-93`). RFC 002 defines the contract.
- **Storage backends** via entry-point group `mempalace.backends` (`pyproject.toml:42-43`) — ChromaDB is the only one shipped.
- **Miners** (`miner.py`, `convo_miner.py`) are hardcoded, not pluggable. `sources/base.py:8-12` signals they will migrate to `BaseSourceAdapter` later.

### Configuration surface

**Environment variables:**

- `MEMPALACE_PALACE_PATH` / `MEMPAL_PALACE_PATH` (`config.py:169`) — palace directory override.
- `MEMPALACE_ENTITY_LANGUAGES` / `MEMPAL_ENTITY_LANGUAGES` (`config.py:208`) — comma-separated language list.
- `MEMPALACE_SOURCE_DIR` (`split_mega_files.py:32`).
- `LLM_ENDPOINT`, `LLM_KEY`, `LLM_MODEL` (`closet_llm.py:101-103`) — optional, advanced features.

**Config file:** `~/.mempalace/config.json` (`config.py:155`) — holds palace path, collection name, topic wings, hall keywords, entity languages, hooks, people map.

**Precedence:** env > config file > defaults (`config.py:169-172,204-216`).

### Palace identity

Palace is identified by **filesystem path** — `MEMPALACE_PALACE_PATH` (default `~/.mempalace/palace`). No UUID, no internal palace name. Single palace per process (`_config` and `_kg` singletons in `mcp_server.py:97,101-103`).

If two processes target the same palace dir: no advisory locking, no warning. `_get_client()` detects external DB changes via inode/mtime (`mcp_server.py:162-211`) and transparently reconnects — this is partial mitigation for concurrent-access drift but does not prevent the HNSW corruption documented in `backends/chroma.py:52-67`.

---

## 9. Observations that shape the server design

Listed as observations here; the design response lives in the PRD.

1. **Library-mode reuse is not viable without refactoring.** Import-time globals, stdio hijacking, and single-palace-per-process design make `from mempalace import ...` unsuitable for driving MemPalace from a server process. The server must either fork/absorb MemPalace code (copy + modify) or push upstream refactors first.
2. **"Palace dir" is not a single directory.** The knowledge graph, config, entity registry, and forensic WAL live at `~/.mempalace/` (global), while Chroma data lives at `~/.mempalace/palace/` (palace-specific). A per-palace server must own or share both layers.
3. **The existing forensic WAL is partial.** `create_tunnel`/`delete_tunnel` don't log. The server should either preserve this gap explicitly or fix it.
4. **Write path has no cross-store transaction.** WAL log + Chroma upsert are independent; partial failure drifts the log from reality. This is a pre-existing condition the server can either preserve or address.
5. **`added_by` is a caller-supplied free string, redundant with no server-set identity.** The server's `caller_id` field sits alongside it cleanly, as already designed in the PRD.
6. **KG already carries drawer-level provenance** via `source_drawer_id` + `adapter_name` (RFC 002). The `caller_id` addition should be consistent with this model, not parallel to it.
7. **Deterministic content-addressed drawer IDs** mean multi-writer semantics default to "last writer wins on identical content ID" — which reduces to idempotent re-write. The hard case is two writers writing *different* content with the *same* wing/room/content prefix... which can't happen, because the ID includes the full content hash. Concurrent-write concerns collapse to: two different writes creating two different drawers in parallel, which SQLite + Chroma can handle serially inside the server.
8. **Embedding model is implicit and unconfigured.** Relying on ChromaDB's default is fragile. The server should pin an explicit model and reject operations against palaces created with a different model dimension.
9. **`tool_reconnect` exists.** The stdio code already acknowledges that its in-memory caches drift on external writes. This is supporting evidence for the server approach — the current design has already met this problem and works around it.
10. **31 tools is a larger surface than assumed.** Server must either expose all of them, an explicit subset, or wrap through a generic passthrough. Admin tools (`tool_hook_settings`, `tool_memories_filed_away`, `tool_reconnect`) have unclear semantics under a shared server — they touch per-process state that no longer exists the same way.
11. **No schema version field anywhere.** KG migrations introspect; drawer metadata has `normalize_version = 2` as a rebuild gate. No global "palace version" concept. Adding one is a server-era requirement.
12. **Closets are derivable, not authoritative.** The `mempalace_closets` collection is a ranking signal built from drawers. It can be rebuilt. This may matter for backup/restore strategy.

---

## 10. References

All file:line citations above point at `github.com/vilosource/mempalace` at `v3.3.0`. Representative anchors:

- [`mempalace/__init__.py`](https://github.com/vilosource/mempalace/blob/main/mempalace/__init__.py) — library surface (`__all__`)
- [`mempalace/mcp_server.py`](https://github.com/vilosource/mempalace/blob/main/mempalace/mcp_server.py) — entrypoint, dispatch, TOOLS dict (`:1144-1560`), WAL log (`:117-160`), module-level init (`:92-123`)
- [`mempalace/palace.py`](https://github.com/vilosource/mempalace/blob/main/mempalace/palace.py) — collection accessors (`:53,66`)
- [`mempalace/knowledge_graph.py`](https://github.com/vilosource/mempalace/blob/main/mempalace/knowledge_graph.py) — schema (`:65-97`), migrations (`:101-115`)
- [`mempalace/backends/chroma.py`](https://github.com/vilosource/mempalace/blob/main/mempalace/backends/chroma.py) — PersistentClient (`:469,490`), quarantine workaround (`:52-67`)
- [`mempalace/searcher.py`](https://github.com/vilosource/mempalace/blob/main/mempalace/searcher.py) — hybrid search + BM25 rerank
- [`mempalace/palace_graph.py`](https://github.com/vilosource/mempalace/blob/main/mempalace/palace_graph.py) — tunnels (not WAL-logged)
- [`mempalace/config.py`](https://github.com/vilosource/mempalace/blob/main/mempalace/config.py) — config schema, env var reads, sanitizers
- [`mempalace/sources/registry.py`](https://github.com/vilosource/mempalace/blob/main/mempalace/sources/registry.py) — entry-point plugin discovery (RFC 002)
- [`pyproject.toml`](https://github.com/vilosource/mempalace/blob/main/pyproject.toml) — version, entry points, backend registry
