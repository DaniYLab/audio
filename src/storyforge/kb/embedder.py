"""Embedding providers — one encode call returns dense AND sparse vectors.

BGE-M3 (local or API) produces both in a single forward pass; the OpenAI
provider produces dense only, so it synthesizes a trivial term-frequency
sparse vector so every backend can serve hybrid (RRF) search unchanged.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from storyforge.core.config import EmbeddingSettings, Settings
from storyforge.core.exceptions import ExternalServiceError, KnowledgeBaseError
from storyforge.core.logging import get_logger
from storyforge.core.retry import retry_external

logger = get_logger(__name__)

# Minimum term length for the fallback sparse encoder; single-letter tokens
# are noise in Vietnamese ASR text.
_MIN_SPARSE_TERM = 2


class SparseVector:
    """CSR-style sparse vector: parallel index/value lists."""

    def __init__(self, indices: list[int], values: list[float]) -> None:
        self.indices = indices
        self.values = values

    def as_dict(self) -> dict[int, float]:
        return dict(zip(self.indices, self.values, strict=False))


class Embedding:
    """Dense + sparse embedding for one text."""

    def __init__(self, dense: list[float], sparse: SparseVector) -> None:
        self.dense = dense
        self.sparse = sparse


@runtime_checkable
class EmbeddingProvider(Protocol):
    def encode(self, texts: list[str]) -> list[Embedding]: ...


class OpenAIEmbeddingProvider:
    """OpenAI-compatible /embeddings endpoint (also used for BGE-M3 APIs)."""

    def __init__(self, settings: EmbeddingSettings, llm_base_url: str, llm_api_key: str) -> None:
        self._settings = settings
        self._base_url = (settings.base_url or llm_base_url).rstrip("/")
        self._api_key = settings.api_key.get_secret_value() or llm_api_key
        if not self._api_key:
            raise KnowledgeBaseError(
                "embedding api_key is required (SF__KNOWLEDGE__EMBEDDING__API_KEY "
                "or SF__LLM__API_KEY)"
            )

    def encode(self, texts: list[str]) -> list[Embedding]:
        import httpx

        def _call() -> dict[str, object]:
            response = httpx.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._settings.model, "input": texts},
                timeout=60.0,
            )
            if response.status_code in (429, 500, 502, 503, 504):
                raise ExternalServiceError(f"embedding http {response.status_code}", retryable=True)
            if response.status_code != 200:
                raise KnowledgeBaseError(
                    f"embedding http {response.status_code}: {response.text[:500]}"
                )
            return dict(response.json())

        data = retry_external(_call)
        items = data.get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            raise KnowledgeBaseError("malformed embedding response")

        # One vocab per batch so the synthetic sparse indices are consistent
        # across all texts in this encode call.
        vocab: dict[str, int] = {}
        embeddings: list[Embedding] = []
        for item, text in zip(items, texts, strict=False):
            if not isinstance(item, dict) or "embedding" not in item:
                raise KnowledgeBaseError("malformed embedding item")
            dense_value = item["embedding"]
            if not isinstance(dense_value, list):
                raise KnowledgeBaseError("malformed embedding vector")
            embeddings.append(
                Embedding([float(x) for x in dense_value], _tfidf_sparse(text, vocab))
            )
        return embeddings


class BGEM3LocalEmbeddingProvider:
    """BGE-M3 via FlagEmbedding: dense + sparse (lexical weights) in one pass.

    Heavy import (torch/FlagEmbedding) and the model load stay inside the
    method per CONVENTIONS.md; the loaded model is cached on the instance so
    repeated encode calls within a process do not reload weights.
    """

    def __init__(self, settings: EmbeddingSettings) -> None:
        self._settings = settings
        self._model: Any = None

    def _load_model(self) -> Any:
        if self._model is None:
            from FlagEmbedding import BGEM3FlagModel  # type: ignore[import-untyped]

            logger.info("loading BGE-M3 model", model=self._settings.model)
            self._model = BGEM3FlagModel(
                self._settings.model,
                use_fp16=True,
            )
        return self._model

    def encode(self, texts: list[str]) -> list[Embedding]:
        model = self._load_model()
        out: dict[str, Any] = model.encode(
            texts,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense_all: list[Any] = list(out["dense_vecs"])
        sparse_all: list[dict[Any, Any]] = list(out["lexical_weights"])
        embeddings: list[Embedding] = []
        for i in range(len(texts)):
            dense = [float(x) for x in dense_all[i]]
            weights = {int(k): float(v) for k, v in sparse_all[i].items() if float(v) > 0}
            indices = sorted(weights)
            embeddings.append(
                Embedding(dense, SparseVector(indices, [weights[j] for j in indices]))
            )
        return embeddings


def _tfidf_sparse(text: str, vocab: dict[str, int] | None = None) -> SparseVector:
    """Fallback sparse encoder: bag-of-words over normalized terms.

    ``vocab`` maps term -> a stable synthetic index so dense-only providers
    still produce consistent sparse vectors across calls within one encode.
    """
    if vocab is None:
        vocab = {}
    counts: dict[int, float] = {}
    for term in _terms(text):
        if term not in vocab:
            vocab[term] = len(vocab)
        counts[vocab[term]] = counts.get(vocab[term], 0.0) + 1.0
    indices = sorted(counts)
    return SparseVector(indices, [counts[i] for i in indices])


def _terms(text: str) -> list[str]:
    normalized = (
        text.lower()
        .replace(".", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace(":", " ")
        .replace("!", " ")
        .replace("?", " ")
    )
    return [t for t in normalized.split() if len(t) >= _MIN_SPARSE_TERM]


def build_embedder(settings: Settings) -> EmbeddingProvider:
    embedding = settings.knowledge.embedding
    if embedding.provider == "openai":
        return OpenAIEmbeddingProvider(
            embedding,
            settings.llm.base_url,
            settings.llm.api_key.get_secret_value(),
        )
    if embedding.provider == "bge_m3_local":
        return BGEM3LocalEmbeddingProvider(embedding)
    raise KnowledgeBaseError(f"unknown embedding provider: {embedding.provider}")
