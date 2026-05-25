"""设计事实提取器 — 从文档中提取结构化设计事实。

使用 DeepSeek API 从游戏设计文档中提取数值约束、枚举定义、流程规则、模块依赖等结构化事实。
支持 SHA256 缓存（含 prompt 版本），文档不变且 prompt 未更新则不重新提取。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from config import FACTS_CACHE_DIR, get_deepseek_client, LLM_API_SEMAPHORE

# 每次修改 EXTRACT_PROMPT 时递增，使旧缓存自动失效
PROMPT_VERSION = "v4"

EXTRACT_PROMPT = """\
你是游戏设计文档分析专家。从以下文档内容中提取关键设计事实。

## 事实类型
- constraint：数值约束（如"上限为15"、"初始容量100"、"超时时间8秒"）
- enum：核心枚举定义（如"分为月卡/季卡/年卡"、"状态包括未发布/已上线/已下架"）
- rule：流程规则（如"购买前必须校验VIP等级"、"已发布商品核心字段禁止修改"）
- dependency：模块依赖（如"调用背包系统发放道具"）

## 应提取的（正面示例）
- "VIP等级上限为15" → constraint，有明确数值边界
- "购买前必须校验VIP等级" → rule，违反会导致逻辑错误
- "已发布商品修改仅对新订单生效" → rule，核心业务规则
- "充值商品分为基础商品/活动商品/特殊商城商品" → enum，核心分类定义
- "支付超时判定时间为8秒" → constraint，明确数值约束

## 不应提取的（负面示例）
- "充值记录筛选条件包括玩家ID/订单ID/金额" → 后台筛选条件列举，不是约束规则
- "操作日志筛选条件包括变更时间/变更人" → 后台筛选条件列举
- "商品查询筛选条件包括充值ID/名称/价格ID" → 后台筛选条件列举
- "编辑弹窗商品名支持简中/繁中/日文" → UI展示细节，多语言支持是展示层功能
- "编辑弹窗标签支持简中/繁中/日文" → UI展示细节
- "通用标签配置字段为tag[0]/tag[1]/tag[2]" → 代码实现细节，不是设计约束
- "价格配置通过price_id关联后端配置" → 太模糊，缺少具体约束值
- "多语言配置表包含14行配置" → 数据量描述，随时会变，不是稳定约束
- "导出格式包括Excel/CSV" → 工具功能描述，不影响游戏设计
- "XX时间筛选默认范围为近7天" → 后台默认值，与游戏逻辑无关

## 通用排除模式
以下模式的内容**一律不提取**，即使文档中有明确描述：
- 后台管理系统的筛选条件、搜索条件、查询条件
- 后台页面的列表展示字段、展示顺序
- 多语言支持范围（如"支持简中/繁中/日文"）
- 后台操作的默认值设定（如"默认显示近7天"）

## 判断标准
核心问题：**违反这条规则会导致游戏功能异常或设计错误吗？**
- 是 → 提取
- 否（仅影响后台操作便利性、仅描述UI布局、仅列举字段名、仅描述筛选功能） → 不提取

## 表格处理规则
- 对于定价表、奖励表、数值配置表等：提取**表结构定义**（列名、行数范围），不要逐行提取每个数据
- 示例：定价表包含10档价格 → 提取一条 constraint "充值档位 共有 10档"，而非10条独立事实
- 同类枚举/选项合并为一条，用"/"分隔值

## 要求
- 只提取文档中**明确陈述**的事实，不要推测
- 每条事实必须包含：type, subject, predicate, value, confidence (0-1)
- subject 和 value 均不得为空字符串
- 合并同类项：同一主题的相似事实合并为一条
- 输出纯 JSON 数组，不要包含其他文字

## confidence 评分指南
- 1.0：文档中有明确数字或定义（如"上限为15"）
- 0.9：文档明确描述但无精确数字（如"需要校验等级"）
- 0.8：从上下文可靠推断（如表格结构隐含的约束）
- 0.7：文档提及但表述模糊（如"大约"、"建议"）
- 0.7 以下：不提取

## 示例输出
[
  {"type": "constraint", "subject": "VIP等级", "predicate": "上限为", "value": "15", "confidence": 1.0},
  {"type": "enum", "subject": "充值商品", "predicate": "分为", "value": "月卡/季卡/年卡", "confidence": 0.9},
  {"type": "rule", "subject": "已发布商品修改", "predicate": "规则为", "value": "仅对新订单生效，核心字段禁止修改", "confidence": 1.0}
]
"""

# 按文档类型附加提示，帮助 LLM 聚焦重点
DOC_TYPE_HINTS = {
    "需求分析文档": "\n\n## 文档类型提示\n本文档是需求分析文档，重点提取：业务规则、数值约束、流程要求。忽略需求背景描述。",
    "后端设计文档": "\n\n## 文档类型提示\n本文档是后端设计文档，重点提取：数据约束、接口规则、配置结构定义。忽略纯代码实现细节（如字段名、API路径）。",
    "前端设计文档": "\n\n## 文档类型提示\n本文档是前端设计文档，重点提取：交互规则、显示约束。忽略纯UI布局描述和样式细节。",
    "后台设计文档": "\n\n## 文档类型提示\n本文档是后台设计文档，重点提取：操作权限规则、数据校验规则。忽略后台管理界面的UI描述和筛选条件列举。",
    "功能概述": "\n\n## 文档类型提示\n本文档是功能概述，重点提取：核心业务规则、分类定义、流程约束。忽略功能介绍性描述。",
}


@dataclass
class DesignFact:
    type: str
    subject: str
    predicate: str
    value: str
    confidence: float

    def to_text(self) -> str:
        return f"{self.subject} {self.predicate} {self.value}"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "confidence": self.confidence,
        }

    @staticmethod
    def from_dict(d: dict) -> "DesignFact":
        return DesignFact(
            type=d.get("type", ""),
            subject=d.get("subject", ""),
            predicate=d.get("predicate", ""),
            value=d.get("value", ""),
            confidence=d.get("confidence", 0.5),
        )


def _cache_key(content: str, doc_type: str = "其他") -> str:
    """生成缓存键：包含 prompt 版本和文档类型，确保 prompt 变更后旧缓存失效。"""
    key_material = f"{PROMPT_VERSION}:{doc_type}:{content}"
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:16]


def _get_cached_facts(key: str) -> list[DesignFact] | None:
    cache_file = FACTS_CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return [DesignFact.from_dict(d) for d in data]
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _put_cached_facts(key: str, facts: list[DesignFact]) -> None:
    try:
        FACTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = [f.to_dict() for f in facts]
        (FACTS_CACHE_DIR / f"{key}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # 缓存写入失败不影响功能


import re as _re

# 硬规则排除模式：LLM 负面示例不够可靠时的兜底过滤
_LOW_VALUE_PATTERNS = [
    # 后台筛选/查询条件
    _re.compile(r'筛选|搜索条件|查询条件', _re.IGNORECASE),
    # 多语言支持描述
    _re.compile(r'支持语言|支持.*简中.*繁中|简中/繁中', _re.IGNORECASE),
    # 列表展示字段
    _re.compile(r'列表展示|展示字段|页面展示', _re.IGNORECASE),
    # 默认显示范围（后台默认值）
    _re.compile(r'默认(显示|展示|范围).*近\d+天', _re.IGNORECASE),
]


def _is_low_value_pattern(fact: DesignFact) -> bool:
    """检查事实是否匹配已知的低价值模式。"""
    full_text = f"{fact.subject} {fact.predicate} {fact.value}"
    return any(p.search(full_text) for p in _LOW_VALUE_PATTERNS)


def extract_facts(text: str, doc_type: str = "其他") -> list[DesignFact]:
    """从文本中提取结构化设计事实。

    Args:
        text: 文档文本内容
        doc_type: 文档类型（用于选择提取策略和缓存隔离）
    """
    if not text or not text.strip():
        return []

    key = _cache_key(text, doc_type)
    cached = _get_cached_facts(key)
    if cached is not None:
        return cached

    try:
        # 构造 system prompt，附加文档类型提示
        system_prompt = EXTRACT_PROMPT + DOC_TYPE_HINTS.get(doc_type, "")

        client = get_deepseek_client()
        with LLM_API_SEMAPHORE:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text[:8000]},
                ],
                temperature=0.1,
                max_tokens=4096,
            )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(content)

        facts = [DesignFact.from_dict(d) for d in data if isinstance(d, dict)]

        # 后置过滤：移除空值、空主题、低置信度事实
        facts = [
            f for f in facts
            if f.value and f.value.strip()
            and f.subject and f.subject.strip()
            and f.confidence >= 0.7
        ]

        # 硬规则过滤：移除 LLM 仍然提取的已知低价值模式
        facts = [f for f in facts if not _is_low_value_pattern(f)]

        # LLM 成功返回：无论结果是否为空都写入缓存（负缓存避免重复调用）
        _put_cached_facts(key, facts)
        return facts

    except Exception as e:
        # API 故障（网络错误、内容审查等）：不写缓存，下次重试
        print(f"[WARN] 事实提取失败（不缓存）: {e}")
        return []


def _extract_one_chunk(chunk: dict) -> list[DesignFact]:
    """提取单个 chunk 的事实（供并行调用）。"""
    heading_chain = chunk.get("heading_chain", "")
    context_prefix = f"[所在章节: {heading_chain}]\n\n" if heading_chain else ""
    text_for_extraction = context_prefix + chunk["text"]
    doc_type = chunk.get("doc_type", "其他")
    return extract_facts(text_for_extraction, doc_type=doc_type)


def extract_facts_from_chunks(chunks: list[dict], max_workers: int = 8) -> list[DesignFact]:
    """从 doc_parser 产出的 chunks 中并行提取事实，然后合并去重。

    替代旧的 text[:8000] 截断方式，确保长文档的全部内容都被处理。
    使用 ThreadPoolExecutor 并行调用 API，受全局 LLM_API_SEMAPHORE 控制并发数。

    Args:
        chunks: doc_parser.parse_document() 返回的 chunk 列表，
                每个 chunk 包含 text, heading_chain, section, doc_type 等字段
        max_workers: 最大并行工作线程数，默认 8

    Returns:
        去重后的事实列表
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_facts: list[DesignFact] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_extract_one_chunk, c): c for c in chunks}
        for future in as_completed(futures):
            try:
                chunk_facts = future.result()
                all_facts.extend(chunk_facts)
            except Exception as e:
                print(f"[WARN] chunk 事实提取失败: {e}")

    # 跨块去重：相同 (type, subject, predicate, value) 只保留 confidence 最高的
    dedup_map: dict[tuple, DesignFact] = {}
    for f in all_facts:
        key = (f.type, f.subject, f.predicate, f.value)
        if key not in dedup_map or f.confidence > dedup_map[key].confidence:
            dedup_map[key] = f

    return list(dedup_map.values())
