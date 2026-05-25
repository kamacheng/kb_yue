---
name: kb-get-document
description: 获取指定文档的全文内容
argument-hint: "[文档路径]"
---

调用 `kb_get_document` 工具读取文档全文。

## 参数

- `path`: 文档路径，绝对路径或相对于 kb_root 的相对路径
  - 例如：`充值与付费/充值与付费-需求分析文档.md`
  - 若用户只给文档名找不到，先用 /kb_list_modules 或 /kb_search 定位完整路径

## 输出处理

- 直接返回文档全文 Markdown
- 文档过长时按需分段引用，并注明来源路径

$ARGUMENTS
