from __future__ import annotations

import os
from typing import Protocol

from config import Config


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    def __init__(
        self,
        model_name: str,
        batch_size: int = 32,
        query_prefix: str = "",
        document_prefix: str = "",
        hf_endpoint: str | None = None,
        cache_dir: str | None = None,
    ):
        if hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", hf_endpoint)
        from fastembed import TextEmbedding

        kwargs = {"model_name": model_name}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        self.model = TextEmbedding(**kwargs)
        self.batch_size = batch_size
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.document_prefix:
            texts = [self.document_prefix + t for t in texts]
        vectors = self.model.embed(texts, batch_size=self.batch_size)
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        vectors = self.model.embed(
            [self.query_prefix + text], batch_size=self.batch_size
        )
        return next(iter(vectors)).tolist()


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        model_name: str,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        batch_size: int = 64,
        query_prefix: str = "",
        document_prefix: str = "",
    ):
        from openai import OpenAI

        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(
                f"缺少环境变量 {api_key_env}，无法使用 openai-compatible embedding。"
            )
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model_name
        self.batch_size = batch_size
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.document_prefix:
            texts = [self.document_prefix + t for t in texts]
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            response = self.client.embeddings.create(model=self.model, input=batch)
            vectors.extend(item.embedding for item in response.data)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        response = self.client.embeddings.create(
            model=self.model, input=[self.query_prefix + text]
        )
        return response.data[0].embedding


def create_embedder(cfg: Config) -> Embedder:
    provider = str(cfg.get("embedding.provider", "fastembed")).strip()
    model = str(cfg.get("embedding.model", "BAAI/bge-small-zh-v1.5")).strip()
    batch_size = int(cfg.get("embedding.batch_size", 32))
    query_prefix = str(cfg.get("embedding.query_prefix", "") or "")
    document_prefix = str(cfg.get("embedding.document_prefix", "") or "")

    if provider == "fastembed":
        return FastEmbedEmbedder(
            model_name=model,
            batch_size=batch_size,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
            hf_endpoint=cfg.get("embedding.hf_endpoint"),
        )
    if provider in {"openai", "openai-compatible"}:
        return OpenAICompatibleEmbedder(
            model_name=model,
            base_url=cfg.get("embedding.base_url"),
            api_key_env=str(cfg.get("embedding.api_key_env", "OPENAI_API_KEY")),
            batch_size=batch_size,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
        )
    raise ValueError(f"未知的 embedding provider: {provider}")
