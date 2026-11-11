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
import re  # noqa: E402
import time  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from typing import Any, Iterator  # noqa: E402

# 保证从任何工作目录、任何方式启动都能 import 同目录模块
if str(_RAG_DIR) not in sys.path:
    sys.path.insert(0, str(_RAG_DIR))

try:
    from config import Config
    from corpus import read_text, redact
    from search import Searcher, _utf8_stdout
    from ui import UI, StatusHandle, make_session, corpus_words_from_index, _NoConsole
    from filetools import edit_file as fs_edit_file, write_card as fs_write_card
    from reindex import reindex as run_reindex
    from shelltool import run as fs_bash
except ModuleNotFoundError as exc:  # pragma: no cover - 环境问题引导
    missing = getattr(exc, "name", "依赖")
    sys.exit(
        f"缺少依赖：{missing}\n\n"
        f"rag/.venv 可能没建好。执行一次：\n"
        f'  uv sync --directory "{_RAG_DIR}"\n'
    )

# 不设总步数上限：正常干活该走几步就走几步，六步对拆卡这种活远远不够。
# 只拦一件事——同一个动作连同参数连续重复，那是原地打转唯一确定的信号。
REPEAT_LIMIT = 3

SYSTEM_PROMPT = """你是这个人的个人助手。它不是查笔记的工具：记忆层（它记住了什么、在哪、可信度多少）是最里面已经建成的那一层，你就在这一层上干活，也能往外伸手——新写卡片、改文件、跑命令。

库分两层：
- knowledge：知识卡片（`Cards/`）
- memory：关于用户的长期记忆（偏好、项目、外部资源指针）

工作规则：
1. 检索返回的 snippet 只是关键信息摘要，不是全文。需要细节、论证、原文摘录或 [[链接]] 时，必须用 read_note 读取整份文件。
2. 引用卡片时保留其 status 标记，引用长期记忆时保留 confidence 和来源。库里没有的内容就说“库中没有足够相关的记录”，不要用常识补全。
3. 回答用中文，先给结论再给依据，并说明信息来自哪些文件路径。
4. 要做的事需要多步（拆卡、批量整理、核对一批文件），就先动手做，不要停在“我可以帮你…”上。

你也能动手写：write_card 新建卡片，edit_file 改已有文件。规矩有四条：
- 新建卡片一律 draft，status 只能由用户改。
- 已经存在的文件不覆盖，要改就走 edit_file。
- edit_file 靠“原文精确匹配”定位，old_text 必须与文件内容一字不差。
- 写之前先 search_vault 查重，别把同一段知识写成两张卡。
- 「原文摘录」必须是原文里真实存在的句子，不得改写、不得拼接、不得自己组一句话冒充引用。

你还有一只 bash：能在库根目录执行命令，用来列目录、看行数、跑脚本。
只读命令（ls / cat / grep / Get-ChildItem 等）直接跑；其它命令会先停下来问用户。
想看库里有几张卡，用 ls Cards/ | measure 一类就行，别靠反复检索去猜。"""

# 拆卡的具体规矩不在代码里，而是运行时从 skill 文件读。只有一份，改一边就生效。
CARD_RULES_PATH = _RAG_DIR.parent / ".pi" / "skills" / "card-writer" / "SKILL.md"

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
    {
        "type": "function",
        "function": {
            "name": "write_card",
            "description": (
                "在 Cards/ 下新建一张知识卡片。只能新建，同名文件已存在会被拒绝。"
                "会检查 frontmatter 必填字段、status 是否为 draft、标题与文件名是否一致、"
                "「核心内容」是否超过 900 字；不合格直接拒绝写入并告诉哪里要改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "卡片标题，会用作文件名（不含 .md）"},
                    "content": {
                        "type": "string",
                        "description": (
                            "卡片完整内容，含 frontmatter。章节顺序：核心内容、原文摘录、"
                            "背景与来源、关联卡片（后三节没内容可省）"
                        ),
                    },
                    "subdir": {
                        "type": "string",
                        "description": "可选，Cards/ 下的子目录名；一般不用传",
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "按原文精确匹配替换文件内容，用于改已有卡片或给老卡补反向链接。"
                "old_text 必须与文件内容一字不差（含空格换行）；找不到或多处命中都会拒绝。"
                "不能改卡片以外的代码与配置，也不能改 status 审核状态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对库根目录的路径"},
                    "old_text": {"type": "string", "description": "要被替换的原文，原样抄"},
                    "new_text": {"type": "string", "description": "替换成什么；传空字符串表示删掉这段"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "原文出现多次時是否全部替换，默认 false",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "在库根目录执行一条命令，返回输出。默认超时 60 秒，输出超长会被截断。"
                "只读命令（ls / dir / cat / type / grep / findstr / Get-ChildItem / measure 等）"
                "直接执行；其它命令会先问用户。删除、关机、格式化、改注册表、"
                "git push 这类一律拒绝。cd 不会延续到下一次调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令，一句话"},
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数，默认 60；跑脚本可以调到 120",
                    },
                },
                "required": ["command"],
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


def _is_empty_result(result: str) -> bool:
    """判断工具这一下是不是什么也没拿到（空检索、读取失败、路径被拒）。"""
    text = (result or "").strip()
    if not text:
        return True
    markers = ("没有命中任何文件", "文件不存在", "拒绝访问", "不是 markdown")
    return text.startswith(markers)


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
        self._card_rules: str | None = None
        # 本次会话是否落过盘；退出时提醒重建索引
        self.dirty = False

    def _system_prompt(self) -> str:
        """主提示词 + 拆卡规矩。规矩文件不在就只发主提示词，不报错。"""
        if self._card_rules is None:
            try:
                self._card_rules = CARD_RULES_PATH.read_text(encoding="utf-8")
            except OSError:
                self._card_rules = ""
        if not self._card_rules:
            return SYSTEM_PROMPT
        # skill 文件开头的 frontmatter 是给别的工具看的，拆卡时用不上
        body = re.sub(r"^---\s*\n[\s\S]*?\n---\s*\n", "", self._card_rules, count=1)
        return (
            SYSTEM_PROMPT
            + "\n\n---\n\n以下是拆卡必须遵守的规矩（与别的工具共用同一份文件，改那里就生效）：\n\n"
            + body.strip()
        )

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
        root = self.cfg.root.resolve()
        target = (root / path).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            return f"拒绝访问：{path} 不在库目录内。"
        if not relative.parts or relative.parts[0] not in {"Cards", "Memory", "resource"}:
            return f"拒绝访问：{path} 不在笔记目录内。"
        if not target.is_file() or target.suffix.lower() != ".md":
            return f"文件不存在或不是 markdown：{path}"
        patterns = [re.compile(pattern) for pattern in self.cfg.secret_patterns]
        return redact(read_text(target), patterns)[0]

    def _tool_write_card(self, title: str, content: str, subdir: str = "") -> str:
        result = fs_write_card(self.cfg.root, title, content, subdir)
        if result.ok:
            self._log(f"write_card {result.path}")
            self.dirty = True
        else:
            self._log(f"write_card 被拒：{title}")
        lines = [result.message]
        if result.errors:
            lines.append("要改的地方：")
            lines.extend(f"- {item}" for item in result.errors)
        if result.path and not result.ok:
            lines.append(f"（目标路径：{result.path}）")
        return "\n".join(lines)

    def _tool_edit_file(
        self, path: str, old_text: str, new_text: str, replace_all: bool = False
    ) -> str:
        result = fs_edit_file(self.cfg.root, path, old_text, new_text, replace_all)
        if result.ok:
            self._log(f"edit_file {result.path}")
            self.dirty = True
        else:
            self._log(f"edit_file 被拒：{path}")
        lines = [result.message]
        if result.errors:
            lines.append("要改的地方：")
            lines.extend(f"- {item}" for item in result.errors)
        return "\n".join(lines)

    def _audit(self, command: str, result: str) -> None:
        """把命令和结果各记一行，出事能回查。"""
        try:
            log_dir = self.cfg.store_path.parent
            log_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "command": command,
                "result": (result or "")[:400],
            }
            with open(log_dir / "shell.log", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _tool_bash(self, command: str, timeout: int = 60) -> str:
        self._log(f"bash {command}")
        result = fs_bash(self.cfg.root, command, timeout=timeout, confirm=self.confirm)
        self._audit(command, result)
        return result

    @staticmethod
    def _action_key(name: str, raw_args: str) -> str:
        """把一次工具调用压成一个可比较的字符串：同名同参数算同一个动作。"""
        try:
            parsed = json.loads(raw_args or "{}")
            normalized = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        except json.JSONDecodeError:
            normalized = (raw_args or "").strip()
        return f"{name}|{normalized}"

    def _dispatch(self, name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return "工具参数不是合法 JSON。"
        if not isinstance(args, dict):
            return "工具参数必须是 JSON 对象。"
        if name == "search_vault":
            return self._tool_search(
                query=str(args.get("query", "")),
                layer=str(args.get("layer", "all")),
                top_k=int(args.get("top_k", 8) or 8),
            )
        if name == "read_note":
            return self._tool_read(str(args.get("path", "")))
        if name == "write_card":
            return self._tool_write_card(
                title=str(args.get("title", "")),
                content=str(args.get("content", "")),
                subdir=str(args.get("subdir", "") or ""),
            )
        if name == "edit_file":
            return self._tool_edit_file(
                path=str(args.get("path", "")),
                old_text=str(args.get("old_text", "")),
                new_text=str(args.get("new_text", "")),
                replace_all=args.get("replace_all") is True,
            )
        if name == "bash":
            return self._tool_bash(
                command=str(args.get("command", "")),
                timeout=int(args.get("timeout", 60) or 60),
            )
        return f"未知工具：{name}"

    def run(
        self, question: str, history: list[dict[str, Any]] | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        """跑一轮问答，返回 (答案, 可供下一轮使用的历史)。

        历史里只保留 user/assistant 文本，丢掉工具调用细节，避免上下文膨胀。
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            *(history or []),
            {"role": "user", "content": question},
        ]
        last_action: str | None = None
        repeats = 0

        while True:
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
                key = self._action_key(call.function.name, call.function.arguments)
                if key == last_action:
                    repeats += 1
                else:
                    last_action, repeats = key, 1

                result = self._dispatch(call.function.name, call.function.arguments)

                # 只在“没拿到任何结果”时算作打转：始终没搜到、读取失败。
                # 相同搜索反复做但每次都有命中，是正常的深度查证，不算。
                nothing_gained = repeats >= 2 and _is_empty_result(result)
                if nothing_gained and repeats >= REPEAT_LIMIT:
                    self._log(f"重复动作 {repeats} 次且无结果，停止：{call.function.name}")
                    answer = (
                        f"（{call.function.name} 连同参数已经连续做过 {repeats} 次，"
                        "每次都没拿到新东西，我停下来了。换个说法，或直接告诉我文件路径。）"
                    )
                    return answer, list(history or []) + [
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": answer},
                    ]

                if nothing_gained:
                    result += (
                        "\n\n[提示] 这是完全相同的动作，而且同样什么都没拿到。"
                        "换个做法，或直接给结论。再重复一次我会停下来。"
                    )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    }
                )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="记忆层命令行：查这个 agent 记住了什么")
    parser.add_argument("question", nargs="?", help="问题；省略则进入交互模式")
    parser.add_argument("--provider", default="open-design", help="读取 ~/.pi/agent/models.json 的 provider")
    parser.add_argument("--model", default=None, help="模型名；省略则用 pi 的默认模型")
    parser.add_argument("--base-url", default=None, help="覆盖 base URL")
    parser.add_argument("--config", default=None, help="RAG 配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="在 stderr 打印工具调用过程")
    return parser


def _report_dirty(agent: VaultAgent, ui: UI) -> int:
    """这次会话写过文件就提醒重建索引，不然刚写的卡搜不到。"""
    if not agent.dirty:
        return 0
    ui.print("新内容尚未进索引，现在搜不到。运行 /index，或退出后执行：")
    ui.print(f'  uv run --directory "{_RAG_DIR}" python ingest.py')
    return 0


def main() -> int:
    _utf8_stdout()
    args = build_arg_parser().parse_args()

    cfg = Config.load(args.config)
    base_url, api_key, model = resolve_provider(args.provider, args.model, args.base_url)
    ui = UI(verbose=args.verbose)
    agent = VaultAgent(cfg, base_url, api_key, model, verbose=args.verbose, ui=ui)

    def _confirm(command: str) -> bool:
        ui.print(f"[yellow]要执行：[/yellow] {command}")
        try:
            return input("  执行吗？[y/N] ").strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False

    # 交互模式才有人可以点头；单次提问时没人看着，一律只跑只读命令
    agent.confirm = None if args.question else _confirm

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
            _report_dirty(agent, ui)
            return 1
        ui.answer(answer)
        return _report_dirty(agent, ui)

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
                    return _report_dirty(agent, ui)
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
                return _report_dirty(agent, ui)

        if not question:
            ui.print("（退出）")
            return _report_dirty(agent, ui)
        if question.lower() in {"/exit", "/quit", "exit", "quit"}:
            ui.print("（退出）")
            return _report_dirty(agent, ui)
        if question.lower() == "/reset":
            history = []
            ui.print("[dim]（已清空上下文，下一轮重新开始）[/dim]")
            continue
        if question.lower() == "/help":
            ui.print("/index 更新索引   /reset 清空上下文   /exit 退出   ↑↓ 翻历史   Tab 补全")
            continue
        if question.lower() == "/index":
            try:
                with ui.working("更新索引…") as status:
                    status.update("扫描变更文件…")
                    _, summary = run_reindex(agent.cfg)
                    agent.searcher = Searcher(agent.cfg)
                    agent.dirty = False
            except Exception as exc:
                ui.error(f"{type(exc).__name__}: {exc}")
                continue
            ui.print(summary)
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
