"""写卡片与改文件：把「能安全落盘」这件事单独放一处。

这里不关心模型说了什么，只关心三件事：
1. 只能落在该落的地方（不许跑出库、不许碰代码和规则）；
2. 只能新建，不许覆盖（写卡）；
3. 卡片的格式对不对、自称的标题和文件名对不对得上。

校验不过就直接拒绝并把问题原文退回去，让模型自己改，而不是写完再救。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from chunker import parse_frontmatter

CARD_DIR = "Cards"
CORE_SECTION = "核心内容"
CORE_MAX_CHARS = 900
DESCRIPTION_WARN_CHARS = 200

WINDOWS_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
LINK_RE = re.compile(r"\[\[([^\]|#]+)")


@dataclass
class WriteResult:
    ok: bool
    message: str
    path: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def safe_filename(title: str, limit: int = 80) -> str:
    """把标题变成一个 Windows 能用的文件名。"""
    name = WINDOWS_BAD_CHARS.sub(" ", title or "").strip()
    name = re.sub(r"\s+", " ", name)
    name = name.rstrip(". ")
    if len(name) > limit:
        name = name[:limit].rstrip(". ")
    return name


def resolve_inside(root: Path, rel_path: str) -> tuple[Path | None, str]:
    """把相对路径解析成库内绝对路径；跑出库就返回 (None, 原因)。"""
    raw = (rel_path or "").strip().strip('"').strip("'")
    if not raw:
        return None, "路径是空的。"
    if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
        return None, f"只接受库内相对路径，不接绝对路径：{raw}"
    target = (root / raw).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None, f"拒绝访问：{raw} 不在库目录内。"
    return target, ""


def _is_card_path(root: Path, target: Path) -> bool:
    return target.suffix.lower() == ".md" and target.is_relative_to(root.resolve() / CARD_DIR)


def _count_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def split_sections(body: str) -> dict[str, str]:
    """按一级标题切正文，返回 {标题名: 正文}。"""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = match.group(1).strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _known_titles(root: Path) -> set[str]:
    titles: set[str] = set()
    for folder in ("Cards", "Memory"):
        base = root / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            titles.add(path.stem)
    return titles


def validate_card(root: Path, title: str, content: str) -> WriteResult:
    """检查一张卡是否符合 card-writer 的规矩。不落盘，只检查。"""
    text = (content or "").strip()
    errors: list[str] = []
    warnings: list[str] = []

    if not text:
        return WriteResult(False, "卡片内容是空的。", errors=["内容是空的"])
    if not text.startswith("---"):
        errors.append("开头缺少 frontmatter（第一行必须是 --- ，然后是 title / description / status 等字段）。")

    meta, body = parse_frontmatter(text)

    for name in ("title", "description", "status"):
        if not str(meta.get(name) or "").strip():
            errors.append(f"frontmatter 缺少必填字段：{name}")

    meta_title = str(meta.get("title") or "").strip()
    if meta_title and title and meta_title != title.strip():
        errors.append(
            f"frontmatter 里的 title（{meta_title}）和本次传入的标题（{title}）不一致，"
            "文件名必须与 title 一致，请统一成一个。"
        )

    status = str(meta.get("status") or "").strip()
    if status and status != "draft":
        errors.append(
            f"新建卡片的 status 必须是 draft，收到的是 {status}。"
            "审核由用户自己做，不要自己标 approved。"
        )

    description = str(meta.get("description") or "").strip()
    if _count_chars(description) > DESCRIPTION_WARN_CHARS:
        warnings.append(
            f"description 有 {_count_chars(description)} 字，太长了。"
            "它的用途是让人不读正文就判断相关性，一两句话就够。"
        )

    sections = split_sections(body)
    core = sections.get(CORE_SECTION, "")
    if not core:
        errors.append(f"正文缺少「# {CORE_SECTION}」这一节，它是唯一进检索索引的部分，必须有。")
    elif _count_chars(core) > CORE_MAX_CHARS:
        errors.append(
            f"「{CORE_SECTION}」有 {_count_chars(core)} 字，超过 {CORE_MAX_CHARS} 字上限。"
            "把展开的论证、例子、细节挪到「原文摘录」或「背景与来源」，不要直接删内容。"
        )

    known = _known_titles(root)
    linked = {m.group(1).strip() for m in LINK_RE.finditer(text)}
    missing = sorted(t for t in linked if t and t not in known and t != title.strip())
    if missing:
        warnings.append(
            "以下 [[链接]] 找不到对应文件（可能是老卡里就没有，或是笔误）："
            + "、".join(missing)
        )

    if errors:
        return WriteResult(False, "卡片没通过检查，未写入。", errors=errors, warnings=warnings)
    return WriteResult(True, "检查通过。", warnings=warnings)


def write_card(
    root: Path, title: str, content: str, subdir: str = ""
) -> WriteResult:
    """在 Cards/ 下新建一张卡。同名文件已存在就拒绝，绝不覆盖。"""
    title_text = (title or "").strip()
    if not title_text:
        return WriteResult(False, "没有给标题。", errors=["title 是空的"])

    name = safe_filename(title_text)
    if name != title_text or PureWindowsPath(f"{name}.md").is_reserved():
        return WriteResult(False, "标题必须能原样用作 Windows 文件名。", errors=["标题无法生成文件名"])

    folder = (CARD_DIR + "/" + subdir.strip().strip("/")) if subdir.strip() else CARD_DIR
    rel_path = f"{folder}/{name}.md"

    target, reason = resolve_inside(root, rel_path)
    if target is None:
        return WriteResult(False, reason, errors=[reason])
    if not _is_card_path(root, target):
        return WriteResult(False, "只能在 Cards/ 下新建卡片。", errors=["路径超出 Cards/"])
    rel_path = target.relative_to(root.resolve()).as_posix()

    if target.exists():
        return WriteResult(
            False,
            f"已经有一张同名的卡：{rel_path}。不覆盖已有文件；"
            "要改它请用 edit_file，要新建请换个标题。",
            path=rel_path,
            errors=["同名文件已存在"],
        )

    check = validate_card(root, title_text, content)
    if not check.ok:
        return WriteResult(False, check.message, path=rel_path, errors=check.errors, warnings=check.warnings)

    target.parent.mkdir(parents=True, exist_ok=True)
    text = content if content.endswith("\n") else content + "\n"
    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        return WriteResult(False, f"已经有一张同名的卡：{rel_path}。", path=rel_path)

    message = f"已写入 {rel_path}"
    if check.warnings:
        message += "\n（另有提醒：\n- " + "\n- ".join(check.warnings) + "\n）"
    return WriteResult(True, message, path=rel_path, warnings=check.warnings)


def edit_file(
    root: Path, path: str, old_text: str, new_text: str, replace_all: bool = False
) -> WriteResult:
    """按原文精确替换。找不到或多处命中都拒绝，不猜。"""
    target, reason = resolve_inside(root, path)
    if target is None:
        return WriteResult(False, reason, errors=[reason])

    rel = target.relative_to(root.resolve()).as_posix()
    if not _is_card_path(root, target):
        return WriteResult(False, f"只能编辑 Cards/ 下的 Markdown 卡片：{rel}", errors=["路径超出卡片范围"])
    if not target.is_file():
        return WriteResult(False, f"文件不存在：{rel}", errors=["文件不存在"])
    if old_text == "":
        return WriteResult(False, "old_text 是空的；要替换什么就把它原样抄进来。", errors=["old_text 为空"])

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return WriteResult(False, f"卡片不是 UTF-8 编码：{rel}", path=rel)
    hits = text.count(old_text)
    if hits == 0:
        return WriteResult(
            False,
            f"在 {rel} 里找不到这段原文，一个字都不能差（含空格与换行）。"
            "请先用 read_note 看一遍，再照抄要替换的那一段。",
            path=rel,
            errors=["未匹配"],
        )
    if hits > 1 and not replace_all:
        return WriteResult(
            False,
            f"这段原文在 {rel} 里出现了 {hits} 次，不知道该改哪一处。"
            "请把它前后多带几行，凑成唯一的一段；确实要全改就传 replace_all=true。",
            path=rel,
            errors=["多处匹配"],
        )

    updated = text.replace(old_text, new_text, -1 if replace_all else 1)
    old_metadata, _ = parse_frontmatter(text.removeprefix("\ufeff"))
    new_metadata, _ = parse_frontmatter(updated.removeprefix("\ufeff"))
    if old_metadata.get("status") != new_metadata.get("status"):
        return WriteResult(
            False,
            "这次修改动了 status。审核状态只能由用户改。",
            path=rel,
            errors=["擅自改审核状态"],
        )

    target.write_text(updated, encoding="utf-8")
    return WriteResult(True, f"已修改 {rel}（替换 {hits if replace_all else 1} 处）", path=rel)
