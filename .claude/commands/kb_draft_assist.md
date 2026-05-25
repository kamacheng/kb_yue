---
name: kb-draft-assist
description: 起草新设计前收集相关素材和法典约束
argument-hint: "[主题] [风格(可选)] [模块(可选)]"
---

调用 `kb_draft_assist` 工具收集起草素材。

**注意：该工具不生成文本，只检索 related_docs / canon_rules / guidance。**

## 参数

- `topic`: 起草主题（必需），例如 "冰雪地图副本背景"
- `style`: 写作风格（可选），例如 "世界观叙事"、"技术文档"
- `module`: 所属模块（可选），缩小检索范围

## 起草流程

1. 调用本工具拿到素材包
2. 基于 related_docs 理解上下文，基于 canon_rules 避开违规
3. 在对话中起草内容
4. 起草完成用 /kb_check mode=full 校验
5. 终稿落盘后用 /kb_index mode=single path=<新文档路径> 索引

$ARGUMENTS
