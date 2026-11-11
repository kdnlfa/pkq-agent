from __future__ import annotations

from pathlib import Path
from typing import Any

from chunker import Chunk


class VectorStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

        import chromadb
        from chromadb.config import Settings

        self.client = chromadb.PersistentClient(
            path=str(self.path),
            settings=Settings(anonymized_telemetry=False),
        )

    def collection(self, name: str, reset: bool = False):
        if reset:
            try:
                self.client.delete_collection(name)
            except Exception:
                pass
        return self.client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(
        self,
        name: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
        batch_size: int = 256,
    ) -> None:
        if not chunks:
            return
        collection = self.collection(name)
        for i in range(0, len(chunks), batch_size):
            part = chunks[i : i + batch_size]
            collection.upsert(
                ids=[c.id for c in part],
                documents=[c.text for c in part],
                metadatas=[c.metadata for c in part],
                embeddings=embeddings[i : i + batch_size],
            )

    def delete_path(self, name: str, rel_path: str) -> None:
        self.collection(name).delete(where={"path": rel_path})

    def replace_path(
        self,
        name: str,
        rel_path: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> None:
        collection = self.collection(name)
        previous_ids = collection.get(where={"path": rel_path}, include=[])["ids"]
        self.upsert(name, chunks, embeddings)
        current_ids = {chunk.id for chunk in chunks}
        stale_ids = [chunk_id for chunk_id in previous_ids if chunk_id not in current_ids]
        if stale_ids:
            collection.delete(ids=stale_ids)

    def count(self, name: str) -> int:
        return self.collection(name).count()

    def query(
        self,
        name: str,
        embedding: list[float],
        n_results: int,
        where: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        collection = self.collection(name)
        total = collection.count()
        if total == 0:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        kwargs: dict[str, Any] = {
            "query_embeddings": [embedding],
            "n_results": max(1, min(n_results, total)),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        return collection.query(**kwargs)

    def get_all(
        self, name: str, where: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        collection = self.collection(name)
        kwargs: dict[str, Any] = {"include": ["documents", "metadatas"]}
        if where:
            kwargs["where"] = where
        return collection.get(**kwargs)
