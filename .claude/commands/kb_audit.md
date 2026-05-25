---
name: kb-audit
description: 审计知识库内部一致性，检测跨文档矛盾
argument-hint: "[模块名(可选)]"
---

调用 `kb_audit` 工具扫描事实库中的跨文档矛盾。

## 参数

- `module`: 可选，仅审计指定模块；不传则全局扫描
- `top_k`: 最多报告条数（1-50），默认 20

## 输出处理

- 按严重度展示矛盾对：事实 A vs 事实 B + 来源文档
- 对每条矛盾给出处理建议：
  - 应固化为规则 → 进入 /kb_canon action=sync
  - 文档需修订 → 用 /kb_get_document 查看原文
- 全部归零后，建议重新跑 /kb_overview view=stats 复查

$ARGUMENTS
