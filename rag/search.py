from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from config import Config
from corpus import read_body
from embeddings import create_embedder
from store import VectorStore

jieba.setLogLevel(logging.WARNING)

TOKEN_RE = re.compile(r"[\w一-鿿]")


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = [t.strip() for t in jieba.cut_for_search(text)]
    return [t for t in tokens if t and TOKEN_RE.search(t)]


def build_where(
    status: str | None = None, doc_type: str | None = None
) -> dict[str, Any] | None:
    conditions: list[dict[str, Any]] = []
    if status:
        conditions.append({"status": status})
    if doc_type:
        conditions.append({"type": doc_type})
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def metadata_matches(
    metadata: dict[str, Any], status: str | None, doc_type: str | None
) -> bool:
    if status and metadata.get("status") != status:
        return False
    if doc_type and metadata.get("type") != doc_type:
        return False
    return True


def make_snippet(text: str, limit: int = 240) -> str:
    body_lines = [
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ]
    snippet = re.sub(r"\s+", " ", " ".join(body_lines)).strip()
    if len(snippet) > limit:
        snippet = snippet[:limit].rstrip() + "…"
    return snippet


class Searcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = VectorStore(cfg.store_path)
        self._embedder = None
        # (collection, status, doc_type) -> 全量文件的 BM25 语料
        self._corpus_cache: dict[tuple, tuple] = {}

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = create_embedder(self.cfg)
        return self._embedder

    def _bm25_corpus(
        self, collection: str, status: str | None, doc_type: str | None
    ) -> tuple[list[str], list[list[str]], list[str], dict[str, list[str]], dict[str, dict]]:
        """按文件路径构建 BM25 语料。

        向量库里只存关键信息，BM25 需要完整正文，所以这里从 .md 文件读取。
        同一文件的多个 chunk 共享一条 BM25 记录，避免重复计分。
        """
        cache_key = (collection, status, doc_type)
        if cache_key in self._corpus_cache:
            return self._corpus_cache[cache_key]

        data = self.store.get_all(collection)
        ids = list(data.get("ids") or [])
        metadatas = list(data.get("metadatas") or [])

        path_ids: dict[str, list[str]] = {}
        meta_by_id: dict[str, dict] = {}
        for chunk_id, metadata in zip(ids, metadatas):
            metadata = metadata or {}
            if not metadata_matches(metadata, status, doc_type):
                continue
            path = metadata.get("path", "")
            if not path:
                continue
            path_ids.setdefault(path, []).append(chunk_id)
            meta_by_id[chunk_id] = metadata

        paths = list(path_ids)
        texts: list[str] = []
        tokenized: list[list[str]] = []
        for path in paths:
            metadata = meta_by_id[path_ids[path][0]]
            try:
                body = read_body(self.cfg.root / path)
            except OSError:
                body = ""
            texts.append(body)
            # 卡片正文里没有标题，标题和 description 只存在于 frontmatter。
            # BM25 必须看到它们，否则关键词命中标题的查询会漏。
            header = [
                str(metadata.get("title") or ""),
                str(metadata.get("description") or ""),
            ]
            indexed = "\n".join(part for part in header if part) + "\n" + body
            tokenized.append(tokenize(indexed))

        corpus = (paths, tokenized, texts, path_ids, meta_by_id)
        self._corpus_cache[cache_key] = corpus
        return corpus

    def _bm25(
        self,
        collection: str,
        query_tokens: list[str],
        candidates: int,
        status: str | None,
        doc_type: str | None,
    ) -> list[dict[str, Any]]:
        paths, tokenized, texts, path_ids, meta_by_id = self._bm25_corpus(
            collection, status, doc_type
        )
        if not paths or not any(tokenized):
            return []

        bm25 = BM25Okapi(tokenized)
        scores = bm25.get_scores(query_tokens)

        ranked = sorted(
            range(len(scores)), key=lambda j: float(scores[j]), reverse=True
        )[:candidates]

        results: list[dict[str, Any]] = []
        for j in ranked:
            if float(scores[j]) <= 0:
                continue
            path = paths[j]
            chunk_id = path_ids[path][0]
            results.append(
                {
                    "id": chunk_id,
                    "text": texts[j],
                    "metadata": meta_by_id[chunk_id],
                    "bm25_score": float(scores[j]),
                }
            )
        return results

    def _dense(
        self,
        collection: str,
        query: str,
        candidates: int,
        status: str | None,
        doc_type: str | None,
    ) -> list[dict[str, Any]]:
        response = self.store.query(
            collection,
            self.embedder.embed_query(query),
            candidates,
            build_where(status, doc_type),
        )
        ids = (response.get("ids") or [[]])[0]
        documents = (response.get("documents") or [[]])[0]
        metadatas = (response.get("metadatas") or [[]])[0]
        distances = (response.get("distances") or [[]])[0]

        results: list[dict[str, Any]] = []
        for index, chunk_id in enumerate(ids):
            results.append(
                {
                    "id": chunk_id,
                    "text": documents[index],
                    "metadata": metadatas[index] or {},
                    "distance": float(distances[index]),
                }
            )
        return results

    def search(
        self,
        query: str,
        top_k: int | None = None,
        layer: str = "all",
        status: str | None = None,
        doc_type: str | None = None,
    ) -> list[dict[str, Any]]:
        top_k = top_k or int(self.cfg.get("search.top_k", 8))
        candidates = int(self.cfg.get("search.candidates", 30))
        rrf_k = int(self.cfg.get("search.rrf_k", 60))
        bm25_weight = float(self.cfg.get("search.bm25_weight", 0.4))
        dense_weight = float(self.cfg.get("search.dense_weight", 0.6))

        if layer == "all":
            collections = list(self.cfg.collections.keys())
        else:
            if layer not in self.cfg.collections:
                raise ValueError(f"未知的 layer: {layer}")
            collections = [layer]

        query_tokens = tokenize(query)
        scores: dict[str, float] = defaultdict(float)
        sources: dict[str, set[str]] = defaultdict(set)
        infos: dict[str, dict[str, Any]] = {}

        for collection in collections:
            dense_results = self._dense(
                collection, query, candidates, status, doc_type
            )
            for rank, item in enumerate(dense_results):
                chunk_id = item["id"]
                scores[chunk_id] += dense_weight / (rrf_k + rank + 1)
                sources[chunk_id].add("dense")
                infos.setdefault(chunk_id, item)

            if bm25_weight > 0 and query_tokens:
                bm25_results = self._bm25(
                    collection, query_tokens, candidates, status, doc_type
                )
                for rank, item in enumerate(bm25_results):
                    chunk_id = item["id"]
                    scores[chunk_id] += bm25_weight / (rrf_k + rank + 1)
                    sources[chunk_id].add("bm25")
                    infos.setdefault(chunk_id, item)

        ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        max_per_path = int(self.cfg.get("search.max_per_path", 1))

        results: list[dict[str, Any]] = []
        path_counts: dict[str, int] = defaultdict(int)
        for chunk_id, score in ranked:
            info = infos[chunk_id]
            metadata = info.get("metadata", {}) or {}
            path = metadata.get("path", "")
            if max_per_path > 0 and path_counts[path] >= max_per_path:
                continue
            results.append(
                {
                    "id": chunk_id,
                    "score": round(float(score), 6),
                    "sources": sorted(sources[chunk_id]),
                    "path": path,
                    "title": metadata.get("title", ""),
                    "heading": metadata.get("heading", ""),
                    "layer": metadata.get("layer", ""),
                    "type": metadata.get("type", ""),
                    "status": metadata.get("status", ""),
                    "snippet": make_snippet(info.get("text", "")),
                }
            )
            path_counts[path] += 1
            if len(results) >= top_k:
                break
        return results


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检索记忆库")
    parser.add_argument("query", help="查询文本")
    parser.add_argument("--top", type=int, default=None, help="返回条数")
    parser.add_argument(
        "--layer",
        default="knowledge",
        choices=["all", "knowledge", "memory"],
        help="检索层：knowledge（默认）、memory、all",
    )
    parser.add_argument("--status", default=None, help="按卡片状态过滤：draft/approved")
    parser.add_argument(
        "--type", dest="doc_type", default=None, help="按类型过滤，如 user/project/card"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--config", default=None, help="配置文件路径")
    return parser


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    _utf8_stdout()
    args = build_arg_parser().parse_args()
    cfg = Config.load(args.config)
    searcher = Searcher(cfg)
    results = searcher.search(
        args.query,
        top_k=args.top,
        layer=args.layer,
        status=args.status,
        doc_type=args.doc_type,
    )

    if args.json:
        print(
            json.dumps(
                {"query": args.query, "results": results},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not results:
        print("未找到相关结果。")
        return 0

    print(f"\n查询：{args.query}\n")
    for index, item in enumerate(results, start=1):
        status = f"[{item['status']}] " if item["status"] else ""
        heading = f" › {item['heading']}" if item["heading"] else ""
        print(f"{index}. score={item['score']:.4f}  {status}{item['path']}{heading}")
        print(f"   {item['title']}")
        print(f"   {item['snippet']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
