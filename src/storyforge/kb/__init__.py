"""KB package: universe-scoped long-term memory (KNOWLEDGE_BASE_DESIGN.md v4).

Layout (Dev 1 owns the backend half, Dev 2 the compiler half):
    types.py         frozen contract — models + KnowledgeStore protocol
    embedder.py      EmbeddingProvider (openai | bge_m3_local), dense+sparse
    entities.py      rule-based entity/alias extraction for ingest
    alias.py         YAML alias table per universe + bootstrap report
    ingest.py        write path: normalize -> alias -> chunk -> embed -> upsert
    qdrant_store.py  Qdrant backend (hybrid dense+sparse, RRF)
    memory_store.py  in-memory fake for unit tests / Dev 2 unblocked
"""

from __future__ import annotations
