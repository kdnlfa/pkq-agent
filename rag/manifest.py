from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Manifest:
    def __init__(self, path: Path, data: dict[str, Any] | None = None):
        self.path = Path(path)
        self.data: dict[str, Any] = data or {
            "version": 1,
            "config_signature": "",
            "files": {},
        }

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.exists():
            return cls(path)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return cls(path)
        return cls(path, data)

    @property
    def files(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("files", {})

    @property
    def config_signature(self) -> str:
        return str(self.data.get("config_signature", ""))

    @config_signature.setter
    def config_signature(self, value: str) -> None:
        self.data["config_signature"] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
