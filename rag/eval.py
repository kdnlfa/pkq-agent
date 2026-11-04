from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from config import Config
from search import Searcher


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(description="评测记忆库检索质量")
    parser.add_argument(
        "--queries",
        default=str(Path(__file__).resolve().parent / "eval_queries.yaml"),
        help="评测查询文件",
    )
    parser.add_argument("--top", type=int, default=5, help="recall@k 的 k")
    parser.add_argument(
        "--layer",
        default="knowledge",
        choices=["all", "knowledge", "memory"],
        help="评测的检索层",
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    with open(args.queries, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    queries = data.get("queries", [])
    if not queries:
        print("评测文件里没有 queries。")
        return 1

    cfg = Config.load(args.config)
    searcher = Searcher(cfg)

    hits = 0
    reciprocal_ranks = 0.0
    print(f"评测 {len(queries)} 条查询，recall@{args.top}\n")
    print(f"{'查询':<34} {'命中':<6} {'排名':<6} 期望文件")
    print("-" * 90)

    for item in queries:
        query = item["query"]
        expected = item.get("expected", [])
        # 每条查询可以单独指定检索层，不写则用命令行 --layer
        layer = item.get("layer", args.layer)
        results = searcher.search(query, top_k=args.top, layer=layer)
        paths = [r["path"] for r in results]

        rank = None
        for path in paths:
            if path in expected:
                rank = paths.index(path) + 1
                break

        if rank:
            hits += 1
            reciprocal_ranks += 1.0 / rank
        hit_label = "✓" if rank else "✗"
        rank_label = str(rank) if rank else "-"
        expected_label = expected[0] if expected else ""
        layer_tag = "" if layer == args.layer else f"  [{layer}]"
        print(f"{query:<34} {hit_label:<6} {rank_label:<6} {expected_label}{layer_tag}")

        if args.verbose:
            for i, result in enumerate(results, start=1):
                print(f"    {i}. {result['path']}  ({result['score']:.4f})")

    count = len(queries)
    print("-" * 90)
    print(f"recall@{args.top}: {hits}/{count} = {hits / count:.2%}")
    print(f"MRR: {reciprocal_ranks / count:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
