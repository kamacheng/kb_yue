"""Excel 转换器 — 两阶段流水线：预处理 + AI 重排版。

xlsx → [阶段1: 代码预处理] → 干净中间文本 → [阶段2: AI重排版] → 高质量 Markdown
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sheet_preprocessor import preprocess_workbook, SheetType
from ai_formatter import ai_format_sheets, fallback_format

# 转换输出目录（由 indexer.py 在运行时设置为知识库目录下）
CONVERTED_DIR: Path | None = None


def _safe_name(name: str) -> str:
    """将 sheet 名称转为安全的文件名部分。"""
    return re.sub(r'[\\/:*?"<>|\s]+', '_', name).strip('_')


def _save_images(sheets, xlsx_stem: str, file_dir: Path) -> dict[str, tuple]:
    """保存所有图片到 {file_dir}/images/ 目录。

    Args:
        sheets: sheet 列表
        xlsx_stem: xlsx 文件名（不含扩展名）
        file_dir: 该 xlsx 对应的输出文件夹

    Returns:
        占位符键 -> (相对路径, sheet名, 图片序号) 的映射
    """
    mapping = {}
    img_dir = file_dir / "images"

    for sheet in sheets:
        if not sheet.images:
            continue
        img_dir.mkdir(parents=True, exist_ok=True)
        safe_sheet = _safe_name(sheet.name)
        for img_info in sheet.images:
            filename = f"{safe_sheet}_{img_info.index}.{img_info.fmt}"
            filepath = img_dir / filename
            filepath.write_bytes(img_info.data)
            key = f"IMG:{sheet.name}:{img_info.index}"
            rel_path = f"images/{filename}"
            mapping[key] = (rel_path, sheet.name, img_info.index)

    return mapping


def _replace_image_placeholders(md_content: str, mapping: dict) -> str:
    """将 {{IMG:sheet_name:index}} 占位符替换为 Markdown 图片引用。"""
    def replacer(match):
        key = match.group(1)
        if key in mapping:
            rel_path, sheet_name, index = mapping[key]
            return f"![{sheet_name} 图{index + 1}]({rel_path})"
        return match.group(0)  # 未知占位符保留原样

    return re.sub(r'\{\{(IMG:[^}]+)\}\}', replacer, md_content)


def _fallback_convert(xlsx_path: str) -> str:
    """纯预处理降级转换（无 AI）。"""
    sheets = preprocess_workbook(xlsx_path)
    p = Path(xlsx_path)

    parts = [f"# {p.stem}\n", f"> 来源: `{p.name}`\n"]
    for sheet in sheets:
        parts.append(fallback_format(sheet))

    return "\n\n".join(parts)


def convert_xlsx(xlsx_path: str, output_dir: str | None = None) -> str:
    """将 xlsx 文件转换为高质量 Markdown 文件。

    两阶段流水线：
    1. 预处理：去空行空列、类型检测、层级提取
    2. AI 重排版：DeepSeek API 调用（带缓存和降级）

    Args:
        xlsx_path: xlsx 文件路径
        output_dir: 输出目录，默认为 converted_xlsx/

    Returns:
        生成的 Markdown 文件路径
    """
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"文件不存在: {xlsx_path}")

    if output_dir:
        base_dir = Path(output_dir)
    elif CONVERTED_DIR:
        base_dir = CONVERTED_DIR
    else:
        base_dir = xlsx_path.parent / "_converted_xlsx"

    # 每个 xlsx 一个独立文件夹
    file_dir = base_dir / xlsx_path.stem
    file_dir.mkdir(parents=True, exist_ok=True)

    print(f"[转换] {xlsx_path.name}")

    # 阶段 1：预处理
    sheets = preprocess_workbook(str(xlsx_path))
    print(f"  预处理完成: {len(sheets)} 个 sheet")
    for s in sheets:
        print(f"    - {s.name}: {s.sheet_type.value}, {s.row_count} 行, ≈{s.estimated_tokens} tokens")

    # 阶段 2：AI 重排版
    formatted = ai_format_sheets(sheets)

    # 保存图片
    img_mapping = _save_images(sheets, xlsx_path.stem, file_dir)

    # 组装最终文档
    md_parts = [f"# {xlsx_path.stem}\n", f"> 来源: `{xlsx_path.name}`\n"]
    md_parts.extend(formatted)

    md_content = "\n\n".join(md_parts)

    # 替换图片占位符
    if img_mapping:
        md_content = _replace_image_placeholders(md_content, img_mapping)
        print(f"  图片: 保存 {len(img_mapping)} 张到 {xlsx_path.stem}/images/")

    output_path = file_dir / f"{xlsx_path.stem}[xlsx转换].md"
    output_path.write_text(md_content, encoding="utf-8")

    print(f"  输出: {output_path}")
    return str(output_path)


def target_md_for_xlsx(xlsx_path: Path, base_dir: Path | None = None) -> Path:
    """推导 xlsx 文件对应的目标 md 路径（与 convert_xlsx 一致）。"""
    out_base = base_dir or CONVERTED_DIR or (xlsx_path.parent / "_converted_xlsx")
    return out_base / xlsx_path.stem / f"{xlsx_path.stem}[xlsx转换].md"


def convert_all_xlsx(kb_dir: str, force: bool = False) -> dict:
    """并行转换 original_file/ 目录下所有 xlsx/xlsm 文件（增量）。

    Args:
        kb_dir: 知识库根目录。
        force: True 时不跳过未变文件，强制重转。

    Returns:
        {"converted": [...md路径], "skipped": [...md路径], "errors": [...]}
    """
    kb_path = Path(kb_dir)
    # 优先从 original_file/ 扫描，兼容旧结构（根目录）
    original_dir = kb_path / "original_file"
    scan_dir = original_dir if original_dir.exists() else kb_path
    xlsx_files = [f for ext in ("*.xlsx", "*.xlsm") for f in scan_dir.rglob(ext)]

    converted: list[str] = []
    skipped: list[str] = []
    errors: list[tuple] = []

    # 增量过滤：目标 md 存在且 mtime ≥ xlsx mtime 即跳过
    pending = []
    for f in xlsx_files:
        if not force:
            tgt = target_md_for_xlsx(f)
            if tgt.exists() and tgt.stat().st_mtime >= f.stat().st_mtime:
                skipped.append(str(tgt))
                continue
        pending.append(f)

    print(f"[批量转换] 找到 {len(xlsx_files)} 个 xlsx，待转 {len(pending)}，跳过 {len(skipped)}")

    if not pending:
        return {"converted": converted, "skipped": skipped, "errors": errors}

    def _convert_one(xlsx_file):
        try:
            return convert_xlsx(str(xlsx_file)), None
        except Exception as e:
            return None, (xlsx_file, e)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_convert_one, f): f for f in pending}
        for future in as_completed(futures):
            output, error = future.result()
            if output:
                converted.append(output)
            elif error:
                errors.append((str(error[0]), str(error[1])))
                print(f"[错误] 转换失败 {error[0]}: {error[1]}")

    print(f"[批量转换] 完成，成功 {len(converted)}/{len(pending)}，累计跳过 {len(skipped)}")
    return {"converted": converted, "skipped": skipped, "errors": errors}
