from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from chunker import chunk_document
from config import Config
from corpus import read_text, redact, scan_files
from embeddings import create_embedder
from manifest import Manifest
from store import VectorStore


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def config_signature(cfg: Config) -> str:
    payload = {
        "chunking": cfg.get("chunking", {}),
        "embedding": {
            "provider": cfg.get("embedding.provider"),
            "model": cfg.get("embedding.model"),
            "query_prefix": cfg.get("embedding.query_prefix"),
            "document_prefix": cfg.get("embedding.document_prefix"),
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为记忆库建立/更新 RAG 索引")
    parser.add_argument("--config", help="配置文件路径", default=None)
    parser.add_argument("--rebuild", action="store_true", help="清空并全量重建索引")
    parser.add_argument("--quiet", action="store_true", help="只输出汇总")
    return parser


@dataclass
class IndexUpdate:
    total_files: int = 0
    changed_files: int = 0
    removed_files: int = 0
    total_chunks: int = 0
    secret_files: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0


def update_index(
    cfg: Config, rebuild: bool = False, report: Callable[[str], None] | None = None
) -> IndexUpdate:
    started = time.time()
    result = IndexUpdate()
    signature = config_signature(cfg)
    manifest = Manifest.load(cfg.manifest_path)

    rebuild_all = rebuild or manifest.config_signature not in ("", signature)
    if rebuild_all and report:
        report("[ingest] 执行全量重建")
    manifest.config_signature = signature

    patterns = [re.compile(p) for p in cfg.secret_patterns]
    store = VectorStore(cfg.store_path)
    embedder = None

    for collection, spec in cfg.collections.items():
        includes = list(spec.get("includes", []))
        excludes = cfg.global_excludes + list(spec.get("excludes", []))
        found = scan_files(cfg.root, includes, excludes)
        result.total_files += len(found)

        if rebuild_all:
            store.collection(collection, reset=True)
            for key in list(manifest.files):
                if key.startswith(f"{collection}::"):
                    del manifest.files[key]

        existing = {
            key: entry
            for key, entry in manifest.files.items()
            if key.startswith(f"{collection}::")
        }

        for rel, path in found.items():
            key = f"{collection}::{rel}"
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            entry = manifest.files.get(key)
            if entry and entry.get("sha256") == digest:
                continue

            text = read_text(path)
            text, secret_count = redact(text, patterns)
            if secret_count:
                result.secret_files.append(f"{rel}（{secret_count} 处）")

            chunks = chunk_document(
                rel_path=rel,
                text=text,
                collection=collection,
                max_chars=int(cfg.get("chunking.max_chars", 800)),
                min_chars=int(cfg.get("chunking.min_chars", 40)),
                overlap=int(cfg.get("chunking.overlap", 80)),
                mode=str(cfg.get("chunking.mode", "section")),
                embed_max_chars=int(cfg.get("chunking.embed_max_chars", 450)),
            )

            vectors = []
            if chunks:
                if embedder is None:
                    if report:
                        report(f"[ingest] 加载 embedding 模型：{cfg.get('embedding.model')}")
                    embedder = create_embedder(cfg)
                vectors = embedder.embed_documents([c.text for c in chunks])
            store.replace_path(collection, rel, chunks, vectors)

            manifest.files[key] = {
                "path": rel,
                "collection": collection,
                "sha256": digest,
                "mtime": path.stat().st_mtime,
                "chunks": [c.id for c in chunks],
            }
            result.changed_files += 1
            result.total_chunks += len(chunks)
            if report:
                report(f"  + {rel}  ({len(chunks)} chunks)")

        for key, entry in list(existing.items()):
            rel = entry["path"]
            if rel not in found:
                store.delete_path(collection, rel)
                del manifest.files[key]
                result.removed_files += 1
                if report:
                    report(f"  - {rel}  (removed)")

    manifest.save()
    result.counts = {name: store.count(name) for name in cfg.collections}
    result.elapsed = time.time() - started
    return result


def main() -> int:
    _utf8_stdout()
    args = build_arg_parser().parse_args()
    cfg = Config.load(args.config)
    result = update_index(cfg, rebuild=args.rebuild, report=None if args.quiet else print)
    print("\n[ingest] 完成")
    print(f"  语料文件：{result.total_files}")
    print(f"  变更文件：{result.changed_files}")
    print(f"  删除文件：{result.removed_files}")
    print(f"  新增 chunks：{result.total_chunks}")
    counts = " / ".join(f"{count} {name}" for name, count in result.counts.items())
    print(f"  索引总量：{counts}")
    print(f"  耗时：{result.elapsed:.1f}s")
    if result.secret_files:
        print("\n  [安全] 以下文件的密钥已脱敏，建议轮换：")
        for item in sorted(set(result.secret_files)):
            print(f"    - {item}")
    if result.changed_files == 0 and result.removed_files == 0:
        print("  索引已是最新。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
