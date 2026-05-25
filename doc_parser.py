"""文档解析器 — 将 Markdown 文档分块并提取元数据。"""

import re
from pathlib import Path
from typing import Optional


# 单个 chunk 最大字符数，超过则按 ### 再拆
MAX_CHUNK_CHARS = 1500

# 最小 chunk 字符数，低于此值的 chunk 自动合并到下一个
MIN_CHUNK_CHARS = 350

# chunk 之间的重叠字符数
CHUNK_OVERLAP_CHARS = 200

# ── i18n 过滤 ─────────────────────────────────────────

_I18N_HEADING_KEYWORDS = re.compile(r'多语言|i18n|国际化', re.IGNORECASE)
_I18N_TABLE_HEADER = re.compile(r'\|\s*ID\s*\|.*CN（简体中文）')


def _is_i18n_section(heading: str) -> bool:
    """判断章节标题是否为多语言表区域"""
    return bool(_I18N_HEADING_KEYWORDS.search(heading))


def _is_i18n_table_header(line: str) -> bool:
    """判断一行是否为多语言表的表头"""
    return bool(_I18N_TABLE_HEADER.search(line))


# 文档类型关键词映射
DOC_TYPE_KEYWORDS = {
    "需求分析": "需求分析文档",
    "功能概述": "功能概述",
    "前端设计": "前端设计文档",
    "后端设计": "后端设计文档",
    "后台设计": "后台设计文档",
    "资源调用": "资源调用文档",
    "PRD": "PRD",
}

# 标签关键词映射（从文件名提取）
TAG_KEYWORDS = {
    "需求": "需求",
    "设计": "设计",
    "前端": "前端",
    "后端": "后端",
    "后台": "后台",
    "配置": "配置",
    "规范": "规范",
    "PRD": "需求",
    "功能概述": "设计",
    "资源调用": "后端",
}


def extract_module_name(file_path: str) -> str:
    """从文件路径或文件名提取模块名。

    规则：
    1. 文件名含 '-' 或 '—' 分隔符时，取第一段作为模块名
    2. 否则用父目录名
    """
    p = Path(file_path)
    stem = p.stem  # 不含扩展名

    # 去除 [xlsx转换] 等标记后缀
    import re
    clean_stem = re.sub(r'\[.*?\]$', '', stem).strip()

    for sep in ["-", "—", "–"]:
        if sep in clean_stem:
            return clean_stem.split(sep)[0].strip()

    # 回退到父目录名（跳过通用目录名）
    _SKIP_DIRS = {"doc", "docs", "resources", "文档资源", "美术变动",
                  "_converted_xlsx", "md_file", "original_file",
                  "kb-mcp-server", "tools"}
    for parent in p.parents:
        name = parent.name
        if name and name.lower() not in _SKIP_DIRS:
            return name
    # 对于无法从目录推断的文件，直接用清理后的文件名
    return clean_stem


def extract_doc_type(file_path: str) -> str:
    """从文件名提取文档类型。"""
    stem = Path(file_path).stem
    for keyword, doc_type in DOC_TYPE_KEYWORDS.items():
        if keyword in stem:
            return doc_type
    return "其他"


def extract_tags(file_path: str) -> list[str]:
    """从文件名提取标签。"""
    stem = Path(file_path).stem
    tags = []
    for keyword, tag in TAG_KEYWORDS.items():
        if keyword in stem and tag not in tags:
            tags.append(tag)
    return sorted(tags)


def extract_cross_references(text: str) -> list[str]:
    """提取文档中的交叉引用（提到的其他系统/模块名）。"""
    # 匹配 "XX系统"、"XX模块"、"XX功能" 模式
    patterns = [
        r'(?:调用|参考|关联|对接|依赖|参见|详见)\s*[「"]*(.+?(?:系统|模块|功能|服务))[」"]*',
        r'([\u4e00-\u9fa5]{2,8}(?:系统|模块))(?:的|，|。|、|）|\))',
    ]
    refs = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            ref = match.group(1).strip()
            if len(ref) <= 20:
                refs.add(ref)
    return sorted(refs)


def _extract_h1(text: str) -> str:
    """提取文档的 H1 标题（如果有）。"""
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            return line.lstrip("# ").strip()
    return ""


def split_by_headers(text: str, level: int = 2) -> list[dict]:
    """按指定级别的标题拆分文本。

    返回: [{"title": str, "content": str, "level": int}, ...]
    """
    prefix = "#" * level
    # 匹配 ##(空格)标题 的行
    pattern = rf"^({prefix}\s+.+)$"
    parts = re.split(pattern, text, flags=re.MULTILINE)

    chunks = []
    # parts[0] 是第一个 ## 之前的内容
    if parts[0].strip():
        chunks.append({"title": "", "content": parts[0].strip(), "level": 0})

    # parts[1], parts[2], parts[3], parts[4], ... 交替是标题和内容
    for i in range(1, len(parts), 2):
        title = parts[i].lstrip("#").strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        chunks.append({"title": title, "content": f"{parts[i]}\n\n{content}".strip(), "level": level})

    return chunks


def _split_by_lines(text: str, max_chars: int, sep: str = "\n") -> list[str]:
    """按行边界将超长文本硬切分，确保每片不超过 max_chars。"""
    lines = text.split(sep)
    parts = []
    current = []
    current_len = 0
    sep_len = len(sep)

    for line in lines:
        line_len = len(line) + sep_len
        if current_len + line_len > max_chars and current:
            parts.append(sep.join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        parts.append(sep.join(current))

    # 如果仍有超长片段（单行超 max_chars），按字符硬切
    final = []
    for part in parts:
        if len(part) <= max_chars:
            final.append(part)
        else:
            for i in range(0, len(part), max_chars):
                final.append(part[i:i + max_chars])

    return final


def _split_by_paragraph(text: str, max_chars: int = MAX_CHUNK_CHARS,
                        overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """按段落边界将超长文本分割为多个片段，相邻片段之间有重叠。"""
    if len(text) <= max_chars:
        return [text]

    paragraphs = text.split("\n\n")
    parts = []
    current = []
    current_len = 0
    overlap_buffer = []  # 保存前一个 part 末尾的段落用于重叠

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for \n\n
        if current_len + para_len > max_chars and current:
            parts.append("\n\n".join(current))
            # 计算 overlap: 从 current 末尾取段落直到达到 overlap 字符数
            overlap_buffer = []
            overlap_len = 0
            for p in reversed(current):
                if overlap_len + len(p) + 2 > overlap:
                    break
                overlap_buffer.insert(0, p)
                overlap_len += len(p) + 2
            current = list(overlap_buffer) + [para]
            current_len = sum(len(p) + 2 for p in current)
        else:
            current.append(para)
            current_len += para_len

    if current:
        parts.append("\n\n".join(current))

    # Fallback: 仍有超长片段时按单行切分
    final = []
    for part in parts:
        if len(part) <= max_chars:
            final.append(part)
        else:
            final.extend(_split_by_lines(part, max_chars))

    return final


def parse_document(file_path: str, text: Optional[str] = None,
                   kb_dir: Optional[Path] = None) -> list[dict]:
    """解析一个 Markdown 文档，返回分块列表。

    每个 chunk: {
        "text": str,           # chunk 全文
        "module": str,         # 模块名
        "doc_type": str,       # 文档类型
        "source": str,         # 来源文件路径
        "section": str,        # 段落标题
        "heading_chain": str,  # 完整标题层级链，如 "世界观 > 种族设定 > 精灵族"
        "cross_refs": list,    # 交叉引用
        "tags": list,          # 标签
        "doc_id": str,         # 文档标识（文件名不含扩展名）
        "chunk_index": int,    # chunk 在文档中的位置（后续填充）
        "total_chunks": int,   # 文档总 chunk 数（后续填充）
    }
    """
    if text is None:
        text = Path(file_path).read_text(encoding="utf-8")

    module = extract_module_name(file_path)
    doc_type = extract_doc_type(file_path)
    cross_refs = extract_cross_references(text)
    tags = extract_tags(file_path)
    doc_id = Path(file_path).stem

    # 先按 ## 拆分
    sections = split_by_headers(text, level=2)

    # 提取文档 H1 标题，用于构建 heading_chain
    h1_title = _extract_h1(text)

    raw_chunks = []
    for section in sections:
        content = section["content"]
        title = section["title"]

        # 过滤多语言表 section：主策略——标题匹配
        if _is_i18n_section(title):
            continue
        # 过滤多语言表 section：辅助策略——前 10 行中有 i18n 表头
        if any(_is_i18n_table_header(line) for line in content.split("\n")[:10]):
            continue

        # 构建当前 section 的 heading_chain（H2 层级）
        if h1_title and title:
            section_chain = f"{h1_title} > {title}"
        else:
            section_chain = h1_title or title

        # 如果 chunk 太长，按 ### 再拆
        if len(content) > MAX_CHUNK_CHARS:
            sub_sections = split_by_headers(content, level=3)
            if len(sub_sections) > 1:
                for sub in sub_sections:
                    sub_title = f"{title} > {sub['title']}" if title and sub["title"] else (title or sub["title"])
                    # 构建子 section 的 heading_chain（H3 层级）
                    if h1_title and title and sub["title"]:
                        sub_chain = f"{h1_title} > {title} > {sub['title']}"
                    elif h1_title and sub["title"]:
                        sub_chain = f"{h1_title} > {sub['title']}"
                    elif title and sub["title"]:
                        sub_chain = f"{title} > {sub['title']}"
                    else:
                        sub_chain = section_chain
                    # 子 section 仍超长时，按段落边界硬截断
                    sub_parts = _split_by_paragraph(sub["content"], MAX_CHUNK_CHARS)
                    for part in sub_parts:
                        raw_chunks.append({"text": part, "section": sub_title, "heading_chain": sub_chain})
                continue
            # 无法按 ### 再拆，按段落边界硬截断
            parts = _split_by_paragraph(content, MAX_CHUNK_CHARS)
            for part in parts:
                raw_chunks.append({"text": part, "section": title, "heading_chain": section_chain})
            continue

        # 正常大小，直接加入
        raw_chunks.append({"text": content, "section": title, "heading_chain": section_chain})

    # 过滤空 chunk
    raw_chunks = [c for c in raw_chunks if c["text"].strip()]

    # 内容质量过滤：去掉标题行、空行、分割线后，纯文本不足 30 字符的视为低质量
    # 低质量 chunk 标记后强制进入 carry 合并流程
    for c in raw_chunks:
        lines = c["text"].split("\n")
        substantive = [
            line for line in lines
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("---")
            and not line.strip().startswith("> ")
        ]
        pure_text = "".join(substantive).strip()
        c["_low_quality"] = len(pure_text) < 30

    # 合并过短的 chunk（< MIN_CHUNK_CHARS）或低质量 chunk 到下一个
    merged = []
    carry = None
    for c in raw_chunks:
        if carry is not None:
            # 合并到当前 chunk；优先使用后续 chunk 的 section/heading_chain（语义更精确）
            combined_section = c["section"] or carry["section"]
            combined_chain = c["heading_chain"] or carry["heading_chain"]
            c = {
                "text": carry["text"] + "\n\n" + c["text"],
                "section": combined_section,
                "heading_chain": combined_chain,
            }
            carry = None

        if len(c["text"]) < MIN_CHUNK_CHARS or c.get("_low_quality"):
            carry = c
        else:
            merged.append(c)

    # 最后剩余的 carry 追加到末尾
    if carry is not None:
        if merged:
            last = merged[-1]
            merged[-1] = {
                "text": last["text"] + "\n\n" + carry["text"],
                "section": last["section"],
                "heading_chain": last["heading_chain"],
            }
        else:
            merged.append(carry)

    # 构建最终 chunk 列表，填充元数据
    total = len(merged)
    chunks = []
    for i, c in enumerate(merged):
        chunks.append({
            "text": c["text"],
            "module": module,
            "doc_type": doc_type,
            "source": (
                str(Path(file_path).relative_to(kb_dir))
                if kb_dir is not None and Path(file_path).is_relative_to(kb_dir)
                else str(file_path)
            ),
            "section": c["section"],
            "heading_chain": c.get("heading_chain", ""),
            "cross_refs": cross_refs,
            "tags": tags,
            "doc_id": doc_id,
            "chunk_index": i,
            "total_chunks": total,
        })

    return chunks


def parse_markdown(text: str, source: str = "unknown.md",
                   kb_dir: Optional[Path] = None) -> list[dict]:
    """解析 Markdown 文本，返回分块列表（parse_document 的便捷封装）。

    参数:
        text:   Markdown 原始文本
        source: 来源标识（文件路径或虚拟名称）
        kb_dir: 知识库根目录，传入时 source 改为相对路径
    """
    return parse_document(file_path=source, text=text, kb_dir=kb_dir)
