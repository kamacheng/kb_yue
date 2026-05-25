"""游戏设计知识库 MCP Server — 提供语义搜索、文档管理等工具。

支持混合搜索、查询重写、邻近上下文、模块过滤、增量索引、文件监听。
"""

import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# 确保项目目录在 path 中
sys.path.insert(0, str(Path(__file__).parent))

from indexer import (
    search, list_modules, get_module_relations, index_single,
    rebuild_all, incremental_rebuild, KB_DIR, _invalidate_bm25,
    _get_facts_store, get_recent_changes, compute_processing_status,
    cleanup_orphans,
)
from consistency_checker import check_consistency, audit_facts
from canon_manager import CanonManager, CanonRule
from facts_store import FactsStore


mcp = FastMCP(
    "kb_mcp",
    instructions="""你是一个游戏设计知识库助手。以下场景应主动调用知识库工具：

1. **用户询问游戏设计相关内容** → kb_search（target="docs"搜文档，target="facts"搜事实）
2. **用户想查看完整文档** → 先 kb_list_modules 找到文档路径，再 kb_get_document 获取全文
3. **用户编写新设计内容** → kb_check（mode="full"双层检查，mode="compliance"仅法典，mode="consistency"仅事实）
4. **用户想了解模块关系** → kb_get_module_relations 或 kb_get_related_designs
5. **用户提到"审计"、"体检"、"自查"** → kb_audit
6. **用户提到"法典"、"规则"** → kb_canon（action="get"查看/action="status"状态/action="export"导出/action="sync"同步）
7. **用户新增或修改文档** → kb_index（mode="incremental"增量/mode="full"全量/mode="single"单文件）
8. **用户说"统计"、"最近变更"、"处理状态"、"哪些没索引"** → kb_overview（view="stats"/"changes"/"gaps"）
9. **用户起草设计文档** → kb_draft_assist

注意事项：
- 这是游戏设计领域的知识库，只包含游戏策划文档
- 搜索结果中的 highlight 字段包含关键词高亮版本，优先展示给用户
- 如果搜索无结果，建议用户换个说法或尝试 rewrite=True 扩展查询
""",
)


# ---------- 格式化工具函数 ----------

def _short_path(path: str) -> str:
    """缩短文件路径，只保留模块/文件名部分。"""
    try:
        p = Path(path)
        parts = p.parts
        # 找到 md_file 后面的部分
        for i, part in enumerate(parts):
            if part == "md_file" and i + 1 < len(parts):
                return "/".join(parts[i + 1:])
        # fallback: 最后两级
        if len(parts) >= 2:
            return "/".join(parts[-2:])
        return p.name
    except Exception:
        return path


def _get_conflict_context(rule: dict, max_chars: int = 200) -> str | None:
    """从 chunks collection 中检索冲突规则的原文上下文片段。"""
    try:
        from embedding import get_collection
        collection = get_collection()
        if collection.count() == 0:
            return None

        source = rule.get("source", "")
        subject = rule.get("subject", "")
        value = rule.get("value", "")
        query_text = f"{subject} {rule.get('predicate', '')} {value}"

        # 在同源文件中搜索
        params = {
            "query_texts": [query_text],
            "n_results": 1,
            "include": ["documents"],
        }
        if source and source != "manual_sync" and source != "unknown":
            params["where"] = {"source": source}

        results = collection.query(**params)
        if results["documents"] and results["documents"][0]:
            doc = results["documents"][0][0]
            # 在原文中定位包含 subject 或 value 的段落
            for para in doc.split("\n"):
                if subject in para or (value and value in para):
                    snippet = para.strip()
                    if len(snippet) > max_chars:
                        snippet = snippet[:max_chars] + "..."
                    return snippet
            # 未找到精确段落，返回文档开头
            snippet = doc.strip()[:max_chars]
            if len(doc.strip()) > max_chars:
                snippet += "..."
            return snippet
    except Exception:
        pass
    return None


def _fmt_search_results(query: str, results: list[dict]) -> str:
    """格式化文档搜索结果。"""
    if not results:
        return f"搜索「{query}」未找到相关文档。\n建议换个说法或使用 rewrite=True 扩展查询。"

    lines = [f"搜索「{query}」找到 {len(results)} 条结果:\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"━━━ 结果 {i} (相关度: {r.get('score', 0):.2f}) ━━━")
        lines.append(f"来源: {_short_path(r.get('source', ''))}")
        lines.append(f"模块: {r.get('module', '未知')} | 类型: {r.get('doc_type', '未知')}")
        lines.append(f"章节: {r.get('section', '无')}")
        # 优先展示 highlight，其次 text
        text = r.get("highlight", r.get("text", ""))
        if text:
            # 截断过长文本
            if len(text) > 500:
                text = text[:500] + "..."
            lines.append(text)
        # 上下文
        ctx = r.get("context")
        if ctx:
            if ctx.get("before"):
                lines.append(f"[上文] {ctx['before'][:200]}...")
            if ctx.get("after"):
                lines.append(f"[下文] {ctx['after'][:200]}...")
        lines.append("")
    return "\n".join(lines)


def _fmt_search_facts(query: str, results: list[dict]) -> str:
    """格式化事实搜索结果。"""
    if not results:
        return f"搜索「{query}」未找到相关事实。"

    lines = [f"搜索「{query}」找到 {len(results)} 条事实:\n"]
    for i, r in enumerate(results, 1):
        type_tag = r.get("type", "?")
        subject = r.get("subject", "")
        predicate = r.get("predicate", "")
        value = r.get("value", "")
        source = _short_path(r.get("source", ""))
        conf = r.get("confidence", 0)
        lines.append(f"{i}. [{type_tag}] {subject} {predicate} {value}")
        lines.append(f"   来源: {source} | 置信度: {conf}")
    return "\n".join(lines)


def _fmt_modules(data: dict) -> str:
    """格式化模块列表。"""
    modules = data.get("modules", {})
    total = data.get("total_docs", 0)
    lines = [f"知识库共 {len(modules)} 个模块，{total} 篇文档:\n"]
    for mod_name, docs in modules.items():
        lines.append(f"  {mod_name} ({len(docs)}篇)")
        for doc in docs:
            lines.append(f"    - {_short_path(doc)}")
    return "\n".join(lines)


def _fmt_cleanup_result(result: dict) -> str:
    """格式化孤儿清理结果。"""
    is_dry = result.get("dry_run", True)
    md_orphans = result.get("orphan_md", [])
    idx_orphans = result.get("orphan_index", [])

    if not md_orphans and not idx_orphans:
        return "无孤儿数据，无需清理。"

    lines = ["[DRY RUN] 仅列出待删项，未执行删除：" if is_dry else "已执行清理："]
    lines.append("")

    if md_orphans:
        lines.append(f"orphan_md ({len(md_orphans)} 项)：")
        for o in md_orphans[:20]:
            lines.append(f"  - {o['dst']}")
            if not is_dry and o.get("actions"):
                lines.append(f"      actions: {', '.join(o['actions'])}")
        if len(md_orphans) > 20:
            lines.append(f"  ... 还有 {len(md_orphans) - 20} 项")
        lines.append("")

    if idx_orphans:
        lines.append(f"orphan_index ({len(idx_orphans)} 项)：")
        for o in idx_orphans[:20]:
            lines.append(f"  - {o['dst']}")
            if not is_dry and o.get("actions"):
                lines.append(f"      actions: {', '.join(o['actions'])}")
        if len(idx_orphans) > 20:
            lines.append(f"  ... 还有 {len(idx_orphans) - 20} 项")
        lines.append("")

    if is_dry:
        lines.append("确认无误后传 confirm=True 执行删除（destructive）。")
    else:
        lines.append(
            f"已删除: 文件 {result.get('deleted_files', 0)}, "
            f"chunks {result.get('deleted_chunks', 0)}, "
            f"facts {result.get('deleted_facts', 0)}, "
            f"废弃规则 {result.get('deprecated_rules', 0)}"
        )
        if result.get("errors"):
            lines.append(f"错误 {len(result['errors'])} 条:")
            for e in result["errors"][:5]:
                lines.append(f"  - {e}")

    return "\n".join(lines)


def _fmt_index_result(mode: str, result: dict) -> str:
    """格式化索引结果。"""
    status = result.get("status", "unknown")
    if status == "error":
        return f"索引失败: {result.get('message', '未知错误')}"

    lines = []
    mode_labels = {"full": "全量重建", "incremental": "增量更新", "single": "单文件索引"}
    lines.append(f"{mode_labels.get(mode, mode)}完成")

    if mode == "single":
        lines.append(f"  文件: {_short_path(result.get('file', ''))}")
        lines.append(f"  Chunks: {result.get('chunks', 0)}个")
    else:
        if "total_files" in result:
            lines.append(f"  文件: {result['total_files']}个")
        elif "indexed" in result:
            lines.append(f"  索引: {result['indexed']}个文件")
            if result.get("unchanged"):
                lines.append(f"  未变: {result['unchanged']}个文件")
            if result.get("removed"):
                lines.append(f"  移除: {result['removed']}个文件")
        if result.get("total_chunks"):
            lines.append(f"  Chunks: {result['total_chunks']}个")
        if result.get("xlsx_converted"):
            lines.append(f"  XLSX转换: {result['xlsx_converted']}个")
        if result.get("md_synced"):
            lines.append(f"  MD同步: {result['md_synced']}个")

    # 法典更新
    canon = result.get("canon_updates")
    if canon:
        lines.append(f"  法典: 新增{canon.get('new_rules', 0)}条规则, "
                      f"冲突{canon.get('conflicts_detected', 0)}条, "
                      f"过滤{canon.get('skipped_low_value', 0)}条低价值")

    # 孤儿清理（增量更新时自动执行）
    cleanup = result.get("cleanup")
    if cleanup and (cleanup.get("deleted_files") or cleanup.get("deleted_chunks")
                    or cleanup.get("deleted_facts") or cleanup.get("deprecated_rules")):
        lines.append(f"  孤儿清理: 文件{cleanup.get('deleted_files', 0)}个, "
                      f"chunks{cleanup.get('deleted_chunks', 0)}, "
                      f"facts{cleanup.get('deleted_facts', 0)}, "
                      f"废弃规则{cleanup.get('deprecated_rules', 0)}")
        for it in cleanup.get("orphan_md", [])[:5]:
            lines.append(f"    - {it.get('dst', '?')}")
        for it in cleanup.get("orphan_index", [])[:5]:
            lines.append(f"    - {it.get('dst', '?')} (索引孤儿)")

    # 计时
    timings = result.get("timings")
    if timings:
        total = timings.get("total", 0)
        lines.append(f"  耗时: {total}秒")
        # 主要阶段耗时
        detail_parts = []
        for key, label in [("parse", "解析"), ("embedding", "嵌入"),
                           ("fact_extraction", "事实提取"), ("canon_sync", "法典同步")]:
            if key in timings:
                detail_parts.append(f"{label}{timings[key]}s")
        if detail_parts:
            lines.append(f"  明细: {' | '.join(detail_parts)}")

    # 错误
    errs = result.get("errors", [])
    if errs:
        lines.append(f"  错误: {len(errs)}个")
        for e in errs[:3]:
            lines.append(f"    - {e.get('file', '?')}: {e.get('error', '?')}")

    return "\n".join(lines)


def _fmt_canon_rules(data: dict) -> str:
    """格式化法典规则列表。"""
    rules = data.get("rules", [])
    count = data.get("count", len(rules))
    if not rules:
        return "法典中无匹配的规则。"

    lines = [f"法典规则 ({count}条):\n"]
    for r in rules:
        status_icon = {"active": "●", "conflict": "⚠", "deprecated": "○",
                       "pending": "◌", "needs_review": "?"}.get(r.get("status", ""), "·")
        priority = r.get("priority", "normal")
        type_tag = r.get("type", "rule")
        subject = r.get("subject", "")
        predicate = r.get("predicate", "")
        value = r.get("value", "")
        lines.append(f"{status_icon} [{type_tag}|{priority}] {subject} {predicate} {value}")
        source = r.get("source", "")
        rule_id = r.get("id", "")
        if source or rule_id:
            detail = f"   ID: {rule_id}" if rule_id else ""
            if source:
                detail += f" | 来源: {_short_path(source)}"
            lines.append(detail)
    return "\n".join(lines)


_CATEGORY_LABEL = {
    "deprecated_old":         "🗑 旧值过时",
    "format_variant":         "📝 表述差异",
    "semantic_overlap":       "🔀 语义重叠",
    "cross_doc_contradiction": "⚠ 跨文档真矛盾",
    "uncategorized":          "❓ 无法判断",
}


def _fmt_canon_classify(results: list[dict]) -> str:
    """格式化冲突分类结果。"""
    if not results:
        return "无 pending 冲突可分类，或法典中没有冲突。"

    grouped: dict[str, list[dict]] = {}
    for r in results:
        grouped.setdefault(r["category"], []).append(r)

    lines = [f"冲突分类完成（共 {len(results)} 对）:\n"]
    for cat, label in _CATEGORY_LABEL.items():
        bucket = grouped.get(cat, [])
        if not bucket:
            continue
        lines.append(f"{label} ({len(bucket)}):")
        for r in bucket[:10]:
            lines.append(
                f"  · {r['subject']} {r['predicate']}  "
                f"[{r['suggestion']}]"
            )
            lines.append(f"      旧: {r['old_value'][:60]}")
            lines.append(f"      新: {r['new_value'][:60]}")
            lines.append(f"      理由: {r['reasoning']}")
            lines.append(f"      conflict_id={r['conflict_id']} pending_id={r['pending_id']}")
        if len(bucket) > 10:
            lines.append(f"  ... 还有 {len(bucket) - 10} 条")
        lines.append("")

    lines.append("处理建议含义: keep_new(用新值替代旧)/keep_old(废弃新)/merge(合并)/manual(人工)")
    lines.append("可用 kb_canon action=resolve_conflict rule_id=<conflict_id> new_value=... 应用决策。")
    return "\n".join(lines)


def _fmt_canon_status(data: dict) -> str:
    """格式化法典状态。"""
    lines = ["法典状态:"]
    lines.append(f"  总规则: {data.get('total_rules', 0)} | "
                 f"活跃: {data.get('active_rules', '?')} | "
                 f"已废弃: {data.get('deprecated_rules', '?')}")

    conflicts = data.get("conflicts", [])
    pending = data.get("pending", [])
    needs_review = data.get("needs_review", [])

    # 构建 conflict_with 映射：冲突规则ID -> pending规则
    pending_by_conflict = {}
    for r in pending:
        cw = r.get("conflict_with")
        if cw:
            pending_by_conflict[cw] = r

    if conflicts:
        lines.append(f"\n  冲突 ({len(conflicts)}条):")
        for r in conflicts[:5]:
            rid = r.get('id', '')
            subject = r.get('subject', '')
            predicate = r.get('predicate', '')
            old_value = r.get('value', '')
            lines.append(f"    ⚠ {subject} {predicate}")
            lines.append(f"      旧值: {old_value}")
            lines.append(f"      ID: {rid} | 来源: {_short_path(r.get('source', ''))}")

            # 展示对应的 pending 新值
            pr = pending_by_conflict.get(rid)
            if pr:
                lines.append(f"      新值: {pr.get('value', '')} (来源: {_short_path(pr.get('source', ''))})")

            # 原文上下文
            ctx = _get_conflict_context(r)
            if ctx:
                lines.append(f"      原文: {ctx}")

    # 展示无配对的 pending 规则
    orphan_pending = [r for r in pending if r.get("conflict_with") not in [c.get("id") for c in conflicts]]
    if orphan_pending:
        lines.append(f"\n  待确认 ({len(orphan_pending)}条):")
        for r in orphan_pending[:5]:
            lines.append(f"    ◌ {r.get('subject', '')} {r.get('predicate', '')} {r.get('value', '')}")

    if needs_review:
        lines.append(f"\n  需审核 ({len(needs_review)}条):")
        for r in needs_review[:5]:
            lines.append(f"    ? {r.get('subject', '')} {r.get('predicate', '')} {r.get('value', '')}")

    if not conflicts and not pending and not needs_review:
        lines.append("  无冲突、无待确认、无需审核项。")
    return "\n".join(lines)


def _fmt_overview_stats(data: dict) -> str:
    """格式化知识库统计。"""
    lines = ["知识库统计:"]
    lines.append(f"  文档: {data.get('documents', 0)}篇 | "
                 f"Chunks: {data.get('chunks', 0)}个 | "
                 f"模块: {data.get('module_count', 0)}个")

    modules = data.get("modules", [])
    if modules:
        lines.append(f"  模块列表: {', '.join(modules)}")

    canon = data.get("canon", {})
    if canon:
        lines.append(f"  法典: {canon.get('total_rules', 0)}条规则 | "
                      f"活跃{canon.get('active_rules', '?')} | "
                      f"冲突{canon.get('conflict_rules', 0)}")
    return "\n".join(lines)


def _fmt_overview_changes(data: dict) -> str:
    """格式化最近变更。"""
    changes = data.get("changes", [])
    days = data.get("days", 7)
    if not changes:
        return f"最近 {days} 天无文档变更。"

    lines = [f"最近 {days} 天变更了 {len(changes)} 篇文档:\n"]
    for c in changes:
        lines.append(f"  - {_short_path(c.get('file', ''))}")
        lines.append(f"    索引时间: {c.get('indexed_at', '?')} | 大小: {c.get('size', 0)} bytes")
    return "\n".join(lines)


_STATUS_LABEL = {
    "ok":             ("✅", "已索引且最新"),
    "outdated_src":   ("🔄", "原始已修订待重建"),
    "outdated_index": ("⚠", "已转换但索引过期"),
    "unprocessed":    ("❌", "未处理"),
    "orphan_md":      ("🧩", "md 孤儿（无原始源）"),
    "orphan_index":   ("🚫", "索引孤儿（文件已删）"),
}


def _humanize_age(seconds: float) -> str:
    """把秒数格式化为人类友好的描述。"""
    seconds = abs(seconds)
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟"
    if seconds < 86400:
        return f"{int(seconds // 3600)}小时"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)}天"
    if seconds < 86400 * 365:
        return f"{int(seconds // (86400 * 30))}个月"
    return f"{int(seconds // (86400 * 365))}年"


def _format_drift(item: dict) -> str:
    """根据状态返回时间偏差描述（如"src 比 dst 新 3 天"）；不适用返回空串。"""
    status = item.get("status")
    src_mtime = item.get("src_mtime")
    dst_mtime = item.get("dst_mtime")
    indexed_mtime = item.get("indexed_mtime")

    if status == "outdated_src" and src_mtime and dst_mtime:
        return f"  (源比目标新 {_humanize_age(src_mtime - dst_mtime)})"
    if status == "outdated_index" and dst_mtime:
        if indexed_mtime is None:
            return "  (索引中无记录)"
        return f"  (目标比索引新 {_humanize_age(dst_mtime - indexed_mtime)})"
    return ""


def _fmt_overview_gaps(data: dict, filter_name: str | None = None, sample: int = 8) -> str:
    """格式化处理状态视图。"""
    summary = data.get("summary", {})
    items = data.get("items", [])
    total_sources = data.get("total_sources", 0)

    if filter_name:
        items = [it for it in items if filter_name in (it.get("src") or "") or filter_name in (it.get("dst") or "")]

    lines = [f"处理状态分析 (源文件 {total_sources} 个):\n"]

    # 汇总
    for status, (icon, label) in _STATUS_LABEL.items():
        cnt = summary.get(status, 0)
        if cnt or status == "ok":
            lines.append(f"  {icon} {label}: {cnt}")
    lines.append("")

    # 按状态分组列出
    grouped: dict[str, list] = {}
    for it in items:
        grouped.setdefault(it["status"], []).append(it)

    has_problem = False
    for status, (icon, label) in _STATUS_LABEL.items():
        if status == "ok":
            continue
        bucket = grouped.get(status, [])
        if not bucket:
            continue
        has_problem = True
        lines.append(f"{icon} {label} ({len(bucket)}):")
        shown = bucket if filter_name else bucket[:sample]
        for it in shown:
            path = it.get("src") or it.get("dst") or "?"
            lines.append(f"    - {path}{_format_drift(it)}")
        if not filter_name and len(bucket) > sample:
            lines.append(f"    ... 还有 {len(bucket) - sample} 个（用 module=<关键字> 查看完整列表）")
        lines.append("")

    # 提示
    if has_problem:
        lines.append("提示: 运行 `kb_index mode=incremental` 可自动重建变更/缺失部分。")
        if summary.get("orphan_md") or summary.get("orphan_index"):
            lines.append("      孤儿文件需手动确认是否删除。")
    else:
        lines.append("✅ 所有源文件均已处理且为最新状态。")

    return "\n".join(lines)


def _fmt_check_result(data: dict) -> str:
    """格式化设计检查结果。"""
    status = data.get("status", "ok")
    mode = data.get("mode", "")
    status_icon = {"ok": "✓", "conflicts_found": "✗", "warnings_found": "⚠"}.get(status, "?")

    lines = [f"检查结果: {status_icon} {status}"]

    # compliance 部分
    comp = data.get("compliance", {})
    if comp:
        c_status = comp.get("status", "ok")
        lines.append(f"\n  法典合规: {c_status}")
        for v in comp.get("violations", [])[:5]:
            lines.append(f"    - 违反: {v.get('rule', '?')}")
            lines.append(f"      详情: {v.get('detail', '?')}")

    # consistency 部分
    cons = data.get("consistency", {})
    if cons:
        c_status = cons.get("status", "ok")
        lines.append(f"\n  事实一致性: {c_status}")
        for c in cons.get("conflicts", [])[:5]:
            lines.append(f"    - 冲突: {c.get('description', '?')}")
            if c.get("existing"):
                lines.append(f"      已有: {c['existing']}")
            if c.get("new"):
                lines.append(f"      新增: {c['new']}")

    suggestions = data.get("suggestions") or comp.get("suggestions") or cons.get("suggestions")
    if suggestions:
        lines.append(f"\n  建议: {suggestions if isinstance(suggestions, str) else '; '.join(str(s) for s in suggestions[:3])}")

    return "\n".join(lines)


def _fmt_audit_result(data: dict) -> str:
    """格式化审计结果。"""
    status = data.get("status", "ok")
    conflicts = data.get("conflicts", [])

    if not conflicts:
        return "审计结果: ✓ 知识库内部一致，未发现矛盾。"

    lines = [f"审计结果: 发现 {len(conflicts)} 处潜在矛盾:\n"]
    for i, c in enumerate(conflicts, 1):
        lines.append(f"  {i}. {c.get('description', '?')}")
        if c.get("fact_a"):
            lines.append(f"     A: {c['fact_a']}")
        if c.get("fact_b"):
            lines.append(f"     B: {c['fact_b']}")
        if c.get("source_a"):
            lines.append(f"     来源A: {_short_path(c['source_a'])}")
        if c.get("source_b"):
            lines.append(f"     来源B: {_short_path(c['source_b'])}")
    return "\n".join(lines)


def _fmt_preview_facts(data: dict) -> str:
    """格式化事实预览。"""
    if "error" in data:
        return f"预览失败: {data['error']}"

    lines = [f"事实预览: {_short_path(data.get('file', ''))}"]
    lines.append(f"  Chunks: {data.get('chunks_count', 0)} | 事实: {data.get('facts_count', 0)}")

    dist = data.get("type_distribution", {})
    if dist:
        dist_str = " | ".join(f"{k}:{v}" for k, v in dist.items())
        lines.append(f"  类型分布: {dist_str}")

    lines.append("")
    for i, f in enumerate(data.get("facts", []), 1):
        type_tag = f.get("type", "?")
        subject = f.get("subject", "")
        predicate = f.get("predicate", "")
        value = f.get("value", "")
        conf = f.get("confidence", 0)
        lines.append(f"  {i}. [{type_tag}] {subject} {predicate} {value} (置信度:{conf})")
    return "\n".join(lines)


def _fmt_relations(data: dict) -> str:
    """格式化模块关系图。"""
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    lines = [f"模块关系图: {len(nodes)} 个模块, {len(edges)} 条引用关系\n"]
    lines.append(f"  模块: {', '.join(nodes)}")
    if edges:
        lines.append("\n  引用关系:")
        for e in edges:
            lines.append(f"    {e.get('from', '?')} → {e.get('to', '?')} (引用: {e.get('ref', '')})")
    else:
        lines.append("  暂无跨模块引用关系。")
    return "\n".join(lines)


def _fmt_related_designs(data: dict) -> str:
    """格式化关联设计摘要。"""
    module = data.get("module", "?")
    related = data.get("related", [])
    own_count = data.get("own_facts_count", 0)

    lines = [f"模块「{module}」关联设计摘要:"]
    lines.append(f"  本模块事实: {own_count}条")

    own_facts = data.get("own_key_facts", [])
    for f in own_facts[:5]:
        if isinstance(f, dict):
            lines.append(f"    - [{f.get('type', '')}] {f.get('subject', '')} {f.get('predicate', '')} {f.get('value', '')}")

    if related:
        lines.append(f"\n  关联模块 ({len(related)}个):")
        for rel in related:
            rmod = rel.get("module", "?")
            rcount = rel.get("facts_count", 0)
            lines.append(f"    {rmod} ({rcount}条事实)")
            for f in rel.get("key_facts", [])[:3]:
                if isinstance(f, dict):
                    lines.append(f"      - [{f.get('type', '')}] {f.get('subject', '')} {f.get('predicate', '')} {f.get('value', '')}")
    else:
        lines.append("  无关联模块。")
    return "\n".join(lines)


def _fmt_draft_assist(data: dict) -> str:
    """格式化起草辅助素材包。"""
    topic = data.get("topic", "?")
    style = data.get("style", "")

    lines = [f"起草素材包: 「{topic}」"]
    if style:
        lines.append(f"  写作风格: {style}")

    # 相关文档
    related = data.get("related_docs", [])
    lines.append(f"\n  相关文档 ({len(related)}条):")
    for i, r in enumerate(related[:5], 1):
        lines.append(f"    {i}. {_short_path(r.get('source', ''))} | {r.get('section', '')}")
        text = r.get("text", "")
        if text:
            lines.append(f"       {text[:150]}...")

    # 法典约束
    rules = data.get("canon_rules", [])
    critical = [r for r in rules if r.get("priority") == "critical"]
    lines.append(f"\n  法典约束 ({len(rules)}条, 其中{len(critical)}条关键):")
    for r in critical[:5]:
        lines.append(f"    ● {r.get('subject', '')} {r.get('predicate', '')} {r.get('value', '')}")

    # 指导
    guidance = data.get("guidance", "")
    if guidance:
        lines.append(f"\n  起草指导:\n{guidance}")

    return "\n".join(lines)


# ---------- Tools ----------

@mcp.tool(
    name="kb_search",
    annotations={
        "title": "搜索知识库",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def search_kb(
    query: str,
    target: str = "docs",
    top_k: int = 5,
    include_context: bool = False,
    module: str | None = None,
    doc_type: str | None = None,
    fact_type: str | None = None,
    rewrite: bool = False,
) -> str:
    """在游戏设计知识库中进行语义搜索,返回最相关的文档片段。

    用于查找与查询语义相近的设计文档内容,支持中文搜索。
    返回每个结果的摘要文本、所属模块、文档类型、来源文件和相关度评分。

    Args:
        query: 搜索查询文本,例如 "购买资格校验"、"弹窗规范"
        target: 搜索目标，"docs"搜索文档（默认），"facts"搜索事实库
        top_k: 返回结果数量,默认 5,最大 20
        include_context: 是否包含前后邻近 chunk 的上下文摘要,默认 False
        module: 按模块名过滤,例如 "充值与付费",默认不过滤
        doc_type: 按文档类型过滤,例如 "需求分析文档",默认不过滤（仅 target="docs"）
        fact_type: 按事实类型过滤 (constraint/enum/rule/dependency),默认不过滤（仅 target="facts"）
        rewrite: 是否使用 LLM 扩展查询同义词以提升召回,默认 False(有 200-500ms 延迟)

    Returns:
        搜索结果列表
    """
    top_k = min(max(top_k, 1), 20)

    if target == "facts":
        store = FactsStore()
        results = store.search_facts(query, top_k=top_k, module=module)
        if fact_type:
            results = [r for r in results if r.get("type") == fact_type]
        return _fmt_search_facts(query, results)

    # target == "docs" (default)
    results = search(
        query,
        top_k=top_k,
        include_context=include_context,
        module=module,
        doc_type=doc_type,
        rewrite=rewrite,
    )

    return _fmt_search_results(query, results)


@mcp.tool(
    name="kb_list_modules",
    annotations={
        "title": "列出知识库模块",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_modules_tool() -> str:
    """列出知识库中所有已索引的模块及其文档清单。

    返回每个模块名及其包含的文档路径列表，以及文档总数。

    Returns:
        模块列表
    """
    result = list_modules()
    return _fmt_modules(result)


@mcp.tool(
    name="kb_get_document",
    annotations={
        "title": "获取文档全文",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_document(path: str) -> str:
    """获取指定文档的全文内容。

    通过文件路径读取文档内容。路径可以是绝对路径，
    也可以是相对于知识库根目录（config.json 中 kb_root 指定的路径）的相对路径。

    Args:
        path: 文档路径，例如 "充值与付费/充值与付费-需求分析文档.md"

    Returns:
        文档全文内容，或错误信息
    """
    p = Path(path)
    if not p.is_absolute():
        p = KB_DIR / p
        if not p.exists():
            p = KB_DIR / "md_file" / path

    if not p.exists():
        return f"错误: 文件不存在 - {p}"

    try:
        content = p.read_text(encoding="utf-8")
        return content
    except Exception as e:
        return f"错误: 读取失败 - {e}"


@mcp.tool(
    name="kb_index",
    annotations={
        "title": "索引管理",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def kb_index_tool(
    mode: str = "incremental",
    path: str | None = None,
    skip_facts: bool = False,
    clear_canon: bool = False,
    confirm: bool = False,
) -> str:
    """管理知识库索引：单文件索引、增量更新、全量重建或清理孤儿。

    Args:
        mode: 索引模式
            - "single": 索引单个文档（需提供 path 参数）
            - "incremental": 增量更新，只处理新增和变更的文件（默认）
            - "full": 全量重建，清空并重新索引所有文档
            - "cleanup": 清理孤儿（md_file 中无源 / index_meta 中文件已删）。
              默认 dry_run，传 confirm=True 才执行删除。
        path: 文档路径（仅 mode="single" 时需要），可以是绝对路径或相对路径
        skip_facts: 是否跳过事实提取（加速索引，默认 False）
        clear_canon: 全量重建时是否同时清空法典（仅 mode="full" 时有效，默认 False）
        confirm: 仅 mode="cleanup" 时使用，True 才真删，否则 dry-run

    Returns:
        索引结果
    """
    if mode == "single":
        if not path:
            return "错误: mode='single' 需要提供 path 参数"
        p = Path(path)
        if not p.is_absolute():
            p = KB_DIR / p
            if not p.exists():
                p = KB_DIR / "md_file" / path
        result = index_single(str(p))
    elif mode == "full":
        result = rebuild_all(full=True, skip_facts=skip_facts, clear_canon=clear_canon)
    elif mode == "cleanup":
        result = cleanup_orphans(dry_run=not confirm)
        return _fmt_cleanup_result(result)
    else:  # incremental
        result = incremental_rebuild(skip_facts=skip_facts)

    return _fmt_index_result(mode, result)


@mcp.tool(
    name="kb_preview_facts",
    annotations={
        "title": "预览事实提取",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def kb_preview_facts_tool(path: str) -> str:
    """预览单个文档的事实提取结果，不写入存储。

    用于在正式索引前审核提取质量，检查是否有误提取或遗漏。

    Args:
        path: 文档路径，可以是绝对路径或相对于知识库根目录的相对路径

    Returns:
        事实预览，包含提取的事实列表和统计信息
    """
    from doc_parser import parse_document
    from fact_extractor import extract_facts_from_chunks

    p = Path(path)
    if not p.is_absolute():
        p = KB_DIR / p
        if not p.exists():
            p = KB_DIR / "md_file" / path

    if not p.exists():
        return f"错误: 文件不存在 - {p}"

    try:
        text = p.read_text(encoding="utf-8")
        chunks = parse_document(str(p), text)
        if not chunks:
            return "错误: 文档解析后无有效内容"

        facts = extract_facts_from_chunks(chunks)

        # 按类型分组统计
        type_counts: dict[str, int] = {}
        for f in facts:
            type_counts[f.type] = type_counts.get(f.type, 0) + 1

        data = {
            "file": str(p),
            "chunks_count": len(chunks),
            "facts_count": len(facts),
            "type_distribution": type_counts,
            "facts": [f.to_dict() for f in facts],
        }
        return _fmt_preview_facts(data)

    except Exception as e:
        return f"错误: 预览失败 - {e}"


@mcp.tool(
    name="kb_get_module_relations",
    annotations={
        "title": "获取模块关系图",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_module_relations_tool() -> str:
    """获取知识库中模块之间的关系图。

    利用文档中的交叉引用（如"调用XX系统"、"依赖XX模块"）构建模块关系网络。

    Returns:
        关系图，包含模块列表和引用关系列表
    """
    result = get_module_relations()
    return _fmt_relations(result)


@mcp.tool(
    name="kb_get_related_designs",
    annotations={
        "title": "获取关联设计",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_related_designs(module: str, depth: int = 1) -> str:
    """获取与指定模块关联的设计摘要。

    根据知识图谱和模块关系，返回直接或间接关联模块的核心设计事实，
    帮助策划在编写新文档时了解相关模块的已有设计。

    Args:
        module: 目标模块名，例如 "充值与付费"
        depth: 关联深度，1=直接关联（默认），2=包含间接关联

    Returns:
        关联设计摘要
    """
    depth = min(max(depth, 1), 3)

    # 获取模块关系图
    relations = get_module_relations()
    edges = json.loads(relations) if isinstance(relations, str) else relations
    edge_list = edges.get("edges", [])

    # BFS 查找关联模块
    related = set()
    frontier = {module}
    for d in range(depth):
        next_frontier = set()
        for edge in edge_list:
            if edge["from"] in frontier:
                next_frontier.add(edge["to"])
            if edge["to"] in frontier:
                next_frontier.add(edge["from"])
        next_frontier -= {module}
        related |= next_frontier
        frontier = next_frontier

    # 获取每个关联模块的设计事实
    store = _get_facts_store()
    data = {"module": module, "related": []}
    for rel_module in sorted(related):
        facts = store.get_facts_by_module(rel_module)
        data["related"].append({
            "module": rel_module,
            "facts_count": len(facts),
            "key_facts": facts[:10],
        })

    # 当前模块的事实
    own_facts = store.get_facts_by_module(module)
    data["own_facts_count"] = len(own_facts)
    data["own_key_facts"] = own_facts[:10]

    return _fmt_related_designs(data)


@mcp.tool(
    name="kb_check",
    annotations={
        "title": "设计检查",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def kb_check_tool(
    content: str,
    module: str | None = None,
    mode: str = "full",
) -> str:
    """检查新设计内容的合规性和一致性。

    支持多种检查模式：法典合规、事实一致性，或双层全量检查。

    Args:
        content: 待检查的新设计内容文本
        module: 所属模块名（可选，缩小检查范围提高准确度）
        mode: 检查模式
            - "full": 先法典合规再事实一致性，合并结果（默认）
            - "compliance": 仅法典合规检查
            - "consistency": 仅事实一致性检查
            - "quick": 快速模式（仅返回相关事实，不做 LLM 对比）

    Returns:
        检查结果
    """
    from consistency_checker import compliance_check

    results = {}

    if mode in ("full", "compliance"):
        mgr = CanonManager()
        store = FactsStore()
        compliance_result = compliance_check(content, canon_mgr=mgr, module=module, store=store)
        if mode == "compliance":
            return _fmt_check_result(compliance_result)
        results["compliance"] = compliance_result

    if mode in ("full", "consistency", "quick"):
        store = _get_facts_store()
        quick = mode == "quick"
        consistency_result = check_consistency(content, store=store, module=module, quick_check=quick)
        if mode in ("consistency", "quick"):
            return _fmt_check_result(consistency_result)
        results["consistency"] = consistency_result

    # full mode: merge results
    merged = {
        "mode": "full",
        "compliance": results.get("compliance", {}),
        "consistency": results.get("consistency", {}),
    }
    c_status = results.get("compliance", {}).get("status", "ok")
    f_status = results.get("consistency", {}).get("status", "ok")
    if "conflict" in c_status or "conflict" in f_status:
        merged["status"] = "conflicts_found"
    elif "warning" in c_status or "warning" in f_status:
        merged["status"] = "warnings_found"
    else:
        merged["status"] = "ok"

    return _fmt_check_result(merged)


@mcp.tool(
    name="kb_audit",
    annotations={
        "title": "审计知识库一致性",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def audit_tool(
    module: str | None = None,
    top_k: int = 20,
) -> str:
    """审计知识库内部的设计一致性，检测不同文档之间的矛盾。

    扫描知识库中所有已提取的设计事实，找出同一主题在不同文档中描述不一致的情况。
    例如：文档A说"VIP等级上限为15"，文档B说"VIP等级上限为20"。

    Args:
        module: 可选，只审计指定模块（不指定则全局扫描）
        top_k: 最多报告多少条矛盾，默认 20

    Returns:
        审计结果
    """
    from indexer import _get_facts_store
    store = _get_facts_store()
    top_k = min(max(top_k, 1), 50)
    result = audit_facts(store=store, module=module, top_k=top_k)
    return _fmt_audit_result(result)


@mcp.tool(
    name="kb_canon",
    annotations={
        "title": "法典管理",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def kb_canon_tool(
    action: str = "get",
    module: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    rule_id: str | None = None,
    new_value: str | None = None,
    reason: str = "",
    top_k: int = 50,
) -> str:
    """管理知识库法典（Canon）：查看、状态、导出、同步、解决冲突、分类冲突、更新规则。

    Args:
        action: 操作类型
            - "get": 读取法典规则，可按 module/status/priority 过滤（默认）
            - "status": 查看法典状态（规则数、冲突数、待确认数）
            - "export": 导出法典为 Markdown 格式
            - "sync": 从事实库同步规则到法典
            - "classify": 用 LLM 把 pending 冲突分类为 deprecated_old / cross_doc_contradiction
              / semantic_overlap / format_variant / uncategorized，并给出处理建议
            - "resolve_conflict": 解决法典冲突（需 rule_id + new_value）
            - "update_value": 更新规则值（需 rule_id + new_value）
            - "deprecate": 废弃规则（需 rule_id）
        module: 按模块过滤（适用于 get/export/sync）
        status: 按状态过滤（适用于 get）
        priority: 按优先级过滤（适用于 get）
        rule_id: 规则 ID（适用于 resolve_conflict/update_value/deprecate）
        new_value: 新值（适用于 resolve_conflict/update_value）
        reason: 变更原因
        top_k: classify 时最多分类多少对冲突（默认 50）

    Returns:
        法典操作结果
    """
    mgr = CanonManager()

    if action == "get":
        rules = mgr.get_rules(module=module, status=status, priority=priority)
        return _fmt_canon_rules({"rules": rules, "count": len(rules)})

    elif action == "status":
        stat = mgr.get_status()
        rules = mgr.get_rules()
        conflicts = [r for r in rules if r.get("status") == "conflict"]
        pending = [r for r in rules if r.get("status") == "pending"]
        needs_review = [r for r in rules if r.get("status") == "needs_review"]
        stat.update({
            "conflicts": conflicts,
            "pending": pending,
            "needs_review": needs_review,
        })
        return _fmt_canon_status(stat)

    elif action == "export":
        md = mgr.export_as_markdown(module=module)
        return md

    elif action == "sync":
        from canon_manager import filter_facts_for_canon
        from fact_extractor import DesignFact

        store = _get_facts_store()
        if module:
            facts_data = store.get_facts_by_module(module)
        else:
            collection = store._collection
            all_data = collection.get(include=["metadatas", "documents"])
            facts_data = all_data.get("metadatas", [])

        facts = [DesignFact.from_dict(f) for f in facts_data if isinstance(f, dict)]
        filtered = filter_facts_for_canon(facts)

        if not filtered:
            return "同步完成: 无符合法典收录标准的新事实。"

        result = mgr.merge_filtered_facts(filtered, source="manual_sync",
                                           module=module or "", trigger="kb_canon")
        return (f"法典同步完成: 新增{result.get('new_rules', 0)}条规则, "
                f"冲突{result.get('conflicts_detected', 0)}条, "
                f"更新{result.get('updated', 0)}条")

    elif action == "classify":
        from canon_manager import classify_canon_conflicts
        rules = mgr.get_rules(module=module)
        results = classify_canon_conflicts(rules, top_k=top_k)
        return _fmt_canon_classify(results)

    elif action == "resolve_conflict":
        if not rule_id or not new_value:
            return "错误: resolve_conflict 需要 rule_id 和 new_value"
        mgr.resolve_conflict(rule_id, action="set_value", new_value=new_value)
        return f"冲突已解决: 规则 {rule_id} 已更新为 \"{new_value}\""

    elif action == "update_value":
        if not rule_id or not new_value:
            return "错误: update_value 需要 rule_id 和 new_value"
        mgr.update_rule(rule_id, new_value, reason)
        return f"规则已更新: {rule_id} → \"{new_value}\""

    elif action == "deprecate":
        if not rule_id:
            return "错误: deprecate 需要 rule_id"
        mgr.deprecate_rule(rule_id)
        return f"规则已废弃: {rule_id}"

    else:
        return f"错误: 未知操作 \"{action}\"，可选: get/status/export/sync/classify/resolve_conflict/update_value/deprecate"


@mcp.tool(
    name="kb_overview",
    annotations={
        "title": "知识库概览",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def kb_overview_tool(
    view: str = "stats",
    module: str | None = None,
    days: int = 7,
) -> str:
    """查看知识库概览信息：统计、最近变更或处理状态。

    Args:
        view: 视图类型
            - "stats": 统计信息（文档数、chunk数、模块数、法典状态）（默认）
            - "changes": 最近变更的文档列表
            - "gaps": 处理状态分析（对比 original_file → md_file → 索引，
              识别未处理/原始已修订/索引过期/孤儿等情况）
        module: 按文件名关键字过滤（适用于 gaps）
        days: 查看最近多少天的变更（适用于 changes），默认 7

    Returns:
        概览信息
    """
    if view == "changes":
        days = min(max(days, 1), 365)
        result = get_recent_changes(days)
        return _fmt_overview_changes(result)

    elif view == "gaps":
        data = compute_processing_status()
        return _fmt_overview_gaps(data, filter_name=module)

    else:  # stats
        from embedding import get_collection
        collection = get_collection()
        facts_store = FactsStore()

        chunk_count = collection.count()
        all_meta = collection.get(include=["metadatas"])
        doc_ids = set(m.get("doc_id", "") for m in all_meta.get("metadatas", []))
        modules = set(m.get("module", "") for m in all_meta.get("metadatas", []))

        try:
            canon_mgr = CanonManager()
            canon_stat = canon_mgr.get_status()
        except Exception:
            canon_stat = {"total_rules": 0}

        data = {
            "documents": len(doc_ids),
            "chunks": chunk_count,
            "modules": sorted(modules - {""}),
            "module_count": len(modules - {""}),
            "canon": canon_stat,
        }
        return _fmt_overview_stats(data)


@mcp.tool(
    name="kb_draft_assist",
    annotations={
        "title": "辅助起草素材收集",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def draft_assist(
    topic: str,
    style: str | None = None,
    module: str | None = None,
) -> str:
    """收集与指定主题相关的素材和法典约束，供 AI 起草设计文档使用。

    该工具不生成文本，只负责检索相关素材和规则。

    Args:
        topic: 起草主题，例如 "冰雪地图副本背景"
        style: 写作风格（可选），例如 "世界观叙事"、"技术文档"
        module: 所属模块（可选），用于缩小检索范围

    Returns:
        素材包，包含 related_docs、canon_rules、guidance
    """
    # 1. 搜索相关文档
    related = search(topic, top_k=10, module=module, include_context=True)

    # 2. 加载法典约束
    canon_mgr = CanonManager()
    canon_rules = canon_mgr.get_rules(module=module, status="active")

    # 3. 组装结果
    data = {
        "topic": topic,
        "style": style,
        "related_docs": related,
        "canon_rules": canon_rules,
        "guidance": _build_guidance(topic, style, canon_rules),
    }
    return _fmt_draft_assist(data)


def _build_guidance(topic: str, style: str | None, canon_rules: list[dict]) -> str:
    """构建起草指导说明。"""
    lines = [f"基于以上素材起草关于「{topic}」的设计时，请注意："]
    if style:
        lines.append(f"- 写作风格：{style}")
    critical = [r for r in canon_rules if r.get("priority") == "critical"]
    if critical:
        lines.append("- 必须遵守以下法典规则：")
        for r in critical[:10]:
            lines.append(f"  - {r['subject']} {r['predicate']} {r['value']}")
    lines.append("- 在末尾标注引用来源")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
