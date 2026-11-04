from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import frontmatter
except ImportError:  # pragma: no cover - dependency is declared in pyproject
    frontmatter = None

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass
class Chunk:
    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (metadata, body). Malformed frontmatter falls back to raw text."""
    if frontmatter is None:
        return {}, text
    try:
        post = frontmatter.loads(text)
        return dict(post.metadata or {}), post.content
    except Exception:
        return {}, text


def _split_sections(body: str) -> list[dict[str, Any]]:
    """Split markdown by headings, never inside code fences.

    Returns sections with the heading path (e.g. "核心内容 > 子标题") and the
    body text *without* the heading line itself.
    """
    lines = body.splitlines(keepends=True)
    sections: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    current: dict[str, Any] = {"path": "", "body_lines": []}
    in_fence = False
    fence_marker = ""

    for line in lines:
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False

        heading_match = None if in_fence else HEADING_RE.match(line)
        if heading_match:
            text = "".join(current["body_lines"])
            if text.strip():
                sections.append({"path": current["path"], "body": text})
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current = {
                "path": " > ".join(t for _, t in stack),
                "body_lines": [],
            }
        else:
            current["body_lines"].append(line)

    text = "".join(current["body_lines"])
    if text.strip():
        sections.append({"path": current["path"], "body": text})
    return sections


def _iter_blocks(text: str) -> list[str]:
    """Split text into paragraphs while keeping code fences intact."""
    blocks: list[str] = []
    buf: list[str] = []
    in_fence = False
    fence_marker = ""

    for line in text.splitlines(keepends=True):
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False

        if not in_fence and not line.strip():
            if buf:
                blocks.append("".join(buf))
                buf = []
        else:
            buf.append(line)

    if buf:
        blocks.append("".join(buf))
    return blocks


def _hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        pieces.append(text[start:end])
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return pieces


def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in _iter_blocks(text):
        if len(current) + len(block) + 2 <= max_chars:
            current = block if not current else current + "\n\n" + block
            continue
        if current:
            chunks.append(current)
        if len(block) <= max_chars:
            current = block
        else:
            chunks.extend(_hard_split(block, max_chars, overlap))
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _chunk_id(collection: str, rel_path: str, index: int) -> str:
    raw = f"{collection}|{rel_path}|{index}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:20]


def _base_metadata(
    rel_path: str,
    title: str,
    heading: str,
    collection: str,
    doc_type: str,
    status: str,
    description: str,
    updated: str,
    related: Any,
    index: int,
) -> dict[str, Any]:
    return {
        "path": rel_path,
        "title": title,
        "heading": heading,
        "layer": collection,
        "type": doc_type,
        "status": status,
        "description": description,
        "updated": updated,
        "related": _as_str(related),
        "chunk_index": index,
    }


def _card_key_text(
    title: str, description: str, body: str, max_chars: int
) -> str:
    """标题 + description + 核心内容，作为一张卡的唯一嵌入文本。"""
    sections = _split_sections(body)
    core = ""
    for section in sections:
        if "核心内容" in section["path"]:
            core = section["body"].strip()
            break
    if not core:
        for section in sections:
            if section["body"].strip():
                core = section["body"].strip()
                break
    if not core:
        core = body.strip()

    parts = [f"# {title}"]
    if description:
        parts.append(description)
    if core:
        parts.append(core)

    text = "\n\n".join(parts).strip()
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars].rstrip()
    boundary = truncated.rfind("\n\n")
    if boundary > max_chars * 0.6:
        truncated = truncated[:boundary].rstrip()
    return truncated


def chunk_document(
    rel_path: str,
    text: str,
    collection: str,
    max_chars: int = 800,
    min_chars: int = 40,
    overlap: int = 80,
    mode: str = "section",
    embed_max_chars: int = 450,
) -> list[Chunk]:
    metadata, body = parse_frontmatter(text)

    title = _as_str(metadata.get("title")).strip() or Path(rel_path).stem
    description = _as_str(metadata.get("description")).strip()
    status = _as_str(metadata.get("status")).strip()
    doc_type = _as_str(metadata.get("type")).strip()
    updated = _as_str(metadata.get("updated") or metadata.get("date")).strip()
    related = metadata.get("related")

    if not doc_type:
        doc_type = "card" if collection == "knowledge" and rel_path.startswith("Cards/") else "note"
        if collection == "memory":
            doc_type = "memory"

    mode = (mode or "section").strip().lower()
    if mode == "card":
        # 一张卡一个 chunk：索引里只放关键信息，全文由 BM25 和 Agent 回读文件
        key_text = _card_key_text(title, description, body, embed_max_chars)
        if len(key_text) < min_chars:
            return []
        return [
            Chunk(
                id=_chunk_id(collection, rel_path, 0),
                text=key_text,
                metadata=_base_metadata(
                    rel_path,
                    title,
                    "关键信息",
                    collection,
                    doc_type,
                    status,
                    description,
                    updated,
                    related,
                    0,
                ),
            )
        ]

    if mode != "section":
        raise ValueError(f"未知的 chunking.mode: {mode}")

    sections = _split_sections(body)
    if not sections:
        sections = [{"path": "", "body": body}]

    chunks: list[Chunk] = []
    index = 0
    for section in sections:
        section_body = section["body"].strip()
        if len(section_body) < min_chars:
            continue

        header_parts = [f"# {title}"]
        if section["path"]:
            header_parts.append(f"## {section['path']}")
        header = "\n\n".join(header_parts)

        for piece in _split_long(section_body, max_chars, overlap):
            piece = piece.strip()
            if not piece:
                continue
            chunk_text = f"{header}\n\n{piece}"
            chunks.append(
                Chunk(
                    id=_chunk_id(collection, rel_path, index),
                    text=chunk_text,
                    metadata=_base_metadata(
                        rel_path,
                        title,
                        section["path"],
                        collection,
                        doc_type,
                        status,
                        description,
                        updated,
                        related,
                        index,
                    ),
                )
            )
            index += 1
    return chunks
