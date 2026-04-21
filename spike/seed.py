"""Seed a spike palace with ~10K drawers from Wikipedia.

Populates /tmp/mempalace-spike/ with a data root that mirrors a MemPalace
palace's on-disk shape. Chroma collection is `mempalace_drawers`.

Not a production ingest path — bypasses MemPalace's pipeline and writes
directly to ChromaDB. The goal is to produce a palace of realistic size
for latency/RAM measurements, not to exercise the real ingest code.

Usage:
    .venv/bin/python seed.py [target_drawers]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import requests

PALACE_ROOT = Path("/tmp/mempalace-spike")
CHROMA_PATH = PALACE_ROOT / "palace"
WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "mempalace-mcp-server-spike/0.1 (https://github.com/vilosource/mempalace-mcp-server)"

TARGET_DRAWERS = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
CHUNK_SIZE = 800
BATCH_ARTICLES = 20  # generator=random max per call
ROOMS = [
    "general", "science", "history", "culture", "technology",
    "geography", "biology", "mathematics", "literature", "politics",
]


def fetch_random_articles(n: int) -> list[dict]:
    """Fetch n random Wikipedia articles with full plaintext extracts."""
    params = {
        "action": "query",
        "format": "json",
        "generator": "random",
        "grnnamespace": "0",
        "grnlimit": str(n),
        "prop": "extracts",
        "explaintext": "1",
        "exlimit": str(n),
    }
    r = requests.get(WIKI_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    pages = r.json().get("query", {}).get("pages", {})
    return [
        {"title": p["title"], "text": p.get("extract", "")}
        for p in pages.values()
        if p.get("extract")
    ]


def chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Word-boundary chunking into ~size-char pieces."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks = []
    i = 0
    while i < len(text):
        end = min(i + size, len(text))
        if end < len(text):
            space = text.rfind(" ", i, end)
            if space > i + size // 2:
                end = space
        chunks.append(text[i:end].strip())
        i = end
    return [c for c in chunks if c]


def pick_room(title: str) -> str:
    h = int(hashlib.sha256(title.encode()).hexdigest(), 16)
    return ROOMS[h % len(ROOMS)]


def drawer_id(wing: str, room: str, content: str) -> str:
    h = hashlib.sha256(f"{wing}{room}{content}".encode()).hexdigest()[:24]
    return f"drawer_{wing}_{room}_{h}"


def write_config_and_kg():
    """Minimal MemPalace-shaped state alongside the Chroma palace dir."""
    PALACE_ROOT.mkdir(parents=True, exist_ok=True)
    config = {
        "palace_path": str(CHROMA_PATH),
        "collection_name": "mempalace_drawers",
        "embedding_model": "all-MiniLM-L6-v2",
        "embedding_dim": 384,
    }
    (PALACE_ROOT / "config.json").write_text(json.dumps(config, indent=2))

    import sqlite3
    kg_path = PALACE_ROOT / "knowledge_graph.sqlite3"
    conn = sqlite3.connect(kg_path)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            type TEXT DEFAULT 'unknown', properties TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS triples (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
            valid_from TEXT, valid_to TEXT,
            confidence REAL DEFAULT 1.0,
            source_closet TEXT, source_file TEXT,
            source_drawer_id TEXT, adapter_name TEXT,
            extracted_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
        CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object);
        CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
        CREATE INDEX IF NOT EXISTS idx_triples_valid ON triples(valid_from, valid_to);
    """)
    conn.close()


def main():
    print(f"seeding {TARGET_DRAWERS} drawers into {PALACE_ROOT}")
    write_config_and_kg()
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    col = client.get_or_create_collection(
        name="mempalace_drawers",
        metadata={"hnsw:space": "cosine"},
    )
    existing = col.count()
    print(f"  existing drawers: {existing}")

    inserted = 0
    seen_ids: set[str] = set()
    batch_docs: list[str] = []
    batch_ids: list[str] = []
    batch_meta: list[dict] = []
    BATCH_UPSERT = 200

    t_start = time.time()
    articles_fetched = 0

    while inserted < TARGET_DRAWERS - existing:
        try:
            articles = fetch_random_articles(BATCH_ARTICLES)
        except Exception as e:
            print(f"  wiki fetch error: {e}; sleeping 5s")
            time.sleep(5)
            continue
        articles_fetched += len(articles)

        for art in articles:
            title = art["title"]
            text = art["text"]
            if not text or len(text) < 200:
                continue
            room = pick_room(title)
            wing = "wikipedia"
            for idx, chunk in enumerate(chunk_text(text)):
                if len(chunk) < 80:
                    continue
                did = drawer_id(wing, room, chunk)
                if did in seen_ids:
                    continue
                seen_ids.add(did)
                batch_docs.append(chunk)
                batch_ids.append(did)
                batch_meta.append({
                    "wing": wing,
                    "room": room,
                    "source_file": title,
                    "chunk_index": idx,
                    "added_by": "spike-seeder",
                    "filed_at": datetime.now(timezone.utc).isoformat(),
                    "source_mtime": 0.0,
                    "hall": "",
                    "entities": "",
                    "normalize_version": 2,
                    "ingest_mode": "spike-synthetic",
                    "extract_mode": "full",
                })

                if len(batch_docs) >= BATCH_UPSERT:
                    col.upsert(documents=batch_docs, ids=batch_ids, metadatas=batch_meta)
                    inserted += len(batch_docs)
                    elapsed = time.time() - t_start
                    rate = inserted / elapsed if elapsed > 0 else 0
                    print(f"  inserted {inserted}/{TARGET_DRAWERS - existing} "
                          f"({rate:.1f}/s) articles={articles_fetched}")
                    batch_docs, batch_ids, batch_meta = [], [], []
                    if inserted >= TARGET_DRAWERS - existing:
                        break
            if inserted >= TARGET_DRAWERS - existing:
                break

        time.sleep(0.5)  # be polite to wikipedia

    if batch_docs:
        col.upsert(documents=batch_docs, ids=batch_ids, metadatas=batch_meta)
        inserted += len(batch_docs)

    total = col.count()
    elapsed = time.time() - t_start
    print(f"done: inserted={inserted} total={total} articles_fetched={articles_fetched} "
          f"elapsed={elapsed:.1f}s")


if __name__ == "__main__":
    main()
