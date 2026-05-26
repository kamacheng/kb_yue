# kb_yue —  MCP Server

一个基于 [Model Context Protocol](https://modelcontextprotocol.io/) 的知识库服务，为 Claude Code / Cursor / 其他 MCP 客户端提供：

- **混合语义搜索**：向量召回 + BM25 关键词召回 + Reranker 精排
- **事实层**：从设计文档中抽取数值/设定/规则，做一致性检查
- **法典层**：把团队约定固化成规则，对新内容做合规检查
- **文档管理**：模块化组织、关系图谱、增量索引、文件监听

> 不论你是想查询知识库（只读使用者）还是维护知识库（写入维护者），**安装方式完全一致**。差别只在「同步哪些子目录」和「能用哪些工具」，见下文。

---

## 快速开始

### 前置条件

- Python 3.10+
- SiliconFlow 账号（向量嵌入 + Reranker）：[申请 API Key](https://cloud.siliconflow.cn/account/ak)
- DeepSeek 账号（事实抽取 / AI 格式化）：[申请 API Key](https://platform.deepseek.com/api_keys)

### 安装

```bash
git clone https://github.com/kamacheng/kb_yue.git
cd kb_yue
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：填入 SILICONFLOW_API_KEY、DEEPSEEK_API_KEY、KB_ROOT
```

### KB_ROOT 目录约定

```
<KB_ROOT>/
├─ md_file/            ← 转化后的 Markdown（查询/读取的内容来源）        【必需】
├─ .kb_index/          ← 索引/事实/法典数据（运行时自动生成或同步获得）   【必需】
└─ original_file/      ← 原始 docx/xlsx/pdf（仅建索引时需要）             【仅维护者】
```

- **只读使用者**：让维护者把 `md_file/` 和 `.kb_index/` 同步给你即可，原始文档不用拉。
- **维护者**：三件套齐全。建议在 `.gitignore` 中忽略 `original_file/`（体积大、不便 diff，只读者也不需要）。

### 接入 MCP 客户端

以 Claude Code 为例，工作目录的 `.mcp.json` 加入：

```json
{
  "mcpServers": {
    "kb_mcp": {
      "command": "python",
      "args": ["D:/_pro/kb_yue/server.py"]
    }
  }
}
```

> 路径换成你实际 clone mcp-server的位置。若 `python` 不在 PATH，把 `command` 写成解释器完整路径。

---

## 工具一览

| 工具 | 用途 | 角色 |
| --- | --- | --- |
| `kb_search` | 混合语义搜索文档 / 事实 | 读 |
| `kb_list_modules` | 列出所有模块和文档 | 读 |
| `kb_get_document` | 获取指定文档全文 | 读 |
| `kb_get_module_relations` | 查看模块间依赖关系 | 读 |
| `kb_get_related_designs` | 找出与某模块相关的其他设计 | 读 |
| `kb_overview` | 统计 / 最近变更 / 处理状态盲区 | 读 |
| `kb_check` | 检查内容是否符合法典 / 与事实一致 | 读 |
| `kb_audit` | 全面体检知识库（跨文档矛盾） | 读 |
| `kb_draft_assist` | 起草新设计前收集素材+法典约束 | 读 |
| `kb_preview_facts` | 预览单文档的事实提取结果（不写入） | 读 |
| `kb_index` | 索引知识库（incremental / full / single / cleanup） | **写** |
| `kb_canon` | 管理法典规则（查看/状态/导出/同步/解决冲突） | **写** |

> 只读使用者不要调用「写」类工具，也不要调用 `kb_overview view=gaps` / `kb_index mode=cleanup`——它们需要 `original_file/` 才能给出正确结果。

---

## 维护者补充

> 只读使用者可跳过本节。

### 首次全量索引

接入 MCP 后，让 Claude 调用 `kb_index` 工具（`mode="full"`），或者直接自然语言输入`全量建立索引`，做一次全量索引（耗时取决于文档量与 LLM 限流）。之后日常 `mode="incremental"` 即可。

### 典型工作流

- **新加入一批文档**：放入 `original_file/` → `kb_index mode=incremental`（会自动转 md 再索引）
- **改单个文档后立刻生效**：`kb_index mode=single path=<文档路径>`
- **固化团队约定**：`kb_canon` 添加/更新规则 → 之后所有 `kb_check` 自动校验
- **`kb_audit` 报矛盾**：`kb_canon` 分类冲突 → 决策升级为法典 / 或回改文档

---

## 配套 Slash 命令（仅 Claude Code）

仓库自带 `.claude/commands/`，与每个 MCP 工具一一对应。复制到项目根目录即可。

其他 MCP 客户端（Cursor 等）不支持 slash 命令，直接用自然语言让模型调用工具即可。

### 命令清单

```
/kb_search /kb_list_modules /kb_get_document /kb_get_module_relations
/kb_get_related_designs /kb_overview /kb_check /kb_audit
/kb_draft_assist /kb_preview_facts

# 仅维护者
/kb_index /kb_canon
```

> 想把这些命令全局可用？把 `.claude/commands/` 整个拷贝到 `~/.claude/commands/`（用户级），任意工作目录都能调用。

---

## 目录结构（代码）

```
kb_yue/
├─ server.py                ← MCP server 入口
├─ indexer.py               ← 索引调度
├─ index_manager.py         ← 索引存储（ChromaDB + BM25）
├─ search_engine.py         ← 混合搜索引擎
├─ doc_parser.py            ← 文档解析
├─ xlsx_converter.py        ← Excel 转 Markdown
├─ sheet_preprocessor.py    ← 表格预处理
├─ fact_extractor.py        ← 事实抽取（调用 LLM）
├─ facts_store.py           ← 事实库
├─ canon_manager.py         ← 法典管理
├─ consistency_checker.py   ← 一致性检查
├─ ai_formatter.py          ← AI 格式化
├─ embedding.py             ← 向量嵌入封装
├─ query_analyzer.py        ← 查询重写
├─ module_aliases.py        ← 模块别名
├─ remap_paths.py           ← 路径重映射工具
├─ config.py                ← 配置加载
├─ config.json.example      ← 配置示例
└─ .env.example             ← 环境变量示例
```

---

## License

MIT
