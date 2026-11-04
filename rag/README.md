# 个人 Agent

这个仓库是一个 **personal agent** 的代码。不是「一个能查笔记的 AI 助手」，也不是「一个记忆层」——记忆层是这个 agent 身上已经建好的那部分。

建设顺序是由内向外：先把记忆层做实，再一层层往外长。先做记忆不是因为它简单，是因为 agent 得先记得住事，才谈得上主动做事；它也是唯一能用题目量出来的一层。

| 层 | 管什么 | 状态 |
|---|---|---|
| 记忆层 | 记住了什么——卡片、关于你的事实、检索 | **已完成**，`rag/` + 库里的 `Cards/` `Memory/` |
| 能力层 | 会做什么——可以反复调用的做法和 skill | 还没开始 |
| 执行层 | 能碰到什么——读写文件、跑命令、上网、发消息 | 还没开始 |

终点是一个 PA：能自己发起事、能动手、跨会话还记得住。从记忆往外走的每一步具体长什么样，还没定，定了再往这里加，不要提前编。

下面整篇讲的是已经做好那一层（记忆层）。`rag/` 是这个 agent 现在的第一层代码，不是项目的边界。

## 记忆层是什么

把 Obsidian 库变成可语义检索的记忆。索引是**增量**的：只处理新增或修改过的文件。

当前分块模式是 `card`：**一张卡一个 chunk，只嵌入关键信息**（标题 + description + 核心内容）。卡片全文不放进向量库，检索命中后由 Agent 按 `path` 读取整张 `.md`，这样既省索引空间，也能看到卡里的 `[[链接]]`。

## 目录结构

```
库根/
├── Cards/      知识卡片（入库）      ← 不提交 git
├── Memory/     长期记忆（入库）      ← 不提交 git
├── resource/   原始资料（不入库）    ← 不提交 git
├── 图库/       图片（不入库）        ← 不提交 git
└── rag/        本管线代码            ← 提交 git
```

`resource/` 里的长文、对话记录、视频笔记是卡片的原材料。卡片拆完之后不再重复索引；
卡片正文的「背景与来源」已标注出处，需要原文时由 Agent 按路径直接读文件。

**Git 只跟踪 `rag/` 代码与规则文档**，个人内容（卡片、记忆、原资料、图片）均未提交。

## 环境

- Python 3.10+（当前机器为 3.12）
- [uv](https://docs.astral.sh/uv/)（已安装）
- 首次运行会自动下载 `google/embeddinggemma-300m`（约 1GB）。旧模型 `BAAI/bge-small-zh-v1.5` 一次只读 512 个字，装不下长卡片，已换掉

## 常用命令

```powershell
# 增量更新索引
uv run --directory rag python ingest.py

# 全量重建
uv run --directory rag python ingest.py --rebuild

# 检索知识层（默认）
uv run --directory rag python search.py "Agent 的权限边界"

# 检索记忆层
uv run --directory rag python search.py "我的项目" --layer memory

# 跨层检索
uv run --directory rag python search.py "休息" --layer all

# JSON（给 Agent 用）
uv run --directory rag python search.py "怎么安排休息" --json

# 过滤
uv run --directory rag python search.py "休息" --status draft
uv run --directory rag python search.py "我的偏好" --layer memory --type user

# 评测
uv run --directory rag python eval.py
```

Windows 下也可以直接双击或调用封装脚本：

```powershell
rag\ingest.cmd
rag\search.cmd "冥想走神"
```

## 架构

```
Markdown 文件
   │  scan_files：按 collection 的 include/exclude 扫描
   ▼
脱敏（secret_patterns）→ frontmatter 解析 → 提取关键信息
   │
   ▼
Embedding（fastembed / openai-compatible）
   │
   ▼
Chroma（rag/.index/chroma）+ manifest（rag/.index/manifest.json，记录 sha256）
```

检索时并行走两路：

1. 稠密向量 top N：只比对关键信息；
2. BM25（jieba 中文分词）top N：**从磁盘读取完整正文**，并补上 frontmatter 里的标题和 description（卡片正文没有 H1，不补标题会漏掉标题命中）。

用 RRF 融合，按 `search.dense_weight` / `search.bm25_weight` 加权。最后按文件去重：同一个文件默认只保留一个 chunk（`search.max_per_path`），Agent 拿到 `path` 后再读整张卡。

`--layer` 默认是 `knowledge`；查个人事实、偏好、项目时用 `--layer memory`；需要跨层时用 `--layer all`。

### 分块模式

`config.yaml` 的 `chunking.mode` 有两种：

| 模式 | 行为 | 适用 |
|---|---|---|
| `card`（当前） | 一张卡一个 chunk，只嵌入标题 + description + 核心内容 | 卡片是独立知识单元，Agent 会按路径回读全文 |
| `section` | 按 Markdown 标题切多个 chunk，每块带标题面包屑 | 长文档，需要精确到小节 |

`chunking.embed_max_chars` 控制关键信息的截断长度；`max_chars` / `min_chars` / `overlap` 只在 `section` 模式下生效。切换模式会导致全量重建（配置签名变化）。

## `pkq` 命令

`pkq "<查询>"` 是记忆层的一个用法，不是项目本体。它靠 `C:\Users\wangm\.local\bin\pkq.cmd` 启动，那个脚本里写死了一个叫 `vaultrag` 的快捷方式，指向本目录。移动或重命名 `rag/` 之后 `pkq` 会失效，重建快捷方式即可：

```powershell
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.local\bin\vaultrag" -Target "<新的 rag 目录路径>"
```

## 目录

| 路径 | 作用 |
|---|---|
| `config.yaml` | 语料范围、分块、模型、检索参数 |
| `chunker.py` | frontmatter 解析、关键信息提取、section 分块 |
| `corpus.py` | 文件扫描、编码兼容读取、去掉 frontmatter 的正文读取 |
| `embeddings.py` | embedding provider 注册表 |
| `store.py` | Chroma 封装 |
| `manifest.py` | 增量索引清单 |
| `ingest.py` | 建库 / 增量更新 |
| `search.py` | 混合检索 CLI |
| `eval.py` + `eval_queries.yaml` | 小评测集 |
| `.index/` | 生成物，可随时删除重建 |

## 扩展方式

- **换 embedding**：改 `config.yaml` 的 `embedding.provider` / `model`，重跑 `ingest.py --rebuild`。
- **接 API**：把 provider 改为 `openai-compatible`，填 `base_url`，用环境变量提供 key（不要把 key 写进配置）。
- **加语料层**：在 `collections` 下新增一个 collection 和 include 规则，重跑 ingest。
- **加 reranker**：在 `search.py` 的 RRF 之后接一个 rerank 函数即可，接口只需返回排序后的列表。
- **接 MCP**：后续可把 `Searcher.search()` 与 `remember()` 包成 MCP 工具，供任意 Agent 调用。

## 国内网络

脚本会默认设置 `HF_ENDPOINT=https://hf-mirror.com`。如果模型下载仍失败，可手动设置：

```powershell
$env:HF_ENDPOINT="https://hf-mirror.com"
uv run --directory rag python ingest.py
```

## 常见问题

**Chroma 报 sqlite 版本错误**：确认 Python 3.10+；必要时在 `rag` 目录删掉 `.index/chroma` 重建。

**模型下载慢**：使用上面的 HF 镜像；模型缓存在用户目录，第二次不再下载。

**分词不理想**：`search.py` 的 `tokenize()` 里可加自定义词典，例如 `jieba.add_word("RAG")`。

**想换向量库**：`store.py` 只暴露 `upsert / delete_path / query / get_all / count`，换实现不影响上层。
