# kb_yue — 游戏设计知识库 MCP Server

一个基于 [Model Context Protocol](https://modelcontextprotocol.io/) 的知识库服务，为 Claude Code / Cursor / 其他 MCP 客户端提供：

- **混合语义搜索**：向量召回 + BM25 关键词召回 + Reranker 精排
- **事实层**：从设计文档中抽取数值/设定/规则，做一致性检查
- **法典层**：把团队约定固化成规则，对新内容做合规检查
- **文档管理**：模块化组织、关系图谱、增量索引、文件监听

---

## 快速开始

### 前置条件

- Python 3.10+
- SiliconFlow 账号（用于向量嵌入 + Reranker）：[申请 API Key](https://cloud.siliconflow.cn/account/ak)
- DeepSeek 账号（用于事实抽取/AI 格式化）：[申请 API Key](https://platform.deepseek.com/api_keys)

### 安装

```bash
# 1. 克隆本仓库
git clone https://github.com/kamacheng/kb_yue.git
cd kb_yue

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 API key + 知识库路径（推荐方式）
cp .env.example .env
# 编辑 .env，填入：
#   - SILICONFLOW_API_KEY
#   - DEEPSEEK_API_KEY
#   - KB_ROOT（你的知识库根目录，绝对路径，Windows 单反斜杠可直接粘贴）
```

> **可选**：如果你想用 JSON 管理路径，复制 `config.json.example` 为 `config.json` 即可。
> 注意：JSON 中的 Windows 路径必须用 `/` 或 `\\`，单反斜杠会触发 JSON 解析错误。

### 准备你的知识库

`kb_root` 指向的目录建议有如下结构：

```
<你的知识库根目录>/
├─ original_file/      ← 原始文档（.docx / .xlsx / .pdf / .md 等）
├─ md_file/            ← 转化后的 Markdown（流程产物，可由工具生成）
└─ .kb_index/          ← 自动生成，存放向量库、事实库、法典等运行时数据
```

`original_file/` 和 `md_file/` 的目录约定可参考 `doc_parser.py` 与 `xlsx_converter.py` 中的处理逻辑。

### 在 MCP 客户端中接入

以 Claude Code 为例，在你的工作目录的 `.mcp.json` 中加入：

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

> 将 `D:/_pro/kb_yue/server.py` 替换为你克隆本仓库后的实际绝对路径。
> 如果系统里 `python` 不在 PATH，请把 `command` 写成 Python 解释器的完整路径，例如：
> `C:/Users/<你>/AppData/Local/Programs/Python/Python312/python.exe`

### 首次建立索引

接入 MCP 后，让 Claude 调用 `kb_index` 工具（`mode="full"`）做一次全量索引（耗时取决于文档量）。之后用 `mode="incremental"` 增量更新即可。

---

## 提供的工具

| 工具 | 用途 |
|---|---|
| `kb_search` | 语义搜索文档 / 事实 |
| `kb_list_modules` | 列出所有模块和文档 |
| `kb_get_document` | 获取指定文档全文 |
| `kb_get_module_relations` | 查看模块间依赖关系 |
| `kb_get_related_designs` | 找出与某模块相关的其他设计 |
| `kb_check` | 检查设计内容是否符合法典 / 与事实一致 |
| `kb_audit` | 全面体检知识库（跨文档矛盾） |
| `kb_canon` | 管理法典规则（查看/状态/导出/同步/解决冲突） |
| `kb_index` | 索引知识库（incremental / full / single / cleanup） |
| `kb_preview_facts` | 预览单文档的事实提取结果（不写入） |
| `kb_overview` | 统计 / 最近变更 / 处理状态盲区 |
| `kb_draft_assist` | 辅助起草新设计文档（收集素材+法典约束） |

详见 `server.py` 中各工具的 docstring。

## 配套 Slash 命令

仓库的 `.claude/commands/` 下提供了与每个工具一一对应的 slash 命令，clone 后在 Claude Code 中可直接使用：

```
/kb_search <关键词>           — 搜索文档/事实
/kb_list_modules             — 列出模块
/kb_get_document <路径>       — 读取文档全文
/kb_get_module_relations     — 模块关系图
/kb_get_related_designs <模块> — 关联设计
/kb_index [mode]             — 索引管理
/kb_preview_facts <路径>      — 预览事实提取
/kb_check <内容>              — 合规+一致性检查
/kb_audit [模块]              — 跨文档矛盾审计
/kb_canon [action]           — 法典管理
/kb_overview [view]          — 概览（stats/changes/gaps）
/kb_draft_assist <主题>       — 起草素材收集
```

---

## 目录结构

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
├─ config.json.example      ← 配置示例（复制为 config.json）
└─ .env.example             ← 环境变量示例（复制为 .env）
```

---

## 常见问题

**Q：报错 `未找到 SILICONFLOW_API_KEY`？**
A：确认 `.env` 文件已从 `.env.example` 复制并填好真实 key；`.env` 必须与 `server.py` 在同一目录。

**Q：报错 `未配置知识库路径`？**
A：确认 `.env` 文件已从 `.env.example` 复制，并填了 `KB_ROOT=<你的绝对路径>`。或者在 `config.json` 里设置 `kb_root`。

**Q：JSON 配置中粘贴 Windows 路径报错？**
A：JSON 不允许单反斜杠（`\t` `\n` 等会被当成转义符）。请改用 `.env` 配置（不需要转义），或在 JSON 里把 `\` 全部替换为 `/` 或 `\\`。

**Q：索引很慢 / 经常超时？**
A：首次全量索引会调用大量 LLM API，受网络和限流影响。可减小并发（修改 `config.py` 中 `LLM_API_SEMAPHORE` 的值）或分批处理。

**Q：能否换其他向量 / LLM 提供商？**
A：可以。修改 `config.py` 中 `get_siliconflow_client()` / `get_deepseek_client()` 的 `base_url` 与 key 即可，只要兼容 OpenAI 接口规范。

---

## License

MIT
