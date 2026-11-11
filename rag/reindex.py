"""Adapt the shared index updater for the interactive /index command."""

from __future__ import annotations

from config import Config
from ingest import update_index


def reindex(cfg: Config) -> tuple[bool, str]:
    """返回索引是否更新，以及给终端显示的一行摘要。"""
    result = update_index(cfg)
    if not result.changed_files and not result.removed_files:
        return False, f"索引已是最新（{result.elapsed:.1f}s）"
    return True, (
        f"索引已更新：新增/变更 {result.changed_files} 个文件，"
        f"删除 {result.removed_files} 个（{result.elapsed:.1f}s）"
    )
