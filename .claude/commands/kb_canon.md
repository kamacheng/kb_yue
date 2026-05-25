---
name: kb-canon
description: 法典管理 - 查看/状态/导出/同步/分类冲突/更新规则
argument-hint: "[get|status|export|sync|classify|resolve_conflict|update_value|deprecate]"
---

调用 `kb_canon` 工具管理法典规则。

## action 路由

| 用户意图 | action | 必需参数 |
|---|---|---|
| 查看法典规则（默认） | `get` | module / status / priority（可选过滤） |
| 看法典健康状态 | `status` | — |
| 导出法典 Markdown | `export` | module（可选） |
| 从事实库同步规则到法典 | `sync` | module（可选） |
| 用 LLM 分类待处理冲突 | `classify` | top_k（默认 50） |
| 解决某条冲突 | `resolve_conflict` | `rule_id` + `new_value` |
| 修改规则值 | `update_value` | `rule_id` + `new_value`（+ reason） |
| 废弃规则 | `deprecate` | `rule_id` |

## 冲突处理流程

1. `action=status` 列出 conflicts / pending / needs_review
2. 必要时 `action=classify` 让 LLM 给出分类建议（deprecated_old / cross_doc_contradiction / semantic_overlap / format_variant）
3. 逐条用 `resolve_conflict` 或 `deprecate` 处理
4. 再次 `action=status` 确认归零

## 相关命令

- /kb_check — 用法典校验新设计
- /kb_audit — 找出事实库本身的矛盾
- /kb_index — 增量索引时会自动同步新事实到法典候选

$ARGUMENTS
