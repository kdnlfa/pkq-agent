# 记忆库 RAG 管线

把 Obsidian 库变成可语义检索的记忆库。索引是**增量**的：只处理新增或修改过的文件。

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
- 首次运行会自动下载 `BAAI/bge-small-zh-v1.5`（约 100MB）

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
