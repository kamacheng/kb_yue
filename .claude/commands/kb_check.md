---
name: kb-check
description: 检查新设计是否违反法典或与事实库矛盾
argument-hint: "[待检查内容]"
---

调用 `kb_check` 工具校验设计内容的合规性和一致性。

## mode 选择

| 场景 | mode |
|---|---|
| 完整双层检查（默认） | `full`（法典 + 事实） |
| 只关心是否违反团队规则 | `compliance` |
| 只关心与已有设定是否矛盾 | `consistency` |
| 快速预检（不调 LLM 对比） | `quick` |

## 参数

- `content`: 待检查内容（必需），从 $ARGUMENTS 提取
- `module`: 指定所属模块，缩小检查范围、提高准确度

## 输出处理

- 高亮 conflict / warning 项
- 标注命中的法典规则 ID 和事实来源
- 若冲突源于知识库本身矛盾，建议跑 /kb_audit 排查根因

## 相关命令

- /kb_draft_assist — 起草新设计前先收集素材
- /kb_canon — 查看或调整法典规则
- /kb_audit — 排查事实库内部冲突

$ARGUMENTS
