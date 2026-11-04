from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

RAG_DIR = Path(__file__).resolve().parent
VAULT_ROOT = RAG_DIR.parent
DEFAULT_CONFIG_PATH = RAG_DIR / "config.yaml"


class Config:
    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path
        self.root = VAULT_ROOT

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data, p)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def resolve(self, value: str | Path) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    @property
    def collections(self) -> dict[str, dict[str, list[str]]]:
        return self.data.get("collections", {})

    @property
    def global_excludes(self) -> list[str]:
        return list(self.get("corpus.exclude", []))

    @property
    def secret_patterns(self) -> list[str]:
        return list(self.get("corpus.secret_patterns", []))

    @property
    def store_path(self) -> Path:
        return self.resolve(self.get("store.path", "rag/.index/chroma"))

    @property
    def manifest_path(self) -> Path:
        return self.resolve("rag/.index/manifest.json")
