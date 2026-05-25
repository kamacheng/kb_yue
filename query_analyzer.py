"""查询分析器 — 查询分解、实体识别。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class QueryType(Enum):
    PRECISE = "precise"
    EXPLORE = "explore"
    RELATION = "relation"


@dataclass
class SubQuery:
    text: str
    entities: list[str] = field(default_factory=list)
    intent: QueryType = QueryType.EXPLORE
    original_query: str = ""


# ID/代号正则模式
# 注意：\b 在中文字符边界处无效，使用负向环视替代
_ENTITY_PATTERNS = [
    r'(?<![A-Za-z])[A-Z]\d{2,4}(?![A-Za-z0-9])',      # P01, A001
    r'(?<![A-Za-z])[A-Z]{2,5}_\d{2,5}(?![A-Za-z0-9_])',  # SKL_001, ITM_01
    r'"([^"]+)"',                                          # "引号内容"
    r'\u201c([^\u201d]+)\u201d',                          # "中文引号内容"
]


def extract_entities(
    query: str,
    module_dict: list[str] | None = None,
) -> list[str]:
    """从查询中提取 ID/代号/专有名词。"""
    entities: list[str] = []

    for pattern in _ENTITY_PATTERNS:
        for match in re.finditer(pattern, query):
            entity = match.group(1) if match.lastindex else match.group(0)
            if entity and entity not in entities:
                entities.append(entity)

    if module_dict:
        for module in module_dict:
            if module in query and module not in entities:
                entities.append(module)

    return entities


# 对比词列表（命中即分解）
_COMPARISON_KEYWORDS = ("对比", "区别", "vs", "VS", "比较", "还是", "和", "与")

# 复合句分隔符（长查询时用，过短时易误切）
_COMPOSITE_SEPARATORS = ("；", ";", "另外", "还有", "另一个", "以及")

# 触发分解的最小长度（短查询单意图概率高）
_MIN_DECOMPOSE_LEN = 20


def should_decompose(query: str) -> bool:
    """判断查询是否需要分解。

    规则：
        - 含明确对比词 → 总是分解
        - 长度 ≥ 30 且含复合句分隔符 → 分解
    """
    for kw in _COMPARISON_KEYWORDS:
        if kw in query:
            return True
    if len(query) >= _MIN_DECOMPOSE_LEN:
        for sep in _COMPOSITE_SEPARATORS:
            if sep in query:
                return True
    return False


def _split_by_separator(query: str, separators: tuple[str, ...]) -> list[str] | None:
    """按分隔符切分查询，返回 ≥2 个有效子句则成功，否则 None。"""
    for sep in separators:
        if sep not in query:
            continue
        parts = [p.strip() for p in query.split(sep) if len(p.strip()) > 1]
        if len(parts) >= 2:
            return parts
    return None


def decompose_query_local(query: str) -> list[SubQuery]:
    """本地规则分解（不调用 LLM）。

    优先按对比词二分（保留语义对仗），其次按复合句分隔符切分。
    """
    if not should_decompose(query):
        return [SubQuery(text=query, original_query=query)]

    # 1. 对比词：二分（区分左右）
    for kw in _COMPARISON_KEYWORDS:
        if kw in query:
            parts = query.split(kw, 1)
            if len(parts) == 2 and len(parts[0].strip()) > 1 and len(parts[1].strip()) > 1:
                return [
                    SubQuery(text=parts[0].strip(), original_query=query),
                    SubQuery(text=parts[1].strip(), original_query=query),
                ]

    # 2. 复合句分隔符：N 段
    parts = _split_by_separator(query, _COMPOSITE_SEPARATORS)
    if parts:
        return [SubQuery(text=p, original_query=query) for p in parts]

    return [SubQuery(text=query, original_query=query)]


def decompose_query_llm(query: str) -> list[SubQuery]:
    """LLM 分解（复杂查询）。失败时回退到本地分解。"""
    from config import get_deepseek_client

    try:
        client = get_deepseek_client()
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": (
                    "你是查询分解助手。将用户的复杂查询拆分为 2-4 个独立的子查询，"
                    "每个子查询可以独立搜索。输出纯 JSON 数组，每个元素是一个子查询字符串。"
                    "如果查询已经足够简单，返回只包含原查询的数组。"
                )},
                {"role": "user", "content": query},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        import json
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        texts = json.loads(content)
        if isinstance(texts, list) and texts:
            return [SubQuery(text=t, original_query=query) for t in texts]
    except Exception as e:
        import sys
        print(f"[WARN] LLM 查询分解失败 ({e})，使用本地分解", file=sys.stderr)

    return decompose_query_local(query)
