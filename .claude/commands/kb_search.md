---
name: kb-search
description: 在知识库中搜索文档或事实，支持语义+关键词混合检索
argument-hint: "[搜索关键词]"
---

调用 `kb_search` 工具搜索知识库。

## 参数选择

- `query`: 搜索文本，从 $ARGUMENTS 解析
- `target`: 默认 `docs`（搜文档）；用户提及"事实/数值/规则/枚举"时改为 `facts`
- `top_k`: 默认 5；用户说"多一些结果"时提升到 10-20
- `include_context`: 用户需要更完整上下文时设 True
- `module` / `doc_type` / `fact_type`: 用户指定过滤条件时传入
- `rewrite`: 召回明显不足或查询模糊时设 True（会有延迟）

## 输出处理

- 按相关度排序展示，保留 citation 溯源信息
- 结果为空时，建议改写查询或拓宽 module 过滤

## 相关命令

- /kb_get_document — 拉取命中文档的全文
- /kb_preview_facts — 检查某文档的事实提取详情

$ARGUMENTS
