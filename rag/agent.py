from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_RAG_DIR = Path(__file__).resolve().parent


def _ensure_venv() -> None:
    """如果当前解释器不是 rag/.venv 里的那个，就用它重新启动自己。

    这样无论用 python、py、还是双击，都能拿到正确依赖。
    """
    if os.environ.get("MEMORY_AGENT_BOOTSTRAPPED") == "1":
        return

    if os.name == "nt":
        venv_python = _RAG_DIR / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = _RAG_DIR / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return

    try:
        if Path(sys.executable).resolve() == venv_python.resolve():
            return
    except OSError:
        pass

    env = dict(os.environ, MEMORY_AGENT_BOOTSTRAPPED="1")
    raise SystemExit(
        subprocess.call(
            [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]], env=env
        )
    )


_ensure_venv()

import argparse  # noqa: E402
import json  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from typing import Any, Iterator  # noqa: E402

# 保证从任何工作目录、任何方式启动都能 import 同目录模块
if str(_RAG_DIR) not in sys.path:
    sys.path.insert(0, str(_RAG_DIR))

try:
    from config import Config
    from search import Searcher, _utf8_stdout
    from ui import UI, StatusHandle, make_session, corpus_words_from_index, _NoConsole
except ModuleNotFoundError as exc:  # pragma: no cover - 环境问题引导
    missing = getattr(exc, "name", "依赖")
    sys.exit(
        f"缺少依赖：{missing}\n\n"
        f"rag/.venv 可能没建好。执行一次：\n"
        f'  uv sync --directory "{_RAG_DIR}"\n'
    )

MAX_TURNS = 6
READ_MAX_CHARS = 8000

SYSTEM_PROMPT = """你是这个 Obsidian 记忆库的检索助手。

库分两层：
- knowledge：知识卡片（`Cards/`）
- memory：关于用户的长期记忆（偏好、项目、反馈、外部资源指针）

工作规则：
1. 检索返回的 snippet 只是关键信息摘要，不是全文。需要细节、论证、原文摘录或 [[链接]] 时，必须用 read_note 读取整份文件。
2. 引用卡片时保留其 status 标记（当前绝大多数为 approved）。库里没有的内容就说“库中没有足够相关的记录”，不要用常识补全。
3. 回答用中文，先给结论再给依据，并说明信息来自哪些文件路径。"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_vault",
            "description": "在记忆库里做混合检索（向量 + BM25）。返回命中的文件路径、标题、状态和关键信息摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索语句，用自然语言描述要找什么"},
                    "layer": {
                        "type": "string",
                        "enum": ["all", "knowledge", "memory"],
                        "description": "检索层。找知识点用 knowledge，找用户偏好/项目/反馈用 memory，不确定用 all",
                    },
                    "top_k": {"type": "integer", "description": "返回条数，默认 8"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "按路径读取库中某个文件的完整正文（含 frontmatter 与 [[链接]]）。路径取自上一步 search_vault 的结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对库根目录的路径，例如 Cards/区分休息与奖励.md"}
                },
                "required": ["path"],
            },
        },
    },
]


def resolve_provider(
    provider: str, model: str | None, base_url: str | None
) -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)。环境变量优先，其次读 pi 的 models.json。"""
    api_key = os.environ.get("AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    env_base = os.environ.get("AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
    env_model = os.environ.get("AGENT_MODEL") or ""

    pi_base, pi_key, pi_model = "", "", ""
    pi_path = Path.home() / ".pi" / "agent" / "models.json"
    if pi_path.exists():
        try:
            data = json.loads(pi_path.read_text(encoding="utf-8"))
            conf = (data.get("providers") or {}).get(provider) or {}
            pi_base = str(conf.get("baseUrl") or "")
            pi_key = str(conf.get("apiKey") or "")
            models = conf.get("models") or []
            if models:
                first = models[0]
                pi_model = str(first.get("id") if isinstance(first, dict) else first)
        except (json.JSONDecodeError, OSError):
            pass

    settings_path = Path.home() / ".pi" / "agent" / "settings.json"
    if not pi_model and settings_path.exists():
        try:
            conf = json.loads(settings_path.read_text(encoding="utf-8"))
            if conf.get("defaultProvider") == provider:
                pi_model = str(conf.get("defaultModel") or "")
        except (json.JSONDecodeError, OSError):
            pass

    final_base = base_url or env_base or pi_base or "https://api.openai.com/v1"
    final_key = api_key or pi_key
    final_model = model or env_model or pi_model
    if not final_key:
        raise RuntimeError(
            "没有可用的 API key。设置环境变量 AGENT_API_KEY，"
            f"或在 ~/.pi/agent/models.json 里配置 provider「{provider}」。"
        )
    if not final_model:
        raise RuntimeError("没有指定模型，用 --model 指定。")
    return final_base, final_key, final_model


class VaultAgent:
    def __init__(
        self,
        cfg: Config,
        base_url: str,
        api_key: str,
        model: str,
        verbose: bool = False,
        ui: UI | None = None,
    ):
        from openai import OpenAI

        self.cfg = cfg
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.verbose = verbose
        self.searcher = Searcher(cfg)
        self.trace: list[str] = []
        self.ui = ui or UI(verbose=verbose)
        self._status: StatusHandle | None = None

    def _log(self, message: str) -> None:
        self.trace.append(message)
        if self._status is not None:
            self._status.update(message)
        if self.verbose:
            # 正在转动画时不打行，避免与 Live 输出交火
            if self._status is None:
                self.ui.print(f"  [dim]· {message}[/dim]")

    def _tool_search(self, query: str, layer: str = "all", top_k: int = 8) -> str:
        results = self.searcher.search(query, top_k=top_k, layer=layer)
        self._log(f"search_vault {query!r}, {layer} → {len(results)} 条")
        if self.verbose:
            self.ui.search_table(results)
        if not results:
            return "没有命中任何文件。可以换关键词，或用 layer=all 再试。"
        lines = []
        for item in results:
            status = f"[{item['status']}] " if item["status"] else ""
            lines.append(
                f"- path: {item['path']}\n"
                f"  title: {item['title']}\n"
                f"  status: {status or '未知'}\n"
                f"  snippet: {item['snippet']}"
            )
        return "\n".join(lines)

    def _tool_read(self, path: str) -> str:
        self._log(f"read_note {path}")
        target = (self.cfg.root / path).resolve()
        try:
            target.relative_to(self.cfg.root.resolve())
        except ValueError:
            return f"拒绝访问：{path} 不在库目录内。"
        if not target.is_file() or target.suffix.lower() != ".md":
            return f"文件不存在或不是 markdown：{path}"
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > READ_MAX_CHARS:
            return text[:READ_MAX_CHARS] + f"\n\n…（已截断，全文 {len(text)} 字）"
        return text

    def _dispatch(self, name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return f"参数不是合法 JSON：{raw_args}"
        if name == "search_vault":
            return self._tool_search(
                query=str(args.get("query", "")),
                layer=str(args.get("layer", "all")),
                top_k=int(args.get("top_k", 8) or 8),
            )
        if name == "read_note":
            return self._tool_read(str(args.get("path", "")))
        return f"未知工具：{name}"

    def run(
        self, question: str, history: list[dict[str, Any]] | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        """跑一轮问答，返回 (答案, 可供下一轮使用的历史)。

        历史里只保留 user/assistant 文本，丢掉工具调用细节，避免上下文膨胀。
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *(history or []),
            {"role": "user", "content": question},
        ]
        for _ in range(MAX_TURNS):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.3,
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                answer = message.content or "(模型没有返回内容)"
                new_history = list(history or []) + [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": answer},
                ]
                return answer, new_history
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [tc.model_dump() for tc in tool_calls],
                }
            )
            for call in tool_calls:
                result = self._dispatch(call.function.name, call.function.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    }
                )
        return "(已达到最大工具调用轮数，未得出结论)", list(history or [])


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="最小检索 agent：连接记忆库 RAG")
    parser.add_argument("question", nargs="?", help="问题；省略则进入交互模式")
    parser.add_argument("--provider", default="open-design", help="读取 ~/.pi/agent/models.json 的 provider")
    parser.add_argument("--model", default=None, help="模型名；省略则用 pi 的默认模型")
    parser.add_argument("--base-url", default=None, help="覆盖 base URL")
    parser.add_argument("--config", default=None, help="RAG 配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="在 stderr 打印工具调用过程")
    return parser


def main() -> int:
    _utf8_stdout()
    args = build_arg_parser().parse_args()

    cfg = Config.load(args.config)
    base_url, api_key, model = resolve_provider(args.provider, args.model, args.base_url)
    ui = UI(verbose=args.verbose)
    agent = VaultAgent(cfg, base_url, api_key, model, verbose=args.verbose, ui=ui)

    # 单次提问：不问就不显示横幅
    if args.question:
        try:
            with ui.working("检索记忆库…") as status:
                agent._status = status
                try:
                    answer, _ = agent.run(args.question)
                finally:
                    agent._status = None
        except Exception as exc:
            ui.error(f"{type(exc).__name__}: {exc}")
            return 1
        ui.answer(answer)
        return 0

    # 交互模式
    ui.banner(model, base_url)
    if args.verbose:
        ui.print(f"  [dim]model={model}  {base_url}[/dim]")

    # 补全词：命令 + 索引里的卡片标题
    def _titles() -> list[str]:
        data = agent.searcher.store.get_all("knowledge")
        metas = data.get("metadatas") or []
        return [str((m or {}).get("title") or "") for m in metas]

    session = make_session(corpus_words_from_index(_titles))
    use_pt = session is not None

    history: list[dict[str, Any]] = []
    while True:
        question: str | None
        if use_pt:
            try:
                question = session.ask("pkq ›")
                if question is None:
                    ui.print("（退出）")
                    return 0
                question = question.strip()
            except _NoConsole:
                ui.print("[yellow]当前终端不支持行编辑，已回退到普通输入[/yellow]")
                use_pt = False
                continue
        else:
            try:
                question = input("\npkq › ").strip()
            except (EOFError, KeyboardInterrupt):
                ui.print()
                return 0

        if not question:
            ui.print("（退出）")
            return 0
        if question.lower() in {"/exit", "/quit", "exit", "quit"}:
            ui.print("（退出）")
            return 0
        if question.lower() == "/reset":
            history = []
            ui.print("[dim]（已清空上下文，下一轮重新开始）[/dim]")
            continue
        if question.lower() == "/help":
            ui.print("/reset 清空上下文   /exit 退出   ↑↓ 翻历史   Tab 补全")
            continue

        try:
            with ui.working("检索记忆库…") as status:
                agent._status = status
                try:
                    answer, history = agent.run(question, history)
                finally:
                    agent._status = None
        except Exception as exc:  # 单轮失败不退出交互
            ui.error(f"{type(exc).__name__}: {exc}")
            ui.print("[dim]（上下文保留，可以换个说法重试，或 /reset）[/dim]")
            continue
        ui.answer(answer)


if __name__ == "__main__":
    raise SystemExit(main())
