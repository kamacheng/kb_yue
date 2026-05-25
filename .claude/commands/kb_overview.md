---
name: kb-overview
description: 知识库概览 - 统计/最近变更/处理状态盲区
argument-hint: "[stats|changes|gaps]"
---

调用 `kb_overview` 工具查看不同维度的概览信息。

## view 选择

| 用户意图 | view | 关键参数 |
|---|---|---|
| 整体统计（默认） | `stats` | — |
| 最近变更的文档 | `changes` | `days`（默认 7，最大 365） |
| 处理状态盲区 | `gaps` | `module`（按文件名关键字过滤） |

## 输出处理

- **stats**：文档数 / chunk 数 / 模块数 / 法典状态 → 反映知识库规模
- **changes**：按时间倒序，发现变更后建议 /kb_index mode=incremental
- **gaps**：识别四类问题
  - 原始已上传但未转 markdown
  - md 已修订但索引过期
  - 索引中的孤儿（源文件已删）
  - 完全未索引
  → 引导 /kb_index 或 /kb_index mode=cleanup 处理

$ARGUMENTS
