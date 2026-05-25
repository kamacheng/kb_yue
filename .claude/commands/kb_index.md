---
name: kb-index
description: 索引管理 - 单文件/增量/全量/清理孤儿
argument-hint: "[incremental|full|single <path>|cleanup]"
---

调用 `kb_index` 工具管理索引。

## mode 路由

| 场景 | mode | 备注 |
|---|---|---|
| 日常更新（发现新增/变更） | `incremental`（默认） | |
| 首次建库 / 大规模重构 | `full` | 耗时长，会重跑全部 LLM |
| 只索引单个文档 | `single` | 需提供 `path` |
| 清理已删除文件的残留 | `cleanup` | 默认 dry-run，需 `confirm=True` 才真删 |

## 关键参数

- `skip_facts=True`：跳过 LLM 事实抽取，加速索引（大批量重建可用）
- `clear_canon=True`：仅 `full` 模式有效，会同时清空法典
- `confirm=True`：仅 `cleanup` 模式需要，否则只展示将删除的项

## 执行后

- 若返回 `conflicts_detected > 0`，用 /kb_canon action=status 查看并处理
- 全量重建后建议跑 /kb_audit 核对跨文档一致性

$ARGUMENTS
