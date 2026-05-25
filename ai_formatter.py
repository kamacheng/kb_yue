"""AI 重排版模块 — 使用 DeepSeek API 将预处理文本转为高质量 Markdown。

支持分块处理、SHA256 缓存、自动重试与降级。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from config import AI_FORMAT_CACHE_DIR, get_deepseek_client
from sheet_preprocessor import CleanedSheet, SheetType

# ---------- 配置 ----------

CACHE_DIR = AI_FORMAT_CACHE_DIR
MAX_TOKENS_PER_CALL = 50000  # 单次 API 调用上限（字符估算 / 2）
MAX_RETRIES = 3
RETRY_DELAYS = [5, 10, 15]

# 每次修改 SYSTEM_PROMPT 时递增，使旧缓存自动失效
_AI_FORMAT_VERSION = "v2"

SYSTEM_PROMPT = """你是一个专业的游戏设计文档排版专家。你的任务是将预处理后的游戏设计文档内容重新排版为高质量的 Markdown 格式。

## 排版规则

### 标题层级
- 用 ## 作为主章节标题
- 用 ### 作为子章节标题
- 用 #### 作为细节标题
- 不要使用 # 一级标题（留给文件标题）

### 内容格式
- 描述性内容使用段落 + 无序列表，不要强制表格化
- 仅当内容确实是结构化数据（有明确的列头和多行同构数据）时才使用表格
- 使用 **加粗** 标注关键词、重要概念、参数名
- 使用 *斜体* 标注附注、备注说明
- 使用 --- 水平线分隔大的章节
- 使用 > 引用块标注特别注意事项或重要提示

### 列表格式
- 主要内容用无序列表 (- )
- 有明确顺序的步骤用有序列表 (1. 2. 3.)
- 子项用缩进表示层级关系

### 严格要求（违反任何一条都是严重错误）
- **严禁编造内容**：只能使用原文中存在的信息
- **严禁省略信息**：原文中的所有内容都必须保留
- **严禁添加总结**：不要在末尾添加任何总结性文字
- **严禁修改任何数值**：原文中的所有数字必须原样保留，不得四舍五入、换算或近似
- **严禁修改名称和术语**：原文中的所有名称、术语、字段名必须完全一致，不得翻译、同义替换或简写
- **严禁调整表格数据顺序**：表格中行与列的顺序必须与原文保持一致
- **严禁合并或拆分表格行**：每一行数据必须独立保留，不得合并同类项
- **严禁删除表格行**：即使内容看起来重复，也必须全部保留
- 保持原文的专业术语不变
- 如果内容是版本记录，保持时间线格式
- 遇到 {{IMG:xxx}} 占位符时，**原样保留**，不要修改或删除

### 错误示例（绝对禁止的操作）
- ❌ 原文 "648.00" → 改为 "648" 或 "约650"（严禁修改数值）
- ❌ 原文有 10 行价格表 → 输出只有 8 行（严禁省略行）
- ❌ 原文顺序 "A, B, C" → 输出变为 "C, A, B"（严禁调整顺序）
- ❌ 原文 "price_id" → 改为 "价格ID"（严禁翻译字段名）
- ❌ 将两行相似数据合并为一行（严禁合并行）"""

CONTINUATION_PROMPT = """继续处理同一文档的后续内容。保持与前面相同的排版风格和标题层级。
注意：这是文档的延续部分，不要重复之前的内容。"""


# ---------- 缓存 ----------

def _cache_key(content: str) -> str:
    """生成缓存键：包含 prompt 版本，确保 prompt 变更后旧缓存失效。"""
    key_material = f"{_AI_FORMAT_VERSION}:{content}"
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:16]


def _load_cache_meta() -> dict:
    meta_file = CACHE_DIR / "cache_meta.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache_meta(meta: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta_file = CACHE_DIR / "cache_meta.json"
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_cached(key: str) -> str | None:
    """尝试从缓存读取。"""
    meta = _load_cache_meta()
    if key in meta:
        cache_file = CACHE_DIR / meta[key]["file"]
        if cache_file.exists():
            return cache_file.read_text(encoding="utf-8")
    return None


def _put_cache(key: str, content: str, sheet_name: str) -> None:
    """写入缓存。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{key}.md"
    (CACHE_DIR / filename).write_text(content, encoding="utf-8")

    meta = _load_cache_meta()
    meta[key] = {"file": filename, "sheet": sheet_name, "time": time.time()}
    _save_cache_meta(meta)


# ---------- API 调用 ----------

def _call_api(client, messages: list[dict]) -> str:
    """调用 DeepSeek API，带重试（认证错误不重试）。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.3,
                max_tokens=8192,
            )
            return response.choices[0].message.content
        except Exception as e:
            last_error = e
            # 认证错误、无效 key 等不可重试的错误
            err_str = str(e).lower()
            if "401" in err_str or "authentication" in err_str or "invalid" in err_str:
                raise
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                print(f"  [AI] API 调用失败 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}，{delay}s 后重试")
                time.sleep(delay)

    raise last_error


# ---------- 分块处理 ----------

def _split_content(content: str, max_chars: int = MAX_TOKENS_PER_CALL * 2) -> list[str]:
    """按段落边界分块。"""
    if len(content) <= max_chars:
        return [content]

    chunks = []
    paragraphs = content.split("\n\n")
    current = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for \n\n
        if current_len + para_len > max_chars and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len

    if current:
        chunks.append("\n\n".join(current))

    return chunks


# ---------- 降级格式化 ----------

def fallback_format(sheet: CleanedSheet) -> str:
    """无 AI 时的降级格式化：直接使用预处理结果。"""
    lines = [f"## {sheet.name}\n"]

    if sheet.sheet_type in (SheetType.DATA_TABLE, SheetType.CONFIG_TABLE, SheetType.ENUM_TABLE):
        lines.append(sheet.content)
    elif sheet.sheet_type == SheetType.VERSION_LOG:
        lines.append(f"*版本记录*\n")
        lines.append(sheet.content)
    else:
        # 文档型：将缩进层级转换为简单的列表格式
        for line in sheet.content.split("\n"):
            if not line.strip():
                lines.append("")
                continue
            # 计算缩进层级
            stripped = line.lstrip()
            indent_count = (len(line) - len(stripped)) // 2
            if indent_count == 0:
                # 可能是标题
                if not stripped.startswith("·") and not stripped.startswith("-") and not stripped[0:1].isdigit():
                    lines.append(f"### {stripped}")
                else:
                    lines.append(stripped)
            else:
                prefix = "  " * (indent_count - 1) + "- "
                lines.append(f"{prefix}{stripped}")

    return "\n".join(lines)


# ---------- 主接口 ----------

def ai_format_sheet(sheet: CleanedSheet) -> str:
    """对单个 CleanedSheet 进行 AI 重排版。

    流程：检查缓存 → 调用 API → 写入缓存。
    失败时降级到 fallback_format。

    Returns:
        重排版后的 Markdown 文本（不含文件级标题）
    """
    if sheet.content == "*(空表)*":
        return f"## {sheet.name}\n\n*(空表)*"

    # 缓存检查
    cache_key = _cache_key(sheet.content)
    cached = _get_cached(cache_key)
    if cached is not None:
        print(f"  [AI] 缓存命中: {sheet.name} (key={cache_key})")
        return cached

    # 尝试 API 调用
    try:
        client = get_deepseek_client()
    except (ImportError, ValueError) as e:
        print(f"  [AI] 无法初始化 API ({e})，使用降级格式化: {sheet.name}")
        return fallback_format(sheet)

    chunks = _split_content(sheet.content)
    print(f"  [AI] 处理 sheet: {sheet.name} (类型={sheet.sheet_type.value}, "
          f"块数={len(chunks)}, 预估tokens≈{sheet.estimated_tokens})")

    formatted_parts = []
    try:
        for i, chunk in enumerate(chunks):
            user_content = f"## Sheet名称: {sheet.name}\n## 内容类型: {sheet.sheet_type.value}\n\n{chunk}"

            if i == 0:
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ]
            else:
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": CONTINUATION_PROMPT + "\n\n" + user_content},
                ]

            result = _call_api(client, messages)
            formatted_parts.append(result)

            if len(chunks) > 1:
                print(f"    块 {i + 1}/{len(chunks)} 完成")

    except Exception as e:
        print(f"  [AI] API 调用最终失败: {sheet.name}: {e}，使用降级格式化")
        return fallback_format(sheet)

    final = "\n\n".join(formatted_parts)

    # 写入缓存
    _put_cache(cache_key, final, sheet.name)
    print(f"  [AI] 完成: {sheet.name} (已缓存 key={cache_key})")

    return final


def ai_format_sheets(sheets: list[CleanedSheet]) -> list[str]:
    """批量处理多个 sheet。单个 sheet 失败不影响其他。"""
    results = []
    for sheet in sheets:
        try:
            result = ai_format_sheet(sheet)
            results.append(result)
        except Exception as e:
            print(f"  [AI] 异常: {sheet.name}: {e}，使用降级格式化")
            results.append(fallback_format(sheet))
    return results
