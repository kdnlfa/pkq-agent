"""终端界面层：Rich 输出渲染 + prompt_toolkit 输入。

两个依赖都是可选的。装不上、或没有真实控制台（管道 / IDE 输出面板）时，
自动回退到最朴素的 print/input，功能不受影响。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Rich：输出渲染
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.spinner import Spinner
    from rich.table import Table
    from rich.text import Text

    HAS_RICH = True
except ImportError:  # pragma: no cover
    HAS_RICH = False


class UI:
    """统一的输出入口。有 Rich 就走富文本，没有就退化到 print。"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.has_rich = HAS_RICH
        self.console: Any = None
        if HAS_RICH:
            try:
                self.console = Console()
                # 无控制台时 Rich 仍能工作（只是没颜色），这里不额外判断
            except Exception:
                self.has_rich = False
                self.console = None

    # -- 基础输出 ---------------------------------------------------------

    def print(self, *args: Any, **kwargs: Any) -> None:
        if self.has_rich and self.console:
            self.console.print(*args, **kwargs)
        else:
            print(*args)

    def rule(self, title: str) -> None:
        if self.has_rich and self.console:
            self.console.rule(f"[bold cyan]{title}[/bold cyan]")
        else:
            print(f"\n=== {title} ===\n")

    def banner(self, model: str, provider_url: str = "") -> None:
        if self.has_rich and self.console:
            body = Text()
            body.append("记忆库 agent", style="bold white")
            body.append("  已就绪\n", style="dim")
            body.append("/reset", style="bold yellow")
            body.append(" 清空上下文    ", style="dim")
            body.append("/exit", style="bold yellow")
            body.append(" 退出    ", style="dim")
            body.append("↑↓", style="bold yellow")
            body.append(" 翻历史    ", style="dim")
            body.append("Tab", style="bold yellow")
            body.append(" 补全\n", style="dim")
            body.append("知识层 Cards/   ·   记忆层 Memory/   ·   两层都会查", style="dim")
            self.console.print(Panel(body, border_style="cyan", padding=(0, 2)))
        else:
            print("记忆库 agent 已就绪")
            print("  /reset 清空上下文   /exit 退出   ↑↓ 翻历史   Tab 补全")
            print("  知识层 Cards/ · 记忆层 Memory/ · 两层都会查")

    # -- 检索结果 ---------------------------------------------------------

    def search_table(self, results: list[dict[str, Any]]) -> None:
        """把一次 search_vault 的结果缩成一张小表。"""
        if not results:
            self.print("[yellow]没有命中[/yellow]" if self.has_rich else "没有命中")
            return

        if not (self.has_rich and self.console):
            for i, item in enumerate(results, 1):
                print(f"  {i}. {item['score']:.4f}  {item['path']}")
            return

        table = Table(
            show_header=True,
            header_style="bold magenta",
            box=None,
            pad_edge=False,
            expand=False,
        )
        table.add_column("#", style="dim", width=3, justify="right")
        table.add_column("score", width=8, justify="right")
        table.add_column("文件", style="cyan", overflow="fold")
        table.add_column("状态", width=10)
        for i, item in enumerate(results, 1):
            status = item.get("status") or "—"
            status_style = "green" if status == "approved" else "yellow"
            table.add_row(
                str(i),
                f"{item['score']:.4f}",
                item["path"],
                Text(status, style=status_style),
            )
        self.console.print(table)

    # -- 工具调用过程 -----------------------------------------------------

    def tool_line(self, name: str, args: str, result: str) -> None:
        if self.has_rich and self.console:
            line = Text("  · ")
            line.append(name, style="bold green")
            line.append(f"({args})", style="dim")
            line.append(f" → {result}", style="bright_black")
            self.console.print(line)
        else:
            print(f"  · {name}({args}) → {result}")

    # -- 回答 -------------------------------------------------------------

    def answer(self, markdown_text: str) -> None:
        if self.has_rich and self.console:
            self.console.print()
            self.console.print(
                Panel(
                    Markdown(markdown_text),
                    border_style="bright_black",
                    padding=(0, 1),
                )
            )
        else:
            print()
            print(markdown_text)

    def error(self, message: str) -> None:
        if self.has_rich and self.console:
            self.console.print(
                Panel(f"[red]{message}[/red]", border_style="red", title="[red]出错[/red]")
            )
        else:
            print(f"[出错] {message}")

    # -- 等待动画 ---------------------------------------------------------

    @contextmanager
    def working(self, initial: str = "处理中…") -> Iterator["StatusHandle"]:
        """在等待模型/检索时显示旋转动画，避免长时间黑屏。"""
        handle = StatusHandle(self, initial)
        if not (self.has_rich and self.console):
            print(f"  {initial}")
            yield handle
            return

        spinner = Spinner("dots", text=Text(f" {initial}", style="dim"))
        with Live(spinner, console=self.console, refresh_per_second=10, transient=True) as live:
            handle._live = live
            try:
                yield handle
            finally:
                handle._live = None

    def status_text(self, message: str) -> Text | str:
        if self.has_rich and self.console:
            return Text(f" {message}", style="dim")
        return f"  {message}"


class StatusHandle:
    """在 working() 内部随时更新当前的等待文案。"""

    def __init__(self, ui: UI, initial: str):
        self.ui = ui
        self.message = initial
        self._live: Any = None

    def update(self, message: str) -> None:
        self.message = message
        if self._live is not None:
            self._live.update(Spinner("dots", text=self.ui.status_text(message)))


# ---------------------------------------------------------------------------
# prompt_toolkit：输入
# ---------------------------------------------------------------------------

COMMANDS = ["/index", "/reset", "/exit", "/quit", "/help"]


def make_session(corpus_words: list[str] | None = None) -> Any:
    """建一个带历史与补全的输入会话；不可用时返回 None。"""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.history import FileHistory

        words = COMMANDS + (corpus_words or [])
        completer = WordCompleter(words, ignore_case=True, sentence=True)

        history_path = None
        try:
            history_path = _history_file()
        except Exception:
            history_path = None

        session = PromptSession(
            history=FileHistory(str(history_path)) if history_path else None,
            completer=completer,
            complete_while_typing=True,
        )
        return _InputSession(session, True)
    except Exception:
        return None


def _history_file():
    from pathlib import Path

    p = Path.home() / ".pkq_history"
    return p


class _InputSession:
    def __init__(self, session: Any, has_completion: bool):
        self.session = session
        self.has_completion = has_completion
        self._broken = False

    def ask(self, prompt_text: str = "pkq › ") -> str | None:
        """返回输入文本；EOF/Ctrl+C 返回 None；环境不支持时抛 _NoConsole。"""
        if self._broken:
            raise _NoConsole()

        try:
            from prompt_toolkit.formatted_text import HTML

            return self.session.prompt(HTML(f"<b>{prompt_text.strip()}</b> "))
        except (EOFError, KeyboardInterrupt):
            return None
        except Exception as exc:
            # Windows 无真实控制台时 prompt_toolkit 会抛 NoConsoleScreenBufferError
            if "console" in str(exc).lower() or "terminal" in str(exc).lower():
                self._broken = True
                raise _NoConsole() from exc
            raise


class _NoConsole(Exception):
    """当前环境没有真实控制台。"""


def corpus_words_from_index(store_getter) -> list[str]:
    """从索引里的标题抽词，给补全用。失败就返回空表。"""
    try:
        titles = store_getter()
        words: list[str] = []
        for title in titles:
            if title and len(title) <= 20:
                words.append(title)
        return words
    except Exception:
        return []
