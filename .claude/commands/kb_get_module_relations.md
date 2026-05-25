---
name: kb-get-module-relations
description: 查看模块之间的依赖/引用关系图
---

调用 `kb_get_module_relations` 工具，无需参数。

## 输出处理

- 列出所有模块节点
- 展示引用关系（from → to）
- 帮助定位耦合点：被多个模块引用的核心模块

## 相关命令

- /kb_get_related_designs — 拉取某模块的关联设计事实
- /kb_list_modules — 模块文档清单
