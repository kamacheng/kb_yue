"""法典管理器 — 结构化规则库的 CRUD、合并、冲突解决、导出。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import filelock

from config import CANON_PATH, CANON_CHANGELOG_PATH, CANON_LOCK_PATH, DATA_DIR


@dataclass
class CanonRule:
    """法典规则数据类。"""
    type: str               # constraint / enum / rule / dependency
    priority: str           # critical / normal / low
    subject: str
    predicate: str
    value: str
    module: str
    source: str
    confidence: float
    status: str = "active"
    depends_on: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    conflict_with: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class CanonManager:
    """法典管理器。"""

    def __init__(
        self,
        canon_path: Path | None = None,
        changelog_path: Path | None = None,
        lock_path: Path | None = None,
    ):
        self._canon_path = Path(canon_path or CANON_PATH)
        self._changelog_path = Path(changelog_path or CANON_CHANGELOG_PATH)
        self._lock_path = Path(lock_path or CANON_LOCK_PATH)
        self._lock = filelock.FileLock(self._lock_path, timeout=30)

    def _read_canon(self) -> dict:
        if self._canon_path.exists():
            try:
                return json.loads(self._canon_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"version": "1.0", "updated_at": _now_iso(), "rules": [], "metadata": {}}

    def _write_canon(self, data: dict):
        self._canon_path.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = _now_iso()
        data["metadata"] = self._compute_metadata(data["rules"])
        with self._lock:
            self._canon_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def _compute_metadata(self, rules: list[dict]) -> dict:
        meta = {
            "total_rules": len(rules),
            "active": 0, "pending": 0, "conflicts": 0,
            "deprecated": 0, "needs_review": 0,
            "by_type": {}, "by_module": {}, "by_priority": {},
        }
        for r in rules:
            status = r.get("status", "active")
            if status in meta:
                meta[status] += 1
            t = r.get("type", "")
            meta["by_type"][t] = meta["by_type"].get(t, 0) + 1
            m = r.get("module", "")
            meta["by_module"][m] = meta["by_module"].get(m, 0) + 1
            p = r.get("priority", "")
            meta["by_priority"][p] = meta["by_priority"].get(p, 0) + 1
        return meta

    def _next_id(self, rules: list[dict]) -> str:
        max_num = 0
        for r in rules:
            rid = r.get("id", "")
            if rid.startswith("canon_"):
                try:
                    num = int(rid.split("_")[1])
                    max_num = max(max_num, num)
                except (IndexError, ValueError):
                    pass
        return f"canon_{max_num + 1:04d}"

    def _log_change(self, action: str, rule_id: str, subject: str,
                    detail: str, trigger: str = "manual",
                    old_value: str | None = None, new_value: str | None = None):
        entry = {
            "timestamp": _now_iso(),
            "action": action,
            "rule_id": rule_id,
            "subject": subject,
            "detail": detail,
            "trigger": trigger,
        }
        if old_value is not None:
            entry["old_value"] = old_value
        if new_value is not None:
            entry["new_value"] = new_value

        changelog = {"entries": []}
        if self._changelog_path.exists():
            try:
                raw = json.loads(self._changelog_path.read_text(encoding="utf-8"))
                # 兼容旧格式：如果是 list 则包装为 dict
                if isinstance(raw, list):
                    changelog = {"entries": raw}
                elif isinstance(raw, dict) and "entries" in raw:
                    changelog = raw
            except (json.JSONDecodeError, OSError):
                pass
        changelog["entries"].append(entry)
        self._changelog_path.parent.mkdir(parents=True, exist_ok=True)
        self._changelog_path.write_text(
            json.dumps(changelog, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- Public API ----------

    def add_rule(self, rule: CanonRule) -> str:
        """添加新规则，返回 rule_id。"""
        data = self._read_canon()
        rule_id = self._next_id(data["rules"])
        now = _now_iso()
        entry = {
            "id": rule_id, "type": rule.type, "priority": rule.priority,
            "subject": rule.subject, "predicate": rule.predicate,
            "value": rule.value, "module": rule.module,
            "source": rule.source, "confidence": rule.confidence,
            "status": rule.status,
            "depends_on": rule.depends_on, "dependents": rule.dependents,
            "conflict_with": rule.conflict_with,
            "added_at": now, "updated_at": now, "history": [],
        }
        data["rules"].append(entry)
        self._write_canon(data)
        self._log_change("added", rule_id, rule.subject,
                         f"新增规则: {rule.subject} {rule.predicate} {rule.value}",
                         new_value=rule.value)
        return rule_id

    def get_rules(
        self,
        module: str | None = None,
        rule_type: str | None = None,
        priority: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """读取法典规则，支持过滤。

        module 过滤时会先归一化入参，使别名（如 "权限获取模块"）也能命中规范名（"权限获取"）。
        """
        from module_aliases import normalize_module

        data = self._read_canon()
        rules = data["rules"]
        if module:
            canonical = normalize_module(module)
            rules = [r for r in rules if r.get("module") == canonical]
        if rule_type:
            rules = [r for r in rules if r.get("type") == rule_type]
        if priority:
            rules = [r for r in rules if r.get("priority") == priority]
        if status:
            rules = [r for r in rules if r.get("status") == status]
        return rules

    def update_rule(self, rule_id: str, new_value: str, reason: str = ""):
        """更新规则值，旧值写入 history，触发依赖传播。"""
        data = self._read_canon()
        for r in data["rules"]:
            if r["id"] == rule_id:
                old_value = r["value"]
                r["history"].append({
                    "old_value": old_value,
                    "new_value": new_value,
                    "changed_at": _now_iso(),
                    "reason": reason,
                })
                r["value"] = new_value
                r["updated_at"] = _now_iso()
                # Propagate to dependents first (modifies data)
                self._propagate_dependents(data, rule_id)
                # Write everything at once
                self._write_canon(data)
                self._log_change("updated", rule_id, r["subject"],
                                 f"值从 {old_value} 更新为 {new_value}: {reason}",
                                 old_value=old_value, new_value=new_value)
                return
        raise ValueError(f"规则 {rule_id} 不存在")

    def deprecate_rule(self, rule_id: str):
        """废弃规则，并自动记录抑制模式防止重新提取。"""
        data = self._read_canon()
        for r in data["rules"]:
            if r["id"] == rule_id:
                r["status"] = "deprecated"
                r["updated_at"] = _now_iso()
                self._write_canon(data)
                self._log_change("deprecated", rule_id, r["subject"], "规则已废弃")
                # 记录抑制模式
                self.add_suppression(
                    r.get("subject", ""), r.get("predicate", ""),
                    f"用户废弃规则 {rule_id}")
                return
        raise ValueError(f"规则 {rule_id} 不存在")

    def get_status(self) -> dict:
        """返回法典统计信息。"""
        data = self._read_canon()
        return data.get("metadata", {})

    def get_changelog(self, days: int = 7) -> list[dict]:
        """返回最近 N 天的变更日志。"""
        if not self._changelog_path.exists():
            return []
        try:
            changelog = json.loads(self._changelog_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        entries = changelog.get("entries", [])
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        return [e for e in entries if e.get("timestamp", "") >= cutoff]

    # 已知同义 predicate 对，用于模糊匹配
    _PREDICATE_SYNONYMS = [
        ("条件为", "前置条件为"),
        ("上限为", "最高为"),
        ("上限为", "不超过"),
        ("分为", "包括"),
        ("分为", "类型包括"),
        ("共有", "包含"),
        ("规则为", "要求"),
    ]

    def _is_similar_rule(self, existing: dict, fact) -> bool:
        """判断现有规则与新事实是否语义相似。"""

        # 精确匹配
        if existing["subject"] == fact.subject and existing["predicate"] == fact.predicate:
            return True

        # subject 子串匹配
        subject_match = (
            fact.subject in existing["subject"] or
            existing["subject"] in fact.subject
        )
        if not subject_match:
            return False

        # predicate 归一化比较
        norm_existing = re.sub(r'[\s，。、：:]+', '', existing["predicate"])
        norm_new = re.sub(r'[\s，。、：:]+', '', fact.predicate)

        if norm_existing == norm_new:
            return True

        # 同义词匹配
        for a, b in self._PREDICATE_SYNONYMS:
            if (a in norm_existing and b in norm_new) or (b in norm_existing and a in norm_new):
                return True

        return False

    @staticmethod
    def _build_subject_index(rules: list[dict]) -> dict[str, list[int]]:
        """构建 {subject: [rule_index]} 映射，用于加速相似规则查找。"""
        from collections import defaultdict
        index: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(rules):
            if r.get("status") in ("active", "conflict"):
                subj = r.get("subject", "")
                if subj:
                    index[subj].append(i)
        return dict(index)

    def _find_similar_rule(self, rules: list[dict], subject_index: dict[str, list[int]], fact) -> dict | None:
        """使用 subject 索引快速查找相似规则。"""
        candidates: set[int] = set()
        for subj, indices in subject_index.items():
            if fact.subject in subj or subj in fact.subject:
                candidates.update(indices)
        for idx in candidates:
            if self._is_similar_rule(rules[idx], fact):
                return rules[idx]
        return None

    def merge_batch(
        self, items: list[dict], trigger: str = "kb_rebuild_index",
    ) -> dict:
        """批量合并事实到法典，单次读写。

        Args:
            items: [{"fact": DesignFact, "priority": str, "source": str, "module": str}, ...]
            trigger: 触发来源标识

        Returns:
            {"new_rules": int, "skipped": int, "conflicts_detected": int, "suppressed": int, "warnings": list[str]}
        """
        from module_aliases import normalize_module

        report = {"new_rules": 0, "skipped": 0, "conflicts_detected": 0,
                  "suppressed": 0, "warnings": []}
        if not items:
            return report

        data = self._read_canon()
        subject_index = self._build_subject_index(data["rules"])

        for item in items:
            fact = item["fact"]
            priority: str = item["priority"]
            source: str = item.get("source", "unknown")
            # 兜底归一化：无论调用方是否预先归一化，写入法典的 module 一律规范名
            module: str = normalize_module(item.get("module", ""))

            # 检查抑制模式
            if self._is_suppressed(fact.subject, fact.predicate):
                report["suppressed"] += 1
                continue

            existing = self._find_similar_rule(data["rules"], subject_index, fact)

            if existing is None:
                rule_id = self._next_id(data["rules"])
                now = _now_iso()
                new_rule = {
                    "id": rule_id, "type": fact.type, "priority": priority,
                    "subject": fact.subject, "predicate": fact.predicate,
                    "value": fact.value, "module": module or fact.subject,
                    "source": source, "confidence": fact.confidence,
                    "status": "active", "depends_on": [], "dependents": [],
                    "conflict_with": None,
                    "added_at": now, "updated_at": now, "history": [],
                }
                data["rules"].append(new_rule)
                # 更新索引
                new_idx = len(data["rules"]) - 1
                subject_index.setdefault(fact.subject, []).append(new_idx)
                report["new_rules"] += 1
                self._log_change("added", rule_id, fact.subject,
                                 f"自动提取: {fact.subject} {fact.predicate} {fact.value}",
                                 trigger=trigger, new_value=fact.value)

            elif existing["value"] == fact.value:
                report["skipped"] += 1

            elif (
                (fact.value in existing["value"] or existing["value"] in fact.value)
                and not re.fullmatch(r'[\d.,\s]+', fact.value.strip())
                and not re.fullmatch(r'[\d.,\s]+', existing["value"].strip())
                and min(len(fact.value), len(existing["value"])) >= 5
            ):
                if len(fact.value) > len(existing["value"]):
                    existing["value"] = fact.value
                    existing["updated_at"] = _now_iso()
                report["skipped"] += 1

            else:
                existing["status"] = "conflict"
                existing["updated_at"] = _now_iso()

                conflict_id = self._next_id(data["rules"])
                now = _now_iso()
                data["rules"].append({
                    "id": conflict_id, "type": fact.type, "priority": priority,
                    "subject": fact.subject, "predicate": fact.predicate,
                    "value": fact.value, "module": module or existing.get("module", ""),
                    "source": source, "confidence": fact.confidence,
                    "status": "pending", "depends_on": [], "dependents": [],
                    "conflict_with": existing["id"],
                    "added_at": now, "updated_at": now, "history": [],
                })
                report["conflicts_detected"] += 1
                warning = (f"{fact.subject} {fact.predicate}: "
                           f"旧值={existing['value']} vs 新值={fact.value}")
                report["warnings"].append(warning)
                self._log_change("conflict_detected", existing["id"], fact.subject,
                                 warning, trigger=trigger)

        self._write_canon(data)
        return report

    def merge_filtered_facts(
        self, filtered: list[dict], source: str, module: str = "",
        trigger: str = "kb_update_index",
    ) -> dict:
        """将筛选后的事实合并到法典。

        Returns:
            {"new_rules": int, "skipped": int, "conflicts_detected": int, "suppressed": int, "warnings": list[str]}
        """
        from fact_extractor import DesignFact
        from module_aliases import normalize_module

        # 兜底归一化：写入法典的 module 一律规范名
        module = normalize_module(module)

        report = {"new_rules": 0, "skipped": 0, "conflicts_detected": 0,
                  "suppressed": 0, "warnings": []}
        data = self._read_canon()
        subject_index = self._build_subject_index(data["rules"])

        for item in filtered:
            fact: DesignFact = item["fact"]
            priority: str = item["priority"]

            # 检查抑制模式
            if self._is_suppressed(fact.subject, fact.predicate):
                report["suppressed"] += 1
                continue

            existing = self._find_similar_rule(data["rules"], subject_index, fact)

            if existing is None:
                rule_id = self._next_id(data["rules"])
                now = _now_iso()
                new_rule = {
                    "id": rule_id, "type": fact.type, "priority": priority,
                    "subject": fact.subject, "predicate": fact.predicate,
                    "value": fact.value, "module": module or fact.subject,
                    "source": source, "confidence": fact.confidence,
                    "status": "active", "depends_on": [], "dependents": [],
                    "conflict_with": None,
                    "added_at": now, "updated_at": now, "history": [],
                }
                data["rules"].append(new_rule)
                # 更新 subject 索引
                new_idx = len(data["rules"]) - 1
                subject_index.setdefault(fact.subject, []).append(new_idx)
                report["new_rules"] += 1
                self._log_change("added", rule_id, fact.subject,
                                 f"自动提取: {fact.subject} {fact.predicate} {fact.value}",
                                 trigger=trigger, new_value=fact.value)

            elif existing["value"] == fact.value:
                report["skipped"] += 1

            elif (
                (fact.value in existing["value"] or existing["value"] in fact.value)
                and not re.fullmatch(r'[\d.,\s]+', fact.value.strip())
                and not re.fullmatch(r'[\d.,\s]+', existing["value"].strip())
                and min(len(fact.value), len(existing["value"])) >= 5
            ):
                # 值为子串关系（非纯数值）：同规则不同完整度，保留更完整的值
                if len(fact.value) > len(existing["value"]):
                    existing["value"] = fact.value
                    existing["updated_at"] = _now_iso()
                report["skipped"] += 1

            else:
                existing["status"] = "conflict"
                existing["updated_at"] = _now_iso()

                conflict_id = self._next_id(data["rules"])
                now = _now_iso()
                data["rules"].append({
                    "id": conflict_id, "type": fact.type, "priority": priority,
                    "subject": fact.subject, "predicate": fact.predicate,
                    "value": fact.value, "module": module or existing.get("module", ""),
                    "source": source, "confidence": fact.confidence,
                    "status": "pending", "depends_on": [], "dependents": [],
                    "conflict_with": existing["id"],
                    "added_at": now, "updated_at": now, "history": [],
                })
                report["conflicts_detected"] += 1
                warning = (f"{fact.subject} {fact.predicate}: "
                           f"旧值={existing['value']} vs 新值={fact.value}")
                report["warnings"].append(warning)
                self._log_change("conflict_detected", existing["id"], fact.subject,
                                 warning, trigger=trigger)

        self._write_canon(data)
        return report

    def resolve_conflict(self, rule_id: str, action: str, new_value: str | None = None):
        """解决法典冲突。
        action: "keep_old" / "accept_new" / "set_value"
        """
        data = self._read_canon()
        target = None
        pending_rule = None

        for r in data["rules"]:
            if r["id"] == rule_id:
                target = r
            if r.get("conflict_with") == rule_id and r["status"] == "pending":
                pending_rule = r

        if target is None:
            raise ValueError(f"规则 {rule_id} 不存在")

        if action == "keep_old":
            target["status"] = "active"
            target["updated_at"] = _now_iso()
            if pending_rule:
                data["rules"].remove(pending_rule)
            self._log_change("resolved", rule_id, target["subject"],
                             f"保留旧值: {target['value']}", trigger="kb_update_canon")

        elif action == "accept_new":
            if pending_rule is None:
                raise ValueError(f"规则 {rule_id} 没有对应的 pending 冲突")
            old_value = target["value"]
            target["history"].append({
                "old_value": old_value,
                "new_value": pending_rule["value"],
                "changed_at": _now_iso(),
                "reason": "冲突解决：采用新值",
            })
            target["value"] = pending_rule["value"]
            target["source"] = pending_rule["source"]
            target["status"] = "active"
            target["updated_at"] = _now_iso()
            data["rules"].remove(pending_rule)
            self._log_change("resolved", rule_id, target["subject"],
                             f"采用新值: {old_value} -> {pending_rule['value']}",
                             trigger="kb_update_canon",
                             old_value=old_value, new_value=pending_rule["value"])
            self._propagate_dependents(data, rule_id)

        elif action == "set_value":
            if new_value is None:
                raise ValueError("set_value 操作需要提供 new_value")
            old_value = target["value"]
            target["history"].append({
                "old_value": old_value,
                "new_value": new_value,
                "changed_at": _now_iso(),
                "reason": "冲突解决：设置新值",
            })
            target["value"] = new_value
            target["status"] = "active"
            target["updated_at"] = _now_iso()
            if pending_rule:
                data["rules"].remove(pending_rule)
            self._log_change("resolved", rule_id, target["subject"],
                             f"设置新值: {old_value} -> {new_value}",
                             trigger="kb_update_canon",
                             old_value=old_value, new_value=new_value)
            self._propagate_dependents(data, rule_id)

        self._write_canon(data)

    def set_dependency(self, parent_id: str, child_id: str):
        """设置规则依赖关系。"""
        data = self._read_canon()
        for r in data["rules"]:
            if r["id"] == parent_id:
                if child_id not in r["dependents"]:
                    r["dependents"].append(child_id)
            if r["id"] == child_id:
                if parent_id not in r["depends_on"]:
                    r["depends_on"].append(parent_id)
        self._write_canon(data)

    def _propagate_dependents(self, data: dict, changed_rule_id: str):
        """依赖传播：标记 dependents 为 needs_review。"""
        for r in data["rules"]:
            if r["id"] == changed_rule_id:
                for dep_id in r.get("dependents", []):
                    for dep_rule in data["rules"]:
                        if dep_rule["id"] == dep_id and dep_rule["status"] == "active":
                            dep_rule["status"] = "needs_review"
                            dep_rule["updated_at"] = _now_iso()
                            self._log_change("needs_review", dep_id, dep_rule["subject"],
                                             f"依赖规则 {changed_rule_id} 已变更",
                                             trigger="dependency_propagation")
                break

    def infer_dependencies(self) -> dict:
        """自动推断规则间的逻辑依赖关系。

        如果规则 A 的 value 中包含规则 B 的 subject（且 subject >= 3字），
        则建立 A depends_on B 的关系。

        Returns:
            {"new_dependencies": int, "total_checked": int}
        """
        data = self._read_canon()
        rules = [r for r in data["rules"] if r["status"] == "active"]
        rules_by_id = {r["id"]: r for r in rules}
        subject_to_rule = {r["subject"]: r for r in rules if len(r.get("subject", "")) >= 3}

        new_deps = 0
        for rule_a in rules:
            value = rule_a.get("value", "")
            if not value:
                continue
            for subj, rule_b in subject_to_rule.items():
                if rule_b["id"] == rule_a["id"]:
                    continue
                if subj in value and rule_b["id"] not in rule_a.get("depends_on", []):
                    rule_a.setdefault("depends_on", []).append(rule_b["id"])
                    rule_b.setdefault("dependents", []).append(rule_a["id"])
                    new_deps += 1

        if new_deps > 0:
            self._write_canon(data)

        return {"new_dependencies": new_deps, "total_checked": len(rules)}

    # ---------- 抑制模式 ----------

    @property
    def _suppression_path(self) -> Path:
        return DATA_DIR / "suppression_patterns.json"

    def _load_suppressions(self) -> list[dict]:
        """加载抑制模式列表。"""
        if self._suppression_path.exists():
            try:
                return json.loads(self._suppression_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save_suppressions(self, patterns: list[dict]) -> None:
        self._suppression_path.parent.mkdir(parents=True, exist_ok=True)
        self._suppression_path.write_text(
            json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_suppression(self, subject_pattern: str, predicate_pattern: str, reason: str = ""):
        """添加抑制模式，防止同类事实重新进入法典。"""
        patterns = self._load_suppressions()
        # 避免重复
        for p in patterns:
            if p.get("subject") == subject_pattern and p.get("predicate") == predicate_pattern:
                return
        patterns.append({
            "subject": subject_pattern,
            "predicate": predicate_pattern,
            "reason": reason,
            "added_at": _now_iso(),
        })
        self._save_suppressions(patterns)

    def _is_suppressed(self, subject: str, predicate: str) -> bool:
        """检查事实是否匹配任何抑制模式。"""
        for p in self._load_suppressions():
            p_subject = p.get("subject", "")
            p_predicate = p.get("predicate", "")
            if p_subject and p_subject in subject:
                if not p_predicate or p_predicate in predicate:
                    return True
        return False

    def export_as_markdown(self, module: str | None = None) -> str:
        """导出法典为 Markdown 格式。"""
        rules = self.get_rules(module=module)
        if not rules:
            return "# 法典导出\n\n暂无规则。\n"

        from collections import defaultdict
        by_module: dict[str, list[dict]] = defaultdict(list)
        for r in rules:
            by_module[r.get("module", "未分类")].append(r)

        now = datetime.now().strftime("%Y-%m-%d")
        lines = [f"# 法典导出 - {now}\n"]

        for mod_name, mod_rules in sorted(by_module.items()):
            lines.append(f"\n## {mod_name}\n")

            by_type: dict[str, list[dict]] = defaultdict(list)
            for r in mod_rules:
                by_type[r.get("type", "other")].append(r)

            type_names = {
                "constraint": "约束 (constraint)",
                "enum": "枚举 (enum)",
                "rule": "规则 (rule)",
                "dependency": "依赖 (dependency)",
            }

            for t, t_rules in sorted(by_type.items()):
                lines.append(f"\n### {type_names.get(t, t)}\n")
                lines.append("| ID | 主题 | 规则 | 优先级 | 来源 | 状态 |")
                lines.append("|----|------|------|--------|------|------|")
                for r in t_rules:
                    rid = r.get("id", "")
                    subj = r.get("subject", "")
                    pred_val = f"{r.get('predicate', '')} {r.get('value', '')}"
                    prio = r.get("priority", "")
                    src = Path(r.get("source", "")).name
                    status = r.get("status", "")
                    lines.append(f"| {rid} | {subj} | {pred_val} | {prio} | {src} | {status} |")

        changelog = self.get_changelog(days=7)
        if changelog:
            lines.append("\n## 最近变更（7天内）\n")
            lines.append("| 时间 | 操作 | 规则ID | 详情 |")
            lines.append("|------|------|--------|------|")
            for entry in changelog[-20:]:
                ts = entry.get("timestamp", "")[:16]
                action = entry.get("action", "")
                rid = entry.get("rule_id", "")
                detail = entry.get("detail", "")
                lines.append(f"| {ts} | {action} | {rid} | {detail} |")

        return "\n".join(lines) + "\n"


# ---------- LLM Filtering ----------

from config import get_deepseek_client
from fact_extractor import DesignFact

CANON_FILTER_PROMPT = """\
你是游戏设计规则筛选专家。判断以下事实是否对设计决策有约束力，值得纳入法典。

## 严格筛选原则
宁缺勿滥。法典只收录「硬约束」——违反它会导致功能出错、数值失衡或玩家利益受损的规则。
如果一条事实只是描述现有功能、列举后台操作步骤、或罗列字段名，则不应纳入。

## 约束力的标准
核心问题：**如果其他策划不知道这条规则，是否可能做出违反它的设计？**
- 是 → worthy: true
- 否（常识性内容、UI 描述、后台操作、字段列举） → worthy: false

## 对每条事实回答
- worthy: true/false（是否值得纳入法典）
- priority: critical/normal/low
  - critical: 违反会导致严重问题（数值边界、核心流程规则、支付安全规则）
  - normal: 违反会导致设计不一致但不致命（核心枚举分类、一般流程规则）
  - low: 信息性规则，最好遵守但不强制（字段描述、显示格式）
- reason: 简短理由

## 正面示例（应纳入）
- [constraint] VIP等级 上限为 15 → worthy:true, critical（数值边界）
- [rule] 已发布商品修改 仅对新订单生效 → worthy:true, critical（核心业务规则）
- [constraint] 支付超时判定时间 为 8秒 → worthy:true, critical（时序约束）
- [enum] 商品类型 分为 基础商品/活动商品/特殊商城商品 → worthy:true, normal（核心分类）
- [rule] 购买前 必须校验 VIP等级 → worthy:true, critical（流程规则）

## 负面示例（不应纳入）
- [rule] 充值记录筛选 条件包括 玩家ID/订单ID → worthy:false（UI功能描述）
- [enum] 导出格式 包括 Excel/CSV → worthy:false（工具功能，非设计约束）
- [constraint] 多语言配置表 包含 14行配置 → worthy:false（数据量描述，随时变）
- [enum] 价格配置字段 分为 sellPrice_CN/sellPrice_TW → worthy:false（字段名列举）
- [rule] 后台新增商品 操作步骤为 点击新增→填写表单→提交 → worthy:false（后台操作流程）
- [rule] 商品列表 支持按名称/ID/状态筛选 → worthy:false（CRUD 功能描述）
- [enum] 运营活动配置 包含 活动名称/开始时间/结束时间/奖励 → worthy:false（运营配置字段枚举）
- [rule] 邮件管理 支持 新建/编辑/删除/批量发送 → worthy:false（后台功能枚举）

输出纯 JSON 数组，与输入事实一一对应。
"""


_CANON_FILTER_BATCH_SIZE = 30  # 每批最多 30 条事实，更小批次让 LLM 判断更精准


def _fallback_priority(f: DesignFact) -> str:
    """回退时根据事实类型做基本 priority 分级。"""
    if f.type == "constraint":
        # 含数值边界关键词 → critical
        boundary_keywords = ("上限", "下限", "不超过", "至少", "超时", "最多", "最少", "最高", "最低")
        if any(kw in f.predicate for kw in boundary_keywords):
            return "critical"
        if any(kw in f.value for kw in boundary_keywords):
            return "critical"
    if f.type == "rule":
        # 含强制/校验/必须等流程约束 → critical
        mandatory_keywords = ("必须", "禁止", "不允许", "不得", "强制", "校验", "验证")
        if any(kw in f.predicate for kw in mandatory_keywords):
            return "critical"
        if any(kw in f.value for kw in mandatory_keywords):
            return "critical"
    return "normal"


def _filter_single_batch(batch_facts: list[DesignFact]) -> list[dict]:
    """对单批事实调用 LLM 筛选。失败时使用规则回退。

    Returns:
        [{"fact": DesignFact, "priority": str, "reason": str}, ...]
    """
    facts_text = "\n".join(
        f"{i+1}. [{f.type}] {f.subject} {f.predicate} {f.value} (confidence={f.confidence})"
        for i, f in enumerate(batch_facts)
    )

    try:
        client = get_deepseek_client()
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": CANON_FILTER_PROMPT},
                {"role": "user", "content": facts_text},
            ],
            temperature=0.1,
            max_tokens=4096,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        evaluations = json.loads(content)

        results = []
        for i, ev in enumerate(evaluations):
            if i < len(batch_facts) and ev.get("worthy", False):
                priority = ev.get("priority", "normal")
                if priority == "low":
                    continue
                results.append({
                    "fact": batch_facts[i],
                    "priority": priority,
                    "reason": ev.get("reason", ""),
                })
        return results

    except Exception as e:
        import sys
        print(f"[WARN] 法典筛选批次失败 ({e})，使用规则回退 ({len(batch_facts)} 条)", file=sys.stderr)
        return [
            {"fact": f, "priority": _fallback_priority(f), "reason": "规则回退"}
            for f in batch_facts
            if f.confidence >= 0.9 and f.type in ("constraint", "rule")
        ]


def filter_facts_for_canon(facts: list[DesignFact]) -> list[dict]:
    """用 LLM 分批筛选有约束力的事实并评估优先级。

    包含本地预过滤（跳过低置信度和 dependency 类型）和并行 LLM 调用。

    Returns:
        [{"fact": DesignFact, "priority": str, "reason": str}, ...]
    """
    import sys
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from config import LLM_API_SEMAPHORE

    if not facts:
        return []

    # 本地预过滤：跳过 confidence < 0.8 和 type == "dependency"
    pre_filtered = [f for f in facts if f.confidence >= 0.8 and f.type != "dependency"]
    skipped = len(facts) - len(pre_filtered)
    if skipped > 0:
        print(f"[CANON] 预过滤跳过 {skipped} 条 (低置信度/dependency)", file=sys.stderr)

    if not pre_filtered:
        return []

    # 分批
    batches = [
        pre_filtered[i:i + _CANON_FILTER_BATCH_SIZE]
        for i in range(0, len(pre_filtered), _CANON_FILTER_BATCH_SIZE)
    ]
    total_batches = len(batches)
    print(f"[CANON] 开始筛选: {len(pre_filtered)} 条事实, {total_batches} 批", file=sys.stderr)

    # 并行 LLM 筛选
    all_results = []
    t_start = time.time()
    completed_count = 0

    def _filter_with_semaphore(batch):
        with LLM_API_SEMAPHORE:
            return _filter_single_batch(batch)

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_idx = {
            executor.submit(_filter_with_semaphore, batch): idx
            for idx, batch in enumerate(batches)
        }
        batch_results_by_idx = {}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            batch_results_by_idx[idx] = future.result()
            completed_count += 1
            elapsed = round(time.time() - t_start, 1)
            print(f"[CANON] 筛选进度: {completed_count}/{total_batches} 批完成 ({elapsed}s)", file=sys.stderr)

    # 按原顺序合并结果
    for idx in range(total_batches):
        all_results.extend(batch_results_by_idx.get(idx, []))

    total_elapsed = round(time.time() - t_start, 1)
    print(f"[CANON] 筛选完成: {len(all_results)}/{len(pre_filtered)} 条通过 ({total_elapsed}s)", file=sys.stderr)
    return all_results


# ---------- 冲突分类 ----------

_CONFLICT_CLASSIFY_PROMPT = """你是法典冲突分析助手。判断以下规则冲突的类型并给出处理建议。

## 冲突类型（必须从以下选其一）

- deprecated_old: 旧值反映被取代的过时设计（如旧版本数值/已下线机制）→ 建议 keep_new
- cross_doc_contradiction: 跨文档真矛盾（不同文档对同一事项给出不一致定义,需要团队定夺）→ 建议 manual
- semantic_overlap: subject 文字相似但 predicate 含义实际不同（不应视为冲突）→ 建议 manual
- format_variant: 表述形式差异但语义完全一致（数值相同、单位相同、含义相同）→ 建议 merge
- uncategorized: 信息不足无法判断 → 建议 manual

## 关键边界判断

**数值差异**: 即使一个用"约"一个用精确数,只要数值不同就是 cross_doc_contradiction（非 format_variant）
  - 例: "上限15" vs "上限约15"  → cross_doc_contradiction（数值表达精度差异,真矛盾）
  - 例: "上限15" vs "上限20"    → cross_doc_contradiction（明显矛盾）
  - 例: "上限15" vs "上限为15"  → format_variant（仅措辞差异,语义一致）

**单位差异**: 同一数量不同单位是 format_variant 还是 contradiction 取决于单位是否等价
  - 例: "超时8秒" vs "超时8s"    → format_variant（单位等价）
  - 例: "超时8秒" vs "超时8000ms" → format_variant（数量等价,单位等价）
  - 例: "超时8秒" vs "超时10秒"  → cross_doc_contradiction（数量不等）

**枚举差异**: 元素集合是否相同
  - 例: "分为A/B/C" vs "分为A/B"           → cross_doc_contradiction（少元素,真矛盾）
  - 例: "分为A/B/C" vs "包括A、B、C"       → format_variant（语义一致,仅措辞差异）
  - 例: "状态: 已上线" vs "状态: 已发布"   → 倾向 semantic_overlap（同义需团队确认）

**版本/年代证据**: 旧值出现在标注"v1"/"旧版"/"已废弃"的文档 → deprecated_old

## 冲突规则

- subject: {subject}
- predicate: {predicate}
- 旧值（来源 {old_source}）: {old_value}
- 新值（来源 {new_source}）: {new_value}

只输出 JSON,不要任何额外文字:
{{"category":"...","suggestion":"...","reasoning":"<50字以内>"}}
"""


def classify_canon_conflicts(rules: list[dict], top_k: int = 50) -> list[dict]:
    """对法典中的冲突规则用 LLM 做分类与建议。

    冲突机制：当新事实与既有规则冲突时，既有规则被标 status='conflict'，
    并新建一条 status='pending' 的规则（含 conflict_with 指向既有规则）。
    本函数把"既有(旧) vs 新事实"成对取出，喂给 LLM 分类。

    Args:
        rules: 全部法典规则
        top_k: 最多分类多少对（默认 50，避免 LLM 调用过多）

    Returns:
        [{conflict_id, pending_id, subject, old_value, new_value,
          category, suggestion, reasoning}, ...]
    """
    import sys
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from config import LLM_API_SEMAPHORE, get_deepseek_client

    rules_by_id = {r.get("id"): r for r in rules if r.get("id")}
    pairs = []
    for r in rules:
        if r.get("status") != "pending":
            continue
        cw = r.get("conflict_with")
        if not cw or cw not in rules_by_id:
            continue
        old = rules_by_id[cw]
        pairs.append((old, r))
        if len(pairs) >= top_k:
            break

    if not pairs:
        return []

    def _classify_one(old: dict, new: dict) -> dict:
        prompt = _CONFLICT_CLASSIFY_PROMPT.format(
            subject=old.get("subject", ""),
            predicate=old.get("predicate", ""),
            old_source=old.get("source", "?"),
            old_value=old.get("value", ""),
            new_source=new.get("source", "?"),
            new_value=new.get("value", ""),
        )
        base = {
            "conflict_id": old.get("id"),
            "pending_id": new.get("id"),
            "subject": old.get("subject", ""),
            "predicate": old.get("predicate", ""),
            "old_value": old.get("value", ""),
            "new_value": new.get("value", ""),
            "old_source": old.get("source", ""),
            "new_source": new.get("source", ""),
        }
        try:
            client = get_deepseek_client()
            with LLM_API_SEMAPHORE:
                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    max_tokens=200,
                )
            content = resp.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            obj = json.loads(content)
            base.update({
                "category": obj.get("category", "uncategorized"),
                "suggestion": obj.get("suggestion", "manual"),
                "reasoning": obj.get("reasoning", ""),
            })
        except Exception as e:
            base.update({
                "category": "uncategorized",
                "suggestion": "manual",
                "reasoning": f"LLM 调用失败: {e}",
            })
        return base

    print(f"[CANON] 开始分类 {len(pairs)} 对冲突 ...", file=sys.stderr)
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_classify_one, old, new) for old, new in pairs]
        for f in as_completed(futures):
            results.append(f.result())
    print(f"[CANON] 分类完成: {len(results)} 对", file=sys.stderr)

    # 按 category 排序，便于查看
    _order = {
        "deprecated_old": 0, "format_variant": 1, "semantic_overlap": 2,
        "cross_doc_contradiction": 3, "uncategorized": 9,
    }
    results.sort(key=lambda r: _order.get(r["category"], 9))
    return results
