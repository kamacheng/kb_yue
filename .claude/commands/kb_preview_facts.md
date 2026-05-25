---
name: kb-preview-facts
description: 预览单文档的事实提取结果（不写入存储）
argument-hint: "[文档路径]"
---

调用 `kb_preview_facts` 工具，**只预览、不写入**事实库。

## 用途

- 索引前审核：检查事实抽取是否准确、有无遗漏
- 调试事实抽取器在某类文档上的表现

## 参数

- `path`: 文档路径，绝对路径或相对于 kb_root 的相对路径

## 输出处理

- 列出提取的事实及类型（constraint/enum/rule/dependency）
- 高亮疑似误提取或冗余项，供人工决策
- 确认无误后，用 /kb_index mode=single 正式索引该文档

$ARGUMENTS
