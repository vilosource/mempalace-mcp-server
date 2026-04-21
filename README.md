# mempalace-mcp-server

Streamable-HTTP MCP server for [MemPalace](https://github.com/vilosource/mempalace) palaces.

**Status:** design phase — PRD in [`doc/`](doc/).

## Why

MemPalace today runs as a per-client stdio MCP server. Every client session opens the palace's SQLite + Chroma HNSW index directly. This breaks under two realistic workloads:

- **Concurrent sessions corrupt the HNSW index.** Two processes writing to the same palace drift metadata vs vector state and can segfault the Chroma PersistentClient. Documented in the MemPalace source at `mempalace/backends/chroma.py:52-67` (the `quarantine_stale_hnsw` workaround for Chroma Issue #823).
- **Single-machine storage.** A palace lives on one disk. Sharing across laptop + workstation + CI isn't practical.

This repo proposes a long-lived, per-palace HTTP MCP server that owns the files as the sole writer. Clients become thin HTTP consumers. Location-transparent: local docker container or remote managed instance.

## Docs

- [MemPalace Server PRD](doc/mempalace-server-PRD.md) — problem, proposal, architecture, open questions.

## Development

See [`.githooks/README.md`](.githooks/README.md) for git hook setup. First-time setup for contributors:

```bash
git config core.hooksPath .githooks
```

## License

MIT — see [LICENSE](LICENSE).
