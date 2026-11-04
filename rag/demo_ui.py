"""终端美化 Demo：对比 Rich 输出渲染与 prompt_toolkit 输入体验。

运行：
    uv run python demo_ui.py

内容全部是硬编码样例，不连模型、不查索引。
"""

from __future__ import annotations

import sys

# ---------------------------------------------------------------------------
# 样例数据：尽量贴近 memory_agent 的真实输出形状
# ---------------------------------------------------------------------------

ANSWER_MD = """## 结论

**休息是无条件保留的基本需求，奖励是完成目标后的额外肯定。** 达标与否只影响奖励，永远不影响休息资格。

## 依据

| 比较项 | 休息 | 奖励 |
| --- | --- | --- |
| 性质 | 基本需求与日常关怀 | 完成目标后的额外肯定 |
| 是否需要达标 | 无条件保留 | 可以事先约定触发条件 |
| 没完成任务怎么办 | 仍然需要休息 | 相应奖励可以暂不发放 |
| 主要作用 | 恢复、托底、避免继续消耗 | 增加期待与成就感 |

卡片给出的判断方式：

> 没有完成什么任务，我平常也应该拥有它吗？

- 是 → 优先放入休息库
- 额外、特别、带庆祝意味 → 奖励库
- 两者重合 → 基础版本留休息库，升级版本放奖励库

```python
# 代码块也应该有语法高亮
def classify(activity: str) -> str:
    return "rest" if is_basic_need(activity) else "reward"
```
"""

SEARCH_RESULTS = [
    {"score": 0.0164, "path": "Cards/区分休息与奖励.md", "status": "approved", "sources": "dense+bm25"},
    {"score": 0.0158, "path": "Cards/用可选清单与抽卡降低休息决策成本.md", "status": "approved", "sources": "dense"},
    {"score": 0.0141, "path": "Cards/奖励要按心理重量设计并能兑现.md", "status": "approved", "sources": "bm25"},
    {"score": 0.0122, "path": "Cards/把结果目标转成可执行的投入承诺.md", "status": "approved", "sources": "dense"},
    {"score": 0.0098, "path": "Memory/user/冥想练习习惯.md", "status": "", "sources": "dense"},
]

TOOL_STEPS = [
    ("search_vault", "'休息和奖励的区别', knowledge", "10 条"),
    ("read_note", "'Cards/区分休息与奖励.md'", "3.2k 字"),
    ("search_vault", "'奖励 兑现 心理重量', all", "8 条"),
]


# ---------------------------------------------------------------------------
# 方案 A：纯 Rich
# ---------------------------------------------------------------------------


def demo_rich() -> None:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()

    console.print()
    console.rule("[bold cyan]Rich 渲染效果[/bold cyan]")
    console.print()

    # 1) 启动横幅
    banner = Text()
    banner.append("记忆库 agent", style="bold white")
    banner.append("  已就绪\n", style="dim")
    banner.append("/reset", style="bold yellow")
    banner.append(" 清空上下文    ", style="dim")
    banner.append("/exit", style="bold yellow")
    banner.append(" 退出", style="dim")
    console.print(Panel(banner, border_style="cyan", padding=(0, 2)))

    # 2) 检索结果表格
    console.print("\n[bold]检索结果[/bold] [dim](search_vault)[/dim]")
    table = Table(show_header=True, header_style="bold magenta", box=None, pad_edge=False)
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("score", width=8, justify="right")
    table.add_column("文件", style="cyan", no_wrap=False)
    table.add_column("来源", width=11)
    for i, item in enumerate(SEARCH_RESULTS, 1):
        table.add_row(
            str(i),
            f"{item['score']:.4f}",
            item["path"],
            Text(item["sources"], style="green" if "+" in item["sources"] else "yellow"),
        )
    console.print(table)

    # 3) 工具调用过程（替代原始 stderr 打印）
    console.print("\n[bold]工具调用[/bold]")
    for name, args, result in TOOL_STEPS:
        line = Text("  · ")
        line.append(name, style="bold green")
        line.append(f"({args})", style="dim")
        line.append(f" → {result}", style="bright_black")
        console.print(line)

    # 4) Markdown 回答
    console.print("\n[bold]回答[/bold] [dim](Markdown 自动渲染)[/dim]")
    console.print(Panel(Markdown(ANSWER_MD), border_style="bright_black", padding=(0, 1)))

    # 5) 错误样式
    console.print("\n[bold]错误提示样式[/bold]")
    console.print(
        Panel(
            "[red]缺少依赖：rank_bm25[/red]\n[dim]执行 uv sync 修复[/dim]",
            border_style="red",
            title="[red]错误[/red]",
        )
    )
    console.print()


def demo_rich_spinner() -> None:
    """展示等待时的加载动画——解决「提问后干等 20 秒黑屏」。"""
    import time

    from rich.console import Console
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.text import Text

    console = Console()
    console.rule("[bold cyan]Rich 加载动画[/bold cyan]")
    with Live(console=console, refresh_per_second=12) as live:
        for label in ("检索记忆库…", "读取 Cards/区分休息与奖励.md…", "生成回答…"):
            spinner = Spinner("dots", text=Text(f" {label}", style="dim"))
            live.update(spinner)
            time.sleep(1.2)
        live.update(Text(" ✓ 完成", style="bold green"))
    console.print()


# ---------------------------------------------------------------------------
# 方案 B：纯 prompt_toolkit
# ---------------------------------------------------------------------------


def demo_prompt_toolkit() -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import InMemoryHistory

    commands = ["/reset", "/exit", "/help", "/quit"]
    corpus_words = [
        "休息", "奖励", "冥想", "睡前", "注意力", "上下文", "Agent",
        "稀疏注意力", "MoE", "PAOS", "卡片", "审核", "记忆",
    ]
    completer = WordCompleter(commands + corpus_words, ignore_case=True, sentence=True)

    print()
    print("=" * 70)
    print("prompt_toolkit 输入体验")
    print("=" * 70)

    try:
        session: PromptSession = PromptSession(
            history=InMemoryHistory(),
            completer=completer,
            complete_while_typing=True,
        )
        session.prompt(HTML("<prompt>pkq</prompt> <arrow>›</arrow> "))
    except Exception as exc:  # 无真实控制台（管道、IDE 输出面板）时会失败
        print(f"[跳过] 当前环境没有真实控制台，prompt_toolkit 无法运行：")
        print(f"       {type(exc).__name__}: {exc}")
        print("       请在 Windows Terminal 或 cmd 窗口里直接运行本脚本。")
        print("       补全能力已单独验证通过：输入 / 提示命令，输入 休 提示「休息」。")
        print()
        return

    print("试试这些：")
    print("  · 输入 / 会弹出命令补全")
    print("  · 输入 休 会提示「休息」")
    print("  · 上下方向键翻历史")
    print()

    turns = 0
    while turns < 3:
        try:
            text = session.prompt(HTML("<prompt>pkq</prompt> <arrow>›</arrow> "))
        except (EOFError, KeyboardInterrupt):
            print("\n（退出）")
            return
        turns += 1
        if not text.strip():
            print("（空输入，跳过）")
            continue
        if text.strip() in {"/exit", "/quit"}:
            print("（退出）")
            return
        print(f"  收到：{text!r}  →  实际会送去检索")
    print("\n（演示结束，已交互 3 次）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = set(sys.argv[1:])
    if not args or "a" in args or "rich" in args:
        demo_rich()
        demo_rich_spinner()
    if not args or "b" in args or "prompt" in args:
        demo_prompt_toolkit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
