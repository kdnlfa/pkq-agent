# 个人 Agent 的约定

## 零、回复方式（优先于下文所有内容）

**回复的首要目标是降低用户的认知负载。**

- 用户不是专业程序员，读代码和技术细节本身就消耗精力。
- **尽可能少用技术名词。** 能说「把这个文件删掉」就不说「执行 `Remove-Item` 移除该 artifact」。
- 术语确实必须用时，第一次出现用一句话说清它是什么，不给定义。
- 说「会发生什么」「你要做什么」，不展开「底层调用什么」。
- 一次只讲一层，不在一段话里同时抛出多个新概念。
- 代码、命令、文件名、路径保持原样（改了会出错），但周围的解释用日常说法。
- 宁可少讲一个细节，也不让用户去查资料。

**少术语不等于含糊。** 结论该明确就明确，问题该指出就指出。降低认知负载是让人好懂，不是回避真话。

---

## 一、这个项目是什么

**整个项目本身就是一个 personal agent。** 不是「基于知识库的 AI 助手」，也不是「一个记忆层」——记忆层是这个 agent 身上已经建好的那部分。

现在的状态：只建成了最里面那一层。

| 层 | 管什么 | 状态 |
|---|---|---|
| 记忆层 | 记住了什么——卡片、关于你的事实、检索 | **已完成**，`Cards/` + `Memory/` + `rag/` |
| 能力层 | 会做什么——可以反复调用的做法和 skill | 还没开始 |
| 执行层 | 能碰到什么——读写文件、跑命令、上网、发消息 | 还没开始 |

建设顺序是**由内向外**：先把记忆层做实，再一层层往外长，每长一层都复用里面那几层。先做记忆不是因为它简单，是因为 agent 得先记得住事，才谈得上主动做事；它也是唯一能用题目量出来的一层——`rag/eval_queries.yaml` 里 87 道题，现在能过 86 道。

终点是一个 PA：能自己发起事、能动手、跨会话还记得住。从记忆往外走的每一步具体长什么样，还没定，定了再往这里写，不要提前编。

**写代码和写文档时按这个定位说话。** `rag/` 是这个 agent 现在的第一层代码，不是项目的边界；别把项目写成「知识库问答工具」。

`CLAUDE.md` 是本文件的逐字副本，两份必须一致。`.git/hooks/pre-commit` 里有钩子会在提交时自动把 `AGENTS.md` 拷过去，但改完后最好立刻手动同步一次——这个仓的钩子不进 git，仓库重建就丢了。

---

## 二、库的结构

Obsidian 库是记忆层的存储面（这个 agent 现在的第一层），由两层记忆加一条检索管线组成：

- `Cards/`：既有的知识卡片，只读，由 `Card-Writer` / `Card-Reader` 规则管理。
- `resource/`：原始资料（长文、对话记录、视频笔记），卡片就是从这里面拆出来的。只读，不入索引。
- `Memory/`：本 Agent 维护的长期记忆，可读写，由本文件管理。
- `rag/`：检索管线，负责把上述内容变成可语义检索的索引。

核心原则：**先索引，后检索，再读原文**。不要一上来扫描整个库。

索引只存每张卡的**关键信息**（标题、description、核心内容），一张卡一个 chunk。卡片全文不在向量库里，检索命中后按结果中的 `path` 读取整张 `.md`，这样也能拿到卡里的 `[[链接]]`。

**入库范围只有 `Cards/` 和 `Memory/`。** `resource/` 的原始资料不入库：卡片本来就是从它们里面拆出来的，重复索引只会让同一段知识占两个位置。卡片正文的「背景与来源」已经标注了原文出处；确实需要原文时，直接按 `resource/…` 路径读文件。

**Git 只跟踪代码与规则。** `Cards/`、`resource/`、`Memory/`、`图库/` 是个人内容，已在 `.gitignore` 里排除；公开仓库 `kdnlfa/pkq-agent` 只包含 `rag/` 代码、规则文档和 Obsidian 配置。

---

## 三、读取顺序

1. **会话开始先读 `MEMORY.md`**（一级索引，一行一条，成本极低）。
2. 需要具体知识或历史细节时，运行检索：

   ```powershell
   # 知识层（默认）：Cards/ 卡片
   uv run --directory rag python search.py "<查询>" --top 8

   # 记忆层：关于用户、偏好、TA 手上的项目
   uv run --directory rag python search.py "<查询>" --layer memory --json

   # 跨层检索
   uv run --directory rag python search.py "<查询>" --layer all
   ```

   也可直接运行 `pkq "<查询>"`（跨层检索问答）。
3. 只对检索命中的文件读取全文。不要为了“多找点资料”批量读取。
4. 检索结果不足时，明确说明“库中没有足够相关的记录”，不要用推测填充。

---

## 四、记忆分层

| 类型 | 目录 | 放什么 |
|---|---|---|
| `user` | `Memory/user/` | 关于用户的稳定事实与偏好（身份、语言、习惯、长期约束） |
| `project` | `Memory/project/` | **用户自己在做的项目**：目标、进展、分工、卡住的地方、做过的决定 |
| `reference` | `Memory/reference/` | 外部资源指针（URL、文档、工具、仓库） |

### `project/` 里记的是用户的活，不是这个 agent 自己的施工记录

这个区分很关键，弄反了以后会乱。

`Memory/project/` 是给 agent 用的——它要知道你手上在忙什么，你提起项目时能接上话。所以里面应该放：北宇治部活记录、你在弄的写作或研究、接下来要交的东西。

不放：

- 这个 agent 自己长成什么样。它有哪些层、`rag/` 怎么重构、下一步加什么能力——这些是本文件的事。
- 这次对话把哪几行代码改了、文档措辞怎么调的。那是提交记录和本文件该记的，写进 `Memory/` 只会把检索结果弄脏。
- 一次性的待办和临时路径。

一句话：**`Memory/project/` 回答「用户手上有哪些事」，本文件回答「这个 agent 自己该怎么长」。** 两边都写项目，但一个是你的事，一个是它的事。

知识性内容继续进 `Cards/`，不要塞进 `Memory/`。两者的区别：**Cards 是可审核的知识，Memory 是关于“人、事、偏好、上下文”的事实。**

### 关键区分：内容 vs 指令

`Memory/` 装的是**这个库的内容**——pkq 检索出来用于回答问题的资料。它不是给开发 agent（Claude Code、Codex 等）的指令。

对开发 agent 的要求（说话方式、操作边界、工作习惯）写在本文件，或写进全局 `CLAUDE.md`。**不要把「你该怎么回复我」这类指令写进 `Memory/`**——那会把指令当资料，检索出来以后变成噪音。

同理，「你该怎么回复我」「文档该怎么写」这类要求，以及这个 agent 自己长成什么样，都是指令与规划，不属于 `Memory/`。

---

## 五、记忆文件格式

一个文件只放一个事实。文件名 = `title`，放在对应类型目录。

```markdown
---
title: 默认中文交流
description: 用户默认使用中文交流，代码、命令、变量名使用英文。
type: user
created: 2026-11-03
updated: 2026-11-03
confidence: high
sources:
  - "[[CLAUDE.md]]"
related:
  - "[[非专业程序员]]"
---

默认用中文回答；代码、命令、变量名用英文。结论先行，不铺垫背景。
```

字段规则：

- `title`、`description`、`type`、`created`、`updated`、`confidence` 必填。
- `confidence` 取 `high` / `medium` / `low`。不确定的记忆必须标 `medium` 或 `low`，并写清不确定性。
- `sources` 记录来源（笔记链接或外部来源）；`related` 记录相关记忆。
- 正文用简洁的自然语言写事实本身，不写成长篇报告。
- 不写密钥、token、密码、身份证号等敏感信息。

---

## 六、何时写入记忆

满足以下任一条件才写：

- 用户明确纠正了你的做法，或确认了某种工作方式。
- 用户表达了稳定的偏好、习惯或长期约束。
- 项目出现了会影响后续决策的目标、状态或决定。
- 获得了一个可复用的外部资源指针。

不要写：

- 一次性的任务细节、临时文件路径、当次对话的中间结论。
- 用户没有确认的推测。
- 已经存在于 `Cards/` 的公共知识。

---

## 七、写入流程

1. 先在 `MEMORY.md` 和已有的 `Memory/` 文件中查重（按 `title` 与 `description`）。
2. 能更新已有记忆就不新建；更新时改 `updated`，必要时调整 `confidence`。
3. 新建文件 → 更新 `MEMORY.md` 增加一行索引。
4. 运行增量索引，让新记忆立即可检索：

   ```powershell
   uv run --directory rag python ingest.py
   ```

5. 在回复中说明写了什么、放在哪里。

---

## 八、边界

- **不修改 `Cards/`**，不修改既有卡片规则与 skill。
- 不把 `draft` 卡片当作 `approved` 使用；引用时保留状态。
- 不自动把卡片从 `draft` 改成 `approved`。
- 不删除任何记忆文件；过时记忆改为更新内容并降低 `confidence`，或标记 `confidence: low`，由用户决定是否清理。
- `rag/.index/` 是生成物，可随时重建，不要手工编辑。
- 记忆是辅助，不是真相：引用记忆时保留 `confidence` 与来源；与用户当前说法冲突时，以当前说法为准并更新记忆。

---

## 九、常用命令

```powershell
# 增量更新索引
uv run --directory rag python ingest.py

# 全量重建
uv run --directory rag python ingest.py --rebuild

# 检索（人类可读）
uv run --directory rag python search.py "Agent 的权限边界"

# 检索（JSON，给 Agent 用）
uv run --directory rag python search.py "怎么安排休息" --layer knowledge --json

# 评测
uv run --directory rag python eval.py
```

检索参数：`--layer knowledge|memory|all`（默认 `knowledge`）、`--status draft|approved`、`--type user|project|reference`、`--top K`。
同一个文件默认只返回一个 chunk（`search.max_per_path`）；需要看全文时按结果里的 `path` 打开文件。

BM25 一路会用完整正文（并补上 frontmatter 里的标题和 description）打分，向量一路只用关键信息，两路 RRF 融合。所以结果里的 `snippet` 只是关键信息摘要，不是全文。

---

## 十、`pkq` 这条命令

`pkq` 已经装在机器上，是记忆层对外的一个用法，不是项目本体。

它靠 `C:\Users\wangm\.local\bin\pkq.cmd` 启动，那个脚本里写死了一个叫 `vaultrag` 的快捷方式，指向本项目里的 `rag/`。移动或重命名这个文件夹之后 `pkq` 会失效，修法是重建那个快捷方式：

```powershell
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.local\bin\vaultrag" -Target "<新的 rag 目录路径>"
```
