from __future__ import annotations

import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from .config import settings
from .embeddings import EMBED_DIM, Embedder


@dataclass
class RetrievedChunk:
    """One runbook snippet returned from a search, with where it came from."""

    text: str
    source: str
    score: float


class VectorStore:
    def __init__(self) -> None:
        # The client reads the URL from settings — never a hardcoded localhost.
        # api_key is None for sidecar/service and set for an external Qdrant.
        self._client = QdrantClient(
            url=settings.vector_db_url,
            api_key=settings.vector_db_api_key,
        )
        self._collection = settings.collection_name
        self._embedder = Embedder()

    def ensure_collection(self) -> None:
        """Create the collection if it doesn't exist yet (idempotent).

        A 'collection' in Qdrant is like a table. It is created with a fixed
        vector size (EMBED_DIM) and a distance metric (COSINE) used to measure
        'closeness' between vectors. Both must match the embedding model.
        """
        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection not in existing:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
            )

    def upsert(self, chunks: list[str], source: str) -> int:
        """Embed `chunks` and store them. Returns how many were written.

        'Upsert' = insert-or-update. Each chunk becomes a 'point': an id, the
        vector, and a 'payload' (arbitrary metadata — here the text and source).
        """
        if not chunks:
            return 0
        vectors = self._embedder.embed(chunks)
        points = [
            PointStruct(
                # Deterministic id from (source, text) so re-upserting the same
                # chunk overwrites its point instead of creating a duplicate.
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}\x00{text}")),
                vector=vector,
                payload={"text": text, "source": source},
            )
            for text, vector in zip(chunks, vectors)
        ]
        self._client.upsert(collection_name=self._collection, points=points)
        return len(points)

    def search(self, query: str, top_k: int = 4) -> list[RetrievedChunk]:
        """Embed the query and return the `top_k` most similar stored chunks."""
        query_vector = self._embedder.embed([query])[0]
        response = self._client.query_points(
            collection_name=self._collection,
            query=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return [
            RetrievedChunk(
                text=hit.payload.get("text", ""),
                source=hit.payload.get("source", "unknown"),
                score=hit.score,
            )
            for hit in response.points
        ]