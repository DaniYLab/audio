"""Knowledge base store — facade over the KB package.

The production backend is Qdrant (storyforge.kb.qdrant_store); Chroma remains
a dev-only backend until Dev 2 migrates the remaining call sites, then it is
deleted (WORKPLAN_M1 D-list). The universe-scoped contract lives in
storyforge/kb/types.py and is frozen after day 2 of M1.
"""

from __future__ import annotations

from typing import Any, Protocol

from storyforge.core.config import KnowledgeSettings, Settings
from storyforge.core.exceptions import KnowledgeBaseError
from storyforge.core.types import KnowledgeChunk
from storyforge.kb.types import KnowledgeStore as KnowledgeStoreProtocol
from storyforge.kb.types import SearchQuery as KBSearchQuery


class SearchHit(Protocol):
    text: str
    metadata: dict[str, object]
    score: float


class KnowledgeStore(Protocol):
    def index(self, chunks: list[KnowledgeChunk]) -> None: ...
    def search(self, query: str, top_k: int = 8) -> list[SearchHit]: ...


class ChromaKnowledgeStore:
    """Embedded Chroma collection; persists under settings.persist_dir."""

    def __init__(self, settings: KnowledgeSettings) -> None:
        self._settings = settings
        self._collection: Any = None

    def _get_collection(self) -> Any:
        if self._collection is not None:
            return self._collection
        import chromadb  # type: ignore[import-not-found]

        client = chromadb.PersistentClient(path=str(self._settings.persist_dir))
        self._collection = client.get_or_create_collection(
            name=self._settings.collection,
            metadata={"hnsw:space": "cosine"},
        )
        return self._collection

    def _embed(self, texts: list[str]) -> list[list[float]]:
        provider = self._settings.embedding.provider
        if provider == "bge_m3_local":
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

            model = SentenceTransformer(self._settings.embedding.model)
            return [vec.tolist() for vec in model.encode(texts)]
        if provider == "openai":
            import httpx

            base = self._settings.embedding.base_url or "https://api.openai.com/v1"
            key = self._settings.embedding.api_key.get_secret_value()
            response = httpx.post(
                f"{base.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": self._settings.embedding.model,
                    "input": texts,
                },
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()["data"]
            return [item["embedding"] for item in data]
        raise KnowledgeBaseError(f"unknown embedding provider: {provider}")

    def index(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks:
            return
        collection = self._get_collection()
        embeddings = self._embed([c.text for c in chunks])
        collection.upsert(
            ids=[c.chunk_id for c in chunks],
            embeddings=embeddings,
            documents=[c.text for c in chunks],
            metadatas=[c.metadata for c in chunks],
        )

    def search(self, query: str, top_k: int = 8) -> list[SearchHit]:
        collection = self._get_collection()
        embedding = self._embed([query])[0]
        result: dict[str, Any] = dict(
            collection.query(query_embeddings=[embedding], n_results=top_k)
        )

        hits: list[SearchHit] = []
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        for doc, meta, dist in zip(documents, metadatas, distances, strict=False):
            hits.append(_Hit(text=str(doc), metadata=dict(meta or {}), score=1.0 - float(dist)))
        return hits


class _Hit:
    def __init__(self, text: str, metadata: dict[str, object], score: float) -> None:
        self.text = text
        self.metadata = metadata
        self.score = score


def build_knowledge_store(settings: KnowledgeSettings) -> KnowledgeStore:
    """Legacy project-scoped factory (old call sites, to be migrated)."""
    if settings.store == "chroma":
        return ChromaKnowledgeStore(settings)
    raise KnowledgeBaseError(f"unknown knowledge store: {settings.store}")


def build_universe_store(settings: Settings, universe_id: str) -> KnowledgeStoreProtocol:
    """Universe-scoped factory — the M1 contract entrypoint.

    The returned store is BOUND to ``universe_id``; every operation is
    structurally scoped to that universe (design v4 §2.1).
    """
    if not universe_id:
        raise KnowledgeBaseError("universe_id is required — no default magic")
    engine = settings.knowledge.store
    if engine == "qdrant":
        from storyforge.kb.qdrant_store import QdrantKnowledgeStore

        return QdrantKnowledgeStore(settings, universe_id)
    if engine == "memory":
        from storyforge.kb.memory_store import InMemoryKnowledgeStore

        return InMemoryKnowledgeStore(settings, universe_id)
    if engine == "chroma":
        raise KnowledgeBaseError(
            "chroma is not universe-scoped; use qdrant or memory for M1 stores"
        )
    raise KnowledgeBaseError(f"unknown knowledge store: {engine}")


__all__ = [
    "ChromaKnowledgeStore",
    "KBSearchQuery",
    "KnowledgeStore",
    "SearchHit",
    "build_knowledge_store",
    "build_universe_store",
]
