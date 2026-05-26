"""一致性检查器 — 检测新设计内容与已有设计事实的矛盾。"""

from __future__ import annotations

import json
import sys

from config import get_deepseek_client
from fact_extractor import DesignFact, extract_facts
from facts_store import FactsStore


COMPARISON_PROMPT = """\
你是游戏设计一致性检查专家。比较以下新设计事实与已有设计事实，判断是否存在矛盾。

## 判断标准
- **真正矛盾**：同一属性的值不一致（如"上限15" vs "上限20"）→ severity: high
- **潜在矛盾**：可能冲突但不确定（如范围重叠）→ severity: medium
- **扩展补充**：在已有基础上增加新内容（如新增一种商品类型）→ 不算矛盾
- **无关内容**：描述不同维度的设计 → 不算矛盾

只报告真正的矛盾和潜在矛盾。扩展补充不算矛盾。

## 输出格式（纯 JSON）
{
  "status": "conflicts_found | no_conflicts | uncertain",
  "conflicts": [
    {
      "new_fact": "新事实文本",
      "existing_fact": "已有事实文本",
      "source": "来源文件",
      "severity": "high | medium | low",
      "explanation": "解释为什么这是矛盾"
    }
  ],
  "suggestions": "修改建议"
}
"""


def check_consistency(
    content: str,
    store: FactsStore,
    module: str | None = None,
    quick_check: bool = False,
) -> dict:
    if quick_check:
        return _quick_check(content, store, module)
    return _full_check(content, store, module)


def _quick_check(content: str, store: FactsStore, module: str | None) -> dict:
    related = store.search_facts(content[:500], top_k=10, module=module)
    return {
        "status": "quick_check",
        "related_facts": related,
        "note": "快速模式：仅返回相关事实，未进行矛盾分析。请人工检查。"
    }


def _full_check(content: str, store: FactsStore, module: str | None) -> dict:
    try:
        new_facts = extract_facts(content)
    except Exception as e:
        return {"status": "error", "checked": False, "message": f"事实提取失败: {e}"}

    if not new_facts:
        return {
            "status": "check_skipped",
            "checked": False,
            "reason": "未从新内容中提取到设计事实（可能内容太短/全是 UI 描述）",
            "message": "无法进行矛盾检查,请检查内容是否包含具体的约束/规则/枚举",
        }

    related_facts = []
    searched_subjects = set()
    for fact in new_facts:
        if fact.subject not in searched_subjects:
            by_subject = store.get_facts_by_subject(fact.subject)
            related_facts.extend(by_subject)
            searched_subjects.add(fact.subject)
        semantic = store.search_facts(fact.to_text(), top_k=5, module=module)
        related_facts.extend(semantic)

    seen = set()
    unique_related = []
    for f in related_facts:
        key = (f.get("subject", ""), f.get("predicate", ""), f.get("value", ""))
        if key not in seen:
            seen.add(key)
            unique_related.append(f)

    if not unique_related:
        return {
            "status": "check_skipped",
            "checked": False,
            "conflicts": [],
            "related_designs": [],
            "reason": "事实库中未找到相关已有事实",
            "suggestions": "新内容涉及全新主题,无法做一致性比对；建议正式索引后再次 audit",
        }

    try:
        client = get_deepseek_client()
        new_facts_text = "\n".join(f"- [{f.type}] {f.to_text()} (confidence={f.confidence})" for f in new_facts)
        existing_text = "\n".join(f"- [{f.get('type', '')}] {f.get('subject', '')} {f.get('predicate', '')} {f.get('value', '')} (来源: {f.get('source', '')}, 模块: {f.get('module', '')})" for f in unique_related)
        user_content = f"## 新设计事实\n{new_facts_text}\n\n## 已有设计事实\n{existing_text}"

        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": COMPARISON_PROMPT}, {"role": "user", "content": user_content}],
            temperature=0.1, max_tokens=2048,
        )
        result_text = response.choices[0].message.content.strip()
        if result_text.startswith("```"):
            result_text = result_text.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(result_text)
        sources = set(f.get("source", "") for f in unique_related if f.get("source"))
        result["related_designs"] = sorted(sources)
        result["checked"] = True
        return result
    except Exception as e:
        print(f"[WARN] 矛盾对比失败: {e}", file=sys.stderr)
        return {"status": "error", "message": f"矛盾对比失败: {e}", "new_facts": [f.to_dict() for f in new_facts], "related_facts": unique_related[:10]}


def compliance_check(
    content: str,
    canon_mgr=None,
    module: str | None = None,
    store: FactsStore | None = None,
) -> dict:
    """法典合规检查：优先对比法典规则，补充事实库比对。"""
    try:
        new_facts = extract_facts(content)
    except Exception as e:
        return {"status": "error", "checked": False, "message": f"事实提取失败: {e}"}

    if not new_facts:
        return {
            "status": "check_skipped",
            "checked": False,
            "canon_violations": [],
            "fact_conflicts": [],
            "passed_rules": 0,
            "reason": "未从内容中提取到事实",
            "suggestions": "无法做合规检查；请确认内容包含具体的数值约束/规则,或扩展内容长度",
        }

    violations = []
    passed = 0

    if canon_mgr is not None:
        active_rules = canon_mgr.get_rules(status="active", module=module)
        for fact in new_facts:
            for rule in active_rules:
                if (fact.subject == rule["subject"]
                        and fact.predicate == rule["predicate"]):
                    if fact.value == rule["value"]:
                        passed += 1
                    else:
                        violations.append({
                            "rule_id": rule["id"],
                            "rule": f"{rule['subject']} {rule['predicate']} {rule['value']}",
                            "your_value": fact.value,
                            "severity": rule.get("priority", "normal"),
                            "source": rule.get("source", ""),
                        })

    fact_conflicts = []
    if store is not None:
        result = _full_check(content, store, module)
        fact_conflicts = result.get("conflicts", [])

    suggestions = ""
    if violations:
        suggestions = "以下内容违反法典规则，建议调整：\n" + "\n".join(
            f"- {v['rule']}: 你的值={v['your_value']}" for v in violations)

    return {
        "status": "conflicts_found" if (violations or fact_conflicts) else "ok",
        "checked": True,
        "canon_violations": violations,
        "fact_conflicts": fact_conflicts,
        "passed_rules": passed,
        "suggestions": suggestions,
    }


AUDIT_PROMPT = """\
你是游戏设计一致性审计专家。以下是知识库中同一主题的设计事实，来自不同文档。
请检查它们之间是否存在矛盾。

## 判断标准
- **真正矛盾**：同一属性的值不一致（如文档A说"上限15"，文档B说"上限20"）→ severity: high
- **潜在矛盾**：描述有差异但可能是不同版本或不同阶段 → severity: medium
- **一致/互补**：描述一致或互为补充 → 不算矛盾

## 输出格式（纯 JSON）
{
  "status": "conflicts_found | no_conflicts",
  "conflicts": [
    {
      "fact_a": "事实A文本 (来源: 文件A)",
      "fact_b": "事实B文本 (来源: 文件B)",
      "severity": "high | medium",
      "explanation": "矛盾说明"
    }
  ],
  "suggestions": "修改建议"
}
"""


def audit_facts(
    store: FactsStore,
    module: str | None = None,
    top_k: int = 20,
) -> dict:
    """审计知识库内部事实的一致性。

    按 subject 分组事实，对来自不同 source 的同主题事实进行矛盾检测。

    Args:
        store: 事实存储实例
        module: 可选，只审计指定模块
        top_k: 最多报告的矛盾数量

    Returns:
        审计结果字典
    """
    # 1. 获取所有事实
    if module:
        all_facts = store.get_facts_by_module(module)
    else:
        # 获取所有模块的事实
        try:
            collection = store._collection
            all_data = collection.get(include=["metadatas"])
            all_facts = [
                {
                    "subject": m.get("subject", ""),
                    "predicate": m.get("predicate", ""),
                    "value": m.get("value", ""),
                    "type": m.get("type", ""),
                    "module": m.get("module", ""),
                    "source": m.get("source", ""),
                    "confidence": m.get("confidence", 0),
                }
                for m in all_data.get("metadatas", [])
            ]
        except Exception:
            all_facts = []

    if not all_facts:
        return {"status": "empty", "message": "事实存储为空，请先索引文档"}

    # 2. 按归一化 subject 分组(去空格/标点),避免"VIP 等级"与"VIP等级"被当作两组
    import re as _re
    from collections import defaultdict

    def _normalize(s: str) -> str:
        if not s:
            return ""
        return _re.sub(r'[\s，。、：:]+', '', s.strip()).lower()

    groups: dict[str, list[dict]] = defaultdict(list)
    for fact in all_facts:
        groups[_normalize(fact.get("subject", "unknown"))].append(fact)

    # 3. 同组内进一步按 (predicate, value) 归一化去重,
    #    确保"上限15" 与 "上限 15" 不被当作两条独立事实进入比对
    def _fact_key(f: dict) -> tuple:
        return (_normalize(f.get("predicate", "")), _normalize(f.get("value", "")))

    candidates = []
    for subject_norm, facts in groups.items():
        seen: dict[tuple, dict] = {}
        for f in facts:
            k = _fact_key(f)
            # 同 (pred, value) 归一后只保留 confidence 最高的代表
            if k not in seen or float(f.get("confidence", 0)) > float(seen[k].get("confidence", 0)):
                seen[k] = f
        unique_facts = list(seen.values())
        sources = set(f.get("source", "") for f in unique_facts)
        if len(sources) > 1:
            # 用原始 subject 作展示名(取首条事实的)
            display_subject = unique_facts[0].get("subject", subject_norm)
            candidates.append((display_subject, unique_facts))

    if not candidates:
        return {"status": "no_duplicates", "message": "未发现同主题跨文档的事实，无需审计"}

    # 4. LLM 对比
    try:
        client = get_deepseek_client()
    except Exception as e:
        return {"status": "error", "message": f"无法连接 API: {e}"}

    all_conflicts = []
    for subject, facts in candidates:
        if len(all_conflicts) >= top_k:
            break

        facts_text = "\n".join(
            f"- {f.get('subject', '')} {f.get('predicate', '')} {f.get('value', '')} "
            f"(来源: {f.get('source', '')}, 模块: {f.get('module', '')})"
            for f in facts
        )
        user_content = f"## 主题: {subject}\n\n{facts_text}"

        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": AUDIT_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.1,
                max_tokens=1024,
            )
            result_text = response.choices[0].message.content.strip()
            if result_text.startswith("```"):
                result_text = result_text.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(result_text)
            if result.get("conflicts"):
                all_conflicts.extend(result["conflicts"])
        except Exception as e:
            print(f"[WARN] 审计主题 '{subject}' 失败: {e}", file=sys.stderr)

    if all_conflicts:
        return {
            "status": "conflicts_found",
            "conflicts": all_conflicts[:top_k],
            "total_subjects_checked": len(candidates),
            "suggestions": "请检查上述矛盾事实，统一描述以保持知识库一致性"
        }
    else:
        return {
            "status": "no_conflicts",
            "conflicts": [],
            "total_subjects_checked": len(candidates),
            "suggestions": "知识库内部事实一致，未发现矛盾"
        }
