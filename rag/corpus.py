from __future__ import annotations

import fnmatch
from pathlib import Path

from chunker import parse_frontmatter


def is_excluded(rel_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


def scan_files(root: Path, includes: list[str], excludes: list[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for pattern in includes:
        for path in root.glob(pattern):
            if not path.is_file() or path.suffix.lower() != ".md":
                continue
            rel = path.relative_to(root).as_posix()
            if is_excluded(rel, excludes):
                continue
            found[rel] = path
    return dict(sorted(found.items()))


def read_text(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def read_body(path: Path) -> str:
    """Read a markdown file and return the body without YAML frontmatter."""
    return parse_frontmatter(read_text(path))[1]
