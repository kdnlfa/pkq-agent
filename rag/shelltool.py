"""在库目录下执行一条命令。

按官方 bash 工具的做法（配合 Claude Code 的权限分层）取了这几条：
1. 白名单优先：只读命令直接跑，别的一律先问人。
2. 每次都有超时，超时连子进程一起杀掉，不留残渣。
3. 输出截断，避免一次打印几万行把上下文撑爆。
4. 命令和执行结果都记账，出事能回查。
5. 输出过一遍脱敏，密钥不外泄。

跟官方实现的差别：官方维持一个常驻 shell 让 cd、环境变量能延续到下一次调用。
这里每次开一个新进程，用库根目录当起点。代价是 cd 不会记住，
换来的是不会有隐藏状态在后台慢慢跑偏。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

# 只读命令：不需要问人，直接跑。
ALLOWED = {
    # 跨平台 / Unix 系
    "ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "egrep",
    "findstr", "find", "pwd", "echo", "tree", "stat", "file", "du", "df",
    "which", "where", "basename", "dirname", "sort", "uniq", "cut", "diff",
    # Windows / PowerShell
    "get-childitem", "get-content", "select-string", "measure-object",
    "get-item", "get-location", "test-path", "resolve-path", "get-command",
    "get-process", "get-date", "convertfrom-json", "convertto-json",
}

# 一律拒绝：与这个库无关、且一旦出错就很难挽回。
DENY_PATTERNS = [
    r"\brm\s+-[a-zA-Z]*(r[a-zA-Z]*f|f[a-zA-Z]*r)[a-zA-Z]*\s+[\"']?/\s*$\s*",
    r"\bdel\s+.*[\\/][sS]\b",
    r"\bformat\s+[a-zA-Z]:",
    r"\bshutdown\b", r"\brestart-computer\b", r"\bstop-computer\b",
    r"\bdiskpart\b", r"\bfdisk\b", r"\bmkfs\b",
    r"\breg\s+(add|delete)\b",
    r"\bset-executionpolicy\b",
    r"\bgit\s+push\b", r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\s+-[a-zA-Z]*f",
    r"\bpip\s+install\b", r"\bnpm\s+install\s+-g\b",
    r"\bcurl\b.*\|\s*(ba)?sh", r"\biwr\b.*\|\s*iex",
]

# 命令里出现这些目录就当作要动代码和规则，提示一句。
PROTECTED_HINTS = ("rag/", ".pi/", ".obsidian/", ".git/")

# Windows 下 PowerShell 默认按旧编码把中文写成乱码。开跑前先把输入输出都锁成 UTF-8。
PS_PROLOGUE = (
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
    "$OutputEncoding=[System.Text.Encoding]::UTF8; "
    "$PSDefaultParameterValues['Get-Content:Encoding']='utf8'; "
)

MAX_OUTPUT_CHARS = 12000
MAX_LINES = 300
DEFAULT_TIMEOUT = 60

SECRET_RES = [
    re.compile(r"(?i)sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[A-Za-z0-9._\-]{16,}"),
]


def _shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        ps = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return [ps, "-NoProfile", "-NonInteractive", "-Command", PS_PROLOGUE + command]
    return ["/bin/sh", "-c", command]


def first_token(command: str) -> str:
    """取出命令的动词。开头的 & 与括号、引号这类前缀不影响分类。"""
    text = command.strip().lstrip("(&@").strip()
    if not text:
        return ""
    return text.split()[0].strip("\"'").lower()


def needs_confirm(command: str) -> bool:
    return first_token(command) not in ALLOWED


def deny_reason(command: str) -> str:
    for pattern in DENY_PATTERNS:
        if re.search(pattern, command, re.I):
            return f"命令命中禁用规则：{pattern}"
    return ""


def redact(text: str) -> str:
    for pattern in SECRET_RES:
        text = pattern.sub("[REDACTED]", text)
    return text


def truncate(text: str) -> str:
    lines = text.splitlines()
    clipped = False
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        clipped = True
    out = "\n".join(lines)
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS]
        clipped = True
    if clipped:
        out += f"\n…（输出过长已截断，原共 {len(text.splitlines())} 行 / {len(text)} 字）"
    return out


def _kill_tree(pid: int) -> None:
    """连同这条命令派生的所有进程一起杀掉，别留后台残留。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "cp936"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run(
    root: Path,
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
    confirm=None,
) -> str:
    """执行一条命令，返回给模型看的文本结果。"""
    cmd = (command or "").strip()
    if not cmd:
        return "命令是空的。"

    reason = deny_reason(cmd)
    if reason:
        return f"拒绝执行：{reason}"

    if needs_confirm(cmd):
        if confirm is None:
            return (
                f"拒绝执行：{first_token(cmd)} 不在只读白名单里，"
                "当前模式无人可以确认，所以没跑。换成只读命令，或先问用户。"
            )
        if not confirm(cmd):
            return f"用户拒绝执行：{cmd}"

    kwargs: dict = {
        "cwd": str(root),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(_shell_argv(cmd), **kwargs)
    except FileNotFoundError:
        return "找不到可用的 shell（powershell / sh）。"

    try:
        raw, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process.pid)
        try:
            process.communicate(timeout=5)
        except Exception:
            pass
        return f"执行超时（{timeout}s），已终止整条命令及其子进程：{cmd}"

    output = redact(truncate(_decode(raw or b""))).strip()
    if not output:
        output = "（没有输出）"
    prefix = "" if process.returncode == 0 else f"[退出码 {process.returncode}]\n"
    # 整段回显都要过一遍脱敏：命令里也可能带着密钥
    return redact(f"$ {cmd}\n{prefix}{output}")
