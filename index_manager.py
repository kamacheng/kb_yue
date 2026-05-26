"""索引管理器 — 文档索引、增量/全量重建、模块查询。"""

import datetime
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as _datetime, timezone
from pathlib import Path

import chromadb

from config import (
    KB_DIR, DATA_DIR, CHROMA_DIR, INDEX_META_PATH, SOURCE_META_PATH, COLLECTION_NAME,
    ORIGINAL_DIR, MD_DIR,
)
from embedding import get_collection, _embed_texts_parallel, _embed_texts_cached
from search_engine import _invalidate_bm25
from doc_parser import parse_document
from fact_extractor import extract_facts_from_chunks
from facts_store import FactsStore
from xlsx_converter import convert_all_xlsx, target_md_for_xlsx
import xlsx_converter

# xlsx 转换输出到 md_file/ 目录
xlsx_converter.CONVERTED_DIR = MD_DIR

# 任何索引/扫描流程都应跳过的目录（按目录名精确匹配）
# 资源/依赖/构建产物等明显非正文的目录，整层跳过
_EXCLUDED_DIR_NAMES = {
    "images", "resources", "_converted_xlsx",
    "node_modules", ".git", "__pycache__", "dist", "build",
    ".next", ".vscode",
}

# 索引时应排除的 md 文件名模式（按文件名 stem 或任一父目录名子串匹配）
# 这些文件不参与事实提取与法典生成，因为它们描述的是动效/美术/埋点/草稿等非业务规则
_EXCLUDED_FILE_PATTERNS = (
    "动效需求", "美术需求", "参考方案",
    "埋点",
    "初稿",
)


def _should_index_md(path) -> bool:
    """返回 True 表示此 md 文件应被索引（事实提取+向量入库）。

    排除规则：
    1. 任一父目录名命中 _EXCLUDED_DIR_NAMES（精确匹配，跳过 node_modules/resources 等）
    2. 文件 stem 或任一父目录名包含 _EXCLUDED_FILE_PATTERNS 子串（动效/美术/埋点等）
    """
    if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
        return False
    if any(pat in path.stem for pat in _EXCLUDED_FILE_PATTERNS):
        return False
    for parent in path.parents:
        if any(pat in parent.name for pat in _EXCLUDED_FILE_PATTERNS):
            return False
    return True


# ---------- 元数据缓存 ----------

_metadata_cache = None
_metadata_cache_valid = False


def _get_all_metadata():
    """获取全量元数据（带内存缓存）。"""
    global _metadata_cache, _metadata_cache_valid
    if _metadata_cache_valid and _metadata_cache is not None:
        return _metadata_cache
    collection = get_collection()
    _metadata_cache = collection.get(include=["metadatas"])
    _metadata_cache_valid = True
    return _metadata_cache


def _invalidate_metadata_cache():
    """索引变更时使元数据缓存失效。"""
    global _metadata_cache_valid
    _metadata_cache_valid = False


# ---------- chunk ID ----------

def _chunk_id(source: str, index: int) -> str:
    """为 chunk 生成稳定的唯一 ID（跨进程一致）。"""
    path_hash = hashlib.md5(source.encode()).hexdigest()[:10]
    return f"{path_hash}_{index}"


def _to_rel_path(p: Path) -> str:
    """返回相对于 KB_DIR 的路径字符串（统一 forward slash）；不在 KB_DIR 下时退回原始路径字符串。

    所有进入 ChromaDB metadata / index_meta / facts store / canon 的 source key 必须经过本函数,
    确保跨平台一致（Windows 反斜杠 / Unix 正斜杠 不再混用,避免 key 比对失配）。
    """
    raw = str(p.relative_to(KB_DIR)) if p.is_relative_to(KB_DIR) else str(p)
    return raw.replace("\\", "/")


# ---------- Facts Store ----------

_facts_store = None


def _get_facts_store() -> FactsStore:
    global _facts_store
    if _facts_store is None:
        _facts_store = FactsStore(chroma_dir=str(CHROMA_DIR))
    return _facts_store


def _purge_source_facts_and_canon(sources: list[str]) -> tuple[int, int]:
    """删除指定 source 的旧 facts 并 deprecate 旧 canon rules。

    用于文档内容变更时，确保旧的派生数据不与新抽取的事实/法典共存。
    chunks 删除由调用方负责（_batch_index_files Phase 2 / index_single）。

    Args:
        sources: 相对于 KB_DIR 的文件路径列表

    Returns:
        (facts_deleted, rules_deprecated)
    """
    facts_deleted = 0
    rules_deprecated = 0

    if not sources:
        return 0, 0

    # 1. 删除 facts
    try:
        facts_deleted = _get_facts_store().delete_by_sources(sources)
    except Exception as e:
        print(f"[WARN] 清理旧 facts 失败: {e}", file=sys.stderr)

    # 2. Deprecate canon rules（仅 active 状态）
    try:
        from canon_manager import CanonManager
        canon_mgr = CanonManager()
        source_set = set(sources)
        rules = canon_mgr.get_rules()
        for r in rules:
            if r.get("source") in source_set and r.get("status") == "active":
                try:
                    canon_mgr.deprecate_rule(r["id"])
                    rules_deprecated += 1
                except Exception:
                    pass
    except Exception as e:
        print(f"[WARN] Deprecate 旧 canon 失败: {e}", file=sys.stderr)

    if facts_deleted or rules_deprecated:
        print(
            f"[INDEX] 清理变更文件旧派生数据: facts={facts_deleted}, canon deprecated={rules_deprecated}",
            file=sys.stderr,
        )
    return facts_deleted, rules_deprecated


# ---------- 计时工具 ----------

def _canon_sync(extracted_facts: dict, trigger: str, timings: dict) -> dict:
    """统一的法典同步逻辑，供 rebuild_all 和 incremental_rebuild 使用。

    Args:
        extracted_facts: {file_path: [DesignFact, ...]} 从 _batch_index_files 返回
        trigger: 触发来源标识
        timings: 计时字典，会写入 canon_sync 耗时

    Returns:
        canon_report dict 或含 error 的 dict
    """
    t0 = time.time()
    canon_report = {"new_rules": 0, "conflicts_detected": 0, "skipped_low_value": 0}

    try:
        from canon_manager import CanonManager, filter_facts_for_canon
        from doc_parser import extract_module_name
        from module_aliases import normalize_module

        canon_mgr = CanonManager()

        # 建立 id(DesignFact) -> (source, module) 映射
        # module 在写入法典前归一化，确保同义模块（如 "权限获取"/"权限获取模块"）合并
        fact_to_source: dict[int, tuple[str, str]] = {}
        all_facts = []
        for fpath, facts in extracted_facts.items():
            if not facts:
                continue
            for f in facts:
                fact_to_source[id(f)] = (fpath, normalize_module(extract_module_name(fpath)))
                all_facts.append(f)

        if all_facts:
            filtered = filter_facts_for_canon(all_facts)  # 1次 LLM 调用
            canon_report["skipped_low_value"] = len(all_facts) - len(filtered)

            if filtered:
                # filtered[i]["fact"] 与 all_facts 中某个对象是同一引用
                batch_items = []
                for item in filtered:
                    src, mod = fact_to_source.get(id(item["fact"]), ("unknown", "未知"))
                    batch_items.append({
                        "fact": item["fact"],
                        "priority": item["priority"],
                        "source": src,
                        "module": mod,
                    })
                merge_result = canon_mgr.merge_batch(batch_items, trigger=trigger)
                canon_report["new_rules"] = merge_result["new_rules"]
                canon_report["conflicts_detected"] = merge_result["conflicts_detected"]

        # 自动推断规则依赖
        canon_mgr.infer_dependencies()
        timings["canon_sync"] = round(time.time() - t0, 2)
        return {"canon_updates": canon_report}

    except Exception as e:
        timings["canon_sync"] = round(time.time() - t0, 2)
        return {
            "canon_sync_error": str(e),
            "canon_sync_hint": (
                "法典同步失败但索引数据已保留。可执行 `kb_canon action=sync` 手动重试,"
                "或 `kb_canon action=status` 查看当前法典状态。"
            ),
        }


def _print_timing_summary(timings: dict):
    """在 stderr 打印计时摘要。"""
    print("[TIMING] === 各阶段耗时 ===", file=sys.stderr)
    order = ["md_sync", "xlsx_convert", "parse", "delete_old", "embedding",
             "fact_extraction", "batch_index", "canon_sync", "total"]
    labels = {
        "md_sync": "MD同步", "xlsx_convert": "XLSX转换", "parse": "文档解析",
        "delete_old": "旧chunks删除", "embedding": "嵌入向量",
        "fact_extraction": "事实提取", "batch_index": "批量索引",
        "canon_sync": "法典同步", "total": "总计",
    }
    for key in order:
        if key in timings:
            label = labels.get(key, key)
            print(f"[TIMING]   {label}: {timings[key]}s", file=sys.stderr)
    # 打印不在 order 中的项
    for key, val in timings.items():
        if key not in order:
            print(f"[TIMING]   {key}: {val}s", file=sys.stderr)


# ---------- 源文件同步 ----------

def _load_source_meta() -> dict:
    """加载源文件签名表（用于检测 original_file 中文件的变更）。"""
    if SOURCE_META_PATH.exists():
        try:
            return json.loads(SOURCE_META_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_source_meta(meta: dict):
    """保存源文件签名表。"""
    SOURCE_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_META_PATH.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sync_md_from_original() -> dict:
    """将 original_file/ 中的 md 文件同步到 md_file/（保持目录结构）。

    使用 source_meta.json 跟踪源文件签名（mtime+size），避免因 dst 被
    人工修改导致 src 后续变更被漏检。

    Returns:
        {"synced": int, "unchanged": int, "errors": [...]}
    """
    import shutil

    if not ORIGINAL_DIR.exists():
        return {"synced": 0, "unchanged": 0, "errors": []}

    MD_DIR.mkdir(parents=True, exist_ok=True)
    source_meta = _load_source_meta()
    synced = 0
    unchanged = 0
    errors = []

    # 遍历 original_file/ 下的所有 md 文件
    for src in ORIGINAL_DIR.rglob("*.md"):
        try:
            rel = src.relative_to(ORIGINAL_DIR)
            dst = MD_DIR / rel
            src_stat = src.stat()
            src_key = _to_rel_path(src)
            cached = source_meta.get(src_key) or {}

            # 源签名未变 + dst 存在 → 跳过
            if (
                dst.exists()
                and cached.get("mtime") == src_stat.st_mtime
                and cached.get("size") == src_stat.st_size
            ):
                unchanged += 1
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            source_meta[src_key] = {
                "mtime": src_stat.st_mtime,
                "size": src_stat.st_size,
                "synced_at": datetime.datetime.now().isoformat(),
            }
            synced += 1
        except Exception as e:
            errors.append({"file": str(src), "error": str(e)})

    _save_source_meta(source_meta)

    # 同步 resources 等资源目录
    for src_dir in ORIGINAL_DIR.rglob("resources"):
        if src_dir.is_dir():
            try:
                rel = src_dir.relative_to(ORIGINAL_DIR)
                dst_dir = MD_DIR / rel
                if not dst_dir.exists():
                    shutil.copytree(str(src_dir), str(dst_dir))
            except Exception:
                pass

    print(f"[SYNC] MD 同步: {synced} 个文件更新, {unchanged} 个未变", file=sys.stderr)
    return {"synced": synced, "unchanged": unchanged, "errors": errors}


# ---------- 批量索引 ----------

CHROMA_ADD_BATCH_SIZE = 5000  # ChromaDB 单次 add 上限


def _batch_index_files(file_paths: list[str], skip_facts: bool = False) -> dict:
    """批量索引多个文件，通过并行嵌入和事实提取加速。

    Returns:
        {"indexed": int, "total_chunks": int, "errors": [...], "timings": {...}}
    """
    collection = get_collection()
    indexed = 0
    total_chunks = 0
    errors = []
    timings = {}

    # Phase 1: 解析所有文档，收集 chunks
    t_parse_start = time.time()
    all_ids = []
    all_documents = []
    all_metadatas = []
    file_texts: dict[str, tuple[str, list]] = {}  # path -> (full_text, chunks)
    sources_to_delete: list[str] = []

    total_files = len(file_paths)

    def _parse_one_file(fpath: str):
        """解析单个文件，返回解析结果或错误。"""
        try:
            p = Path(fpath)
            if not p.exists() or p.suffix.lower() not in (".md",):
                return {"error": {"file": fpath, "error": "文件不存在或类型不支持"}}

            text = p.read_text(encoding="utf-8")

            # 计算相对于 KB_DIR 的路径（用于 ChromaDB source 和 chunk ID）
            rel_fpath = _to_rel_path(p)

            chunks = parse_document(str(fpath), text, kb_dir=KB_DIR)
            if not chunks:
                return {"error": {"file": fpath, "error": "文档解析后无有效内容"}}

            doc_version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
            doc_mtime = _datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
            indexed_at = _datetime.now(timezone.utc).isoformat()

            ids = [_chunk_id(rel_fpath, j) for j in range(len(chunks))]
            documents = [c["text"] for c in chunks]
            metadatas = [
                {
                    "module": c["module"],
                    "doc_type": c["doc_type"],
                    "source": rel_fpath,
                    "section": c["section"],
                    "cross_refs": json.dumps(c["cross_refs"], ensure_ascii=False),
                    "tags": json.dumps(c["tags"], ensure_ascii=False),
                    "doc_id": c["doc_id"],
                    "chunk_index": c["chunk_index"],
                    "total_chunks": c["total_chunks"],
                    "doc_version": doc_version,
                    "doc_mtime": doc_mtime,
                    "indexed_at": indexed_at,
                }
                for c in chunks
            ]

            return {
                "fpath": rel_fpath, "text": text, "chunks": chunks,
                "ids": ids, "documents": documents, "metadatas": metadatas,
            }
        except Exception as e:
            return {"error": {"file": fpath, "error": str(e)}}

    # 并行解析所有文件
    with ThreadPoolExecutor(max_workers=8) as executor:
        parse_results = list(executor.map(_parse_one_file, file_paths))

    for result in parse_results:
        if "error" in result:
            errors.append(result["error"])
            continue

        fpath = result["fpath"]
        file_texts[fpath] = (result["text"], result["chunks"])
        sources_to_delete.append(fpath)
        all_ids.extend(result["ids"])
        all_documents.extend(result["documents"])
        all_metadatas.extend(result["metadatas"])
        indexed += 1
        total_chunks += len(result["chunks"])

    timings["parse"] = round(time.time() - t_parse_start, 2)
    print(f"[INDEX] 解析文档: {indexed}/{total_files} 完成, 共 {total_chunks} 个 chunks", file=sys.stderr)

    if not all_documents:
        return {"indexed": 0, "total_chunks": 0, "errors": errors, "timings": timings}

    # Phase 2: 批量删除旧 chunks
    t_delete_start = time.time()
    for source in sources_to_delete:
        try:
            existing = collection.get(where={"source": source})
            if existing["ids"]:
                collection.delete(ids=existing["ids"])
        except Exception:
            pass

    # Phase 2.5: 清理旧 facts + deprecate 旧 canon（仅在不跳过事实提取时）
    # 确保文件内容变更时,旧的派生数据不与新抽取的事实/法典共存
    if not skip_facts and sources_to_delete:
        _purge_source_facts_and_canon(sources_to_delete)

    timings["delete_old"] = round(time.time() - t_delete_start, 2)

    # Phase 3+4: 嵌入向量 (SiliconFlow API) 和事实提取 (DeepSeek API) 并行执行
    def _do_embedding():
        """Phase 3: 生成嵌入向量并写入 ChromaDB。失败时尝试逐批兜底,避免整批丢失。"""
        t_start = time.time()
        print(f"[INDEX] 开始生成嵌入向量 ({len(all_documents)} 个文本)...", file=sys.stderr)
        try:
            embs = _embed_texts_cached(all_documents)
        except Exception as e:
            errors.append({"file": "<embedding>", "error": f"嵌入生成失败: {e}. 建议: 检查 SiliconFlow API key/网络,然后重跑 kb_index"})
            print(f"[WARN] 嵌入生成失败,本轮无法写入向量: {e}", file=sys.stderr)
            return round(time.time() - t_start, 2)

        success_batches = 0
        failed_batches = 0
        for i in range(0, len(all_ids), CHROMA_ADD_BATCH_SIZE):
            end = min(i + CHROMA_ADD_BATCH_SIZE, len(all_ids))
            try:
                collection.add(
                    ids=all_ids[i:end],
                    embeddings=embs[i:end],
                    documents=all_documents[i:end],
                    metadatas=all_metadatas[i:end],
                )
                success_batches += 1
            except Exception as e:
                failed_batches += 1
                errors.append({"file": f"<chroma_batch_{i // CHROMA_ADD_BATCH_SIZE}>",
                              "error": f"ChromaDB 写入失败: {e}. 建议: 检查磁盘空间/权限"})
                print(f"[WARN] ChromaDB batch 写入失败 ({i}-{end}): {e}", file=sys.stderr)
        elapsed = round(time.time() - t_start, 2)
        if failed_batches:
            print(f"[WARN] 向量写入完成 (成功 {success_batches} 批, 失败 {failed_batches} 批)", file=sys.stderr)
        else:
            print(f"[INDEX] 向量索引写入完成", file=sys.stderr)
        return elapsed

    def _do_facts():
        """Phase 4: 事实提取（per-chunk，仅调用 API，不写 ChromaDB）。"""
        t_start = time.time()
        facts_result: dict[str, list] = {}
        fact_items = []
        for fpath, (text, chunks) in file_texts.items():
            module = chunks[0]["module"] if chunks else "未知"
            fact_items.append((fpath, chunks, module))

        completed_facts = 0
        total_fact_files = len(fact_items)

        def _extract_one(item):
            fp, cks, mod = item
            try:
                facts = extract_facts_from_chunks(cks)
                return fp, facts or [], None
            except Exception as e:
                return fp, [], str(e)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_extract_one, item): item for item in fact_items}
            for future in as_completed(futures):
                fp, facts, err = future.result()
                facts_result[fp] = facts
                completed_facts += 1
                if err:
                    print(f"[WARN] 事实提取失败 ({Path(fp).name}): {err}", file=sys.stderr)
                print(f"[INDEX] 事实提取: {completed_facts}/{total_fact_files} 完成", file=sys.stderr)

        elapsed = round(time.time() - t_start, 2)
        return facts_result, elapsed

    def _store_facts(extracted: dict[str, list]):
        """收集所有文件事实后批量写入 FactsStore（单次内存去重 + 单次 upsert）。

        批量失败时 fallback 为 per-source 写入,确保单个文件出错不影响其他文件的事实。
        """
        store = _get_facts_store()
        all_fact_items = []
        per_source: dict[str, list] = {}
        for fpath, facts in extracted.items():
            if not facts:
                continue
            module = "未知"
            if fpath in file_texts:
                _, chunks = file_texts[fpath]
                module = chunks[0]["module"] if chunks else "未知"
            per_source[fpath] = (module, facts)
            for f in facts:
                all_fact_items.append({"fact": f, "source": fpath, "module": module})

        if not all_fact_items:
            return

        t0 = time.time()
        try:
            count = store.add_facts_batch(all_fact_items, semantic_dedup=True)
            elapsed = round(time.time() - t0, 2)
            print(f"[INDEX] 事实批量写入: {count}/{len(all_fact_items)} 条, 耗时 {elapsed}s", file=sys.stderr)
            return
        except Exception as e:
            print(f"[WARN] facts store 批量写入失败,改为 per-source 兜底: {e}", file=sys.stderr)

        ok_sources = 0
        for fpath, (module, facts) in per_source.items():
            try:
                store.add_facts(facts, source=fpath, module=module)
                ok_sources += 1
            except Exception as e2:
                errors.append({"file": fpath, "error": f"facts 写入失败(兜底亦失败): {e2}"})
                print(f"[WARN] facts 写入失败 ({fpath}): {e2}", file=sys.stderr)
        elapsed = round(time.time() - t0, 2)
        print(f"[INDEX] 事实兜底写入: {ok_sources}/{len(per_source)} 源成功, 耗时 {elapsed}s", file=sys.stderr)

    extracted_facts: dict[str, list] = {}
    if skip_facts:
        # 仅嵌入，不提取事实
        timings["embedding"] = _do_embedding()
        timings["fact_extraction"] = 0
        print(f"[INDEX] 跳过事实提取 (skip_facts=True)", file=sys.stderr)
    else:
        # 并行执行嵌入（SiliconFlow API）和事实提取（DeepSeek API）
        # ChromaDB 写入串行：先完成 embedding 写入，再写入 facts store
        t_parallel_start = time.time()
        with ThreadPoolExecutor(max_workers=2) as executor:
            embed_future = executor.submit(_do_embedding)
            facts_future = executor.submit(_do_facts)
            timings["embedding"] = embed_future.result()  # 含 ChromaDB 写入
            extracted_facts, timings["fact_extraction"] = facts_future.result()  # 仅 API 调用
        # embedding 写入已完成，串行写入 facts store（无 ChromaDB 竞争）
        _store_facts(extracted_facts)
        print(f"[INDEX] 嵌入+事实并行完成，墙钟 {round(time.time() - t_parallel_start, 2)}s", file=sys.stderr)

    _invalidate_bm25()
    _invalidate_metadata_cache()
    return {"indexed": indexed, "total_chunks": total_chunks, "errors": errors, "extracted_facts": extracted_facts, "timings": timings}


# ---------- 核心函数 ----------

def index_single(file_path: str, skip_facts: bool = False) -> dict:
    """索引单个文档。

    Args:
        file_path: 文档路径 (md 文件)
        skip_facts: 是否跳过事实提取

    Returns:
        {"status": "success", "chunks": int} 或 {"status": "error", "message": str}
    """
    try:
        p = Path(file_path).resolve()
        if not p.exists():
            return {"status": "error", "message": f"文件不存在: {file_path}"}

        if p.suffix.lower() not in (".md",):
            return {"status": "error", "message": f"不支持的文件类型: {p.suffix}"}

        text = p.read_text(encoding="utf-8")

        rel_path = _to_rel_path(p)

        chunks = parse_document(str(file_path), text, kb_dir=KB_DIR)

        if not chunks:
            return {"status": "error", "message": "文档解析后无有效内容"}

        doc_version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        doc_mtime = _datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
        indexed_at = _datetime.now(timezone.utc).isoformat()

        collection = get_collection()

        # 删除该文件的旧 chunks
        existing = collection.get(where={"source": rel_path})
        if existing["ids"]:
            collection.delete(ids=existing["ids"])

        # 删除该文件的旧 facts + deprecate 旧 canon（仅在不跳过事实提取时）
        if not skip_facts:
            _purge_source_facts_and_canon([rel_path])

        # 添加新 chunks（包含完整元数据）
        ids = [_chunk_id(rel_path, i) for i in range(len(chunks))]
        documents = [c["text"] for c in chunks]
        metadatas = [
            {
                "module": c["module"],
                "doc_type": c["doc_type"],
                "source": rel_path,
                "section": c["section"],
                "cross_refs": json.dumps(c["cross_refs"], ensure_ascii=False),
                "tags": json.dumps(c["tags"], ensure_ascii=False),
                "doc_id": c["doc_id"],
                "chunk_index": c["chunk_index"],
                "total_chunks": c["total_chunks"],
                "doc_version": doc_version,
                "doc_mtime": doc_mtime,
                "indexed_at": indexed_at,
            }
            for c in chunks
        ]

        collection.add(ids=ids, documents=documents, metadatas=metadatas)

        # 提取并存储设计事实（per-chunk 提取，替代旧的 text[:8000] 截断）
        extracted_facts: dict[str, list] = {}
        if not skip_facts:
            try:
                facts = extract_facts_from_chunks(chunks)
                if facts:
                    store = _get_facts_store()
                    module = chunks[0]["module"] if chunks else "未知"
                    store.add_facts(facts, source=rel_path, module=module)
                    print(f"[INDEX] 提取 {len(facts)} 条设计事实: {p.name}", file=sys.stderr)
                    extracted_facts[rel_path] = facts
            except Exception as e:
                print(f"[WARN] 事实提取失败 ({p.name}): {e}", file=sys.stderr)

        # 索引变更，BM25 和元数据缓存失效
        _invalidate_bm25()
        _invalidate_metadata_cache()

        result = {"status": "success", "chunks": len(chunks), "file": str(file_path)}

        # Canon sync：单文件索引也同步法典
        if not skip_facts and extracted_facts:
            timings: dict = {}
            canon_result = _canon_sync(extracted_facts, trigger="kb_index_single", timings=timings)
            result.update(canon_result)

        return result

    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------- 模块查询 ----------

def list_modules() -> dict:
    """列出所有已索引的模块及其文档清单。

    模块名按 module_aliases.json 归一化合并（如 "权限获取" 与 "权限获取模块" 合并为同一项）。

    Returns:
        {"modules": {"模块名": ["文档路径1", ...], ...}, "total_docs": int}
    """
    from module_aliases import normalize_module

    all_data = _get_all_metadata()

    modules: dict[str, set[str]] = {}
    for meta in all_data["metadatas"]:
        module = normalize_module(meta.get("module", "未知"))
        source = meta.get("source", "")
        if module not in modules:
            modules[module] = set()
        modules[module].add(source)

    result = {name: sorted(docs) for name, docs in sorted(modules.items())}
    total_docs = sum(len(docs) for docs in result.values())
    return {"modules": result, "total_docs": total_docs}


def get_module_relations() -> dict:
    """利用 cross_refs 构建模块关系图。

    节点与边均使用归一化后的规范名，避免因 "权限获取" / "权限获取模块" 等同义名称重复成节点。

    Returns:
        {"nodes": ["模块A", ...], "edges": [{"from": "A", "to": "B", "refs": ["XX系统"]}, ...]}
    """
    from module_aliases import normalize_module

    all_data = _get_all_metadata()

    # 收集每个模块的交叉引用
    module_refs: dict[str, set[str]] = {}
    all_modules = set()

    for meta in all_data["metadatas"]:
        module = normalize_module(meta.get("module", "未知"))
        all_modules.add(module)
        refs_str = meta.get("cross_refs", "[]")
        try:
            refs = json.loads(refs_str)
        except (json.JSONDecodeError, TypeError):
            refs = []
        if module not in module_refs:
            module_refs[module] = set()
        for ref in refs:
            module_refs[module].add(ref)

    # 构建边：如果模块 A 引用了包含模块 B 名称的术语
    edges = []
    seen = set()
    for mod_a, refs in module_refs.items():
        for ref in refs:
            for mod_b in all_modules:
                if mod_b != mod_a and mod_b in ref:
                    edge_key = (mod_a, mod_b)
                    if edge_key not in seen:
                        seen.add(edge_key)
                        edges.append({"from": mod_a, "to": mod_b, "ref": ref})

    return {
        "nodes": sorted(all_modules),
        "edges": edges,
    }


def suggest_queries(partial: str, max_suggestions: int = 10) -> list[str]:
    """基于已索引的章节标题和高频关键词返回搜索建议。纯本地实现。"""
    collection = get_collection()
    if collection.count() == 0:
        return []

    all_data = _get_all_metadata()
    sections = set()
    modules = set()

    for meta in all_data["metadatas"]:
        section = meta.get("section", "")
        module = meta.get("module", "")
        if section:
            sections.add(section)
        if module:
            modules.add(module)

    suggestions = []
    partial_lower = partial.lower()

    # 匹配章节标题
    for s in sorted(sections):
        if partial_lower in s.lower():
            suggestions.append(s)
            if len(suggestions) >= max_suggestions:
                break

    # 匹配模块名
    if len(suggestions) < max_suggestions:
        for m in sorted(modules):
            if partial_lower in m.lower() and m not in suggestions:
                suggestions.append(m)
                if len(suggestions) >= max_suggestions:
                    break

    return suggestions[:max_suggestions]


# ---------- 增量索引 ----------

def _load_index_meta() -> dict:
    """加载增量索引元数据。"""
    if INDEX_META_PATH.exists():
        try:
            return json.loads(INDEX_META_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_index_meta(meta: dict):
    """保存增量索引元数据。"""
    INDEX_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_META_PATH.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _file_signature(p: Path) -> dict:
    """获取文件签名（mtime + size + indexed_at）。"""
    stat = p.stat()
    return {
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "indexed_at": datetime.datetime.now().isoformat(),
    }


def get_recent_changes(days: int = 7) -> dict:
    """返回最近 N 天内变更的文档列表。"""
    meta = _load_index_meta()
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)

    changes = []
    for fpath, info in meta.items():
        indexed_at = info.get("indexed_at", "")
        if indexed_at:
            try:
                dt = datetime.datetime.fromisoformat(indexed_at)
                if dt >= cutoff:
                    changes.append({
                        "file": fpath,
                        "indexed_at": indexed_at,
                        "size": info.get("size", 0),
                    })
            except (ValueError, TypeError):
                pass

    changes.sort(key=lambda x: x["indexed_at"], reverse=True)
    return {"days": days, "changes": changes, "total": len(changes)}


def incremental_rebuild(skip_facts: bool = False) -> dict:
    """增量重建索引：只索引变更/新增文件，删除已移除文件的 chunks。

    Args:
        skip_facts: 是否跳过事实提取（加速索引）

    Returns:
        {"status": "success", "indexed": int, "unchanged": int, "removed": int, "errors": [...]}
    """
    start_time = time.time()
    timings = {}

    # 1. 同步 original_file/ 中的 md 文件到 md_file/
    t0 = time.time()
    sync_result = _sync_md_from_original()
    timings["md_sync"] = round(time.time() - t0, 2)

    # 2. 转换 original_file/ 中的 xlsx 到 md_file/
    t0 = time.time()
    converted = convert_all_xlsx(str(KB_DIR))
    timings["xlsx_convert"] = round(time.time() - t0, 2)

    # 3. 收集 md_file/ 下所有 md 文件（排除 _EXCLUDED_FILE_PATTERNS 命中的文件）
    MD_DIR.mkdir(parents=True, exist_ok=True)
    md_files = [f for f in MD_DIR.rglob("*.md") if _should_index_md(f)]
    current_files = {str(f): f for f in md_files}

    # 建立 absolute -> relative 路径映射，用于 index_meta 和 ChromaDB 查询
    # md_files 来自 MD_DIR.rglob，MD_DIR 是 KB_DIR 的子目录，_to_rel_path 不会退回
    rel_path_map = {str(f): _to_rel_path(f) for f in md_files}

    # 4. 加载旧元数据
    old_meta = _load_index_meta()
    new_meta = {}

    unchanged = 0
    removed = 0
    changed_files = []

    # 5. 检测需要索引的文件
    file_sigs = {}
    for fpath, p in current_files.items():
        rel_fpath = rel_path_map[fpath]
        sig = _file_signature(p)
        file_sigs[fpath] = sig
        old_sig = old_meta.get(rel_fpath)  # 用相对路径查旧元数据

        if old_sig and old_sig.get("mtime") == sig["mtime"] and old_sig.get("size") == sig["size"]:
            new_meta[rel_fpath] = sig  # 写相对路径 key
            unchanged += 1
        else:
            changed_files.append(fpath)  # 保持绝对路径供 _batch_index_files 做文件 I/O

    # 6. 批量索引变更文件
    result: dict = {}
    if changed_files:
        t0 = time.time()
        batch_result = _batch_index_files(changed_files, skip_facts=skip_facts)
        timings["batch_index"] = round(time.time() - t0, 2)
        timings.update(batch_result.get("timings", {}))
        for fpath in changed_files:
            new_meta[rel_path_map[fpath]] = file_sigs[fpath]  # 写相对路径 key
        errors = batch_result["errors"]
        indexed = batch_result["indexed"]
        total_chunks = batch_result["total_chunks"]

        # 6a. Canon sync
        if not skip_facts:
            extracted_facts = batch_result.get("extracted_facts", {})
            canon_result = _canon_sync(extracted_facts, trigger="kb_update_index", timings=timings)
            result.update(canon_result)
    else:
        errors = []
        indexed = 0
        total_chunks = 0

    # 7. 删除已移除文件的 chunks
    current_rel_set = set(rel_path_map.values())
    removed_files = [fpath for fpath in old_meta if fpath not in current_rel_set]
    # old_meta 的 key 是相对路径，collection.get(where={"source": fpath}) 用相对路径查询 ✓
    collection = get_collection()
    for fpath in removed_files:
        try:
            existing = collection.get(where={"source": fpath})
            if existing["ids"]:
                collection.delete(ids=existing["ids"])
                removed += 1
        except Exception:
            pass

    # 7a. Canon 废弃已删除文件的规则
    if removed_files:
        try:
            from canon_manager import CanonManager
            canon_mgr = CanonManager()
            for fpath in removed_files:
                rules = canon_mgr.get_rules()
                for r in rules:
                    if r.get("source") == fpath and r.get("status") == "active":
                        canon_mgr.deprecate_rule(r["id"])
        except Exception:
            pass

    # 7b. Facts store 同步删除已移除文件的事实
    if removed_files:
        try:
            facts_deleted = _get_facts_store().delete_by_sources(removed_files)
            if facts_deleted:
                print(f"[INDEX] 清理孤儿事实: {facts_deleted} 条", file=sys.stderr)
        except Exception as e:
            print(f"[WARN] 清理孤儿事实失败: {e}", file=sys.stderr)

    # 8. 保存新元数据
    _save_index_meta(new_meta)
    _invalidate_bm25()
    _invalidate_metadata_cache()

    # 9. 检测孤儿（md_file 中无 original 源 / index_meta 中文件已删）
    #    历史行为是自动 destructive cleanup,容易因路径大小写/分隔符差异误删。
    #    现改为 dry-run 检测,在 result 中提示用户运行 `kb_index mode=cleanup confirm=True` 显式清理。
    cleanup_result = None
    t0 = time.time()
    try:
        cleanup_result = cleanup_orphans(dry_run=True)
    except Exception as e:
        print(f"[WARN] 孤儿检测失败: {e}", file=sys.stderr)
    timings["cleanup_orphans"] = round(time.time() - t0, 2)

    timings["total"] = round(time.time() - start_time, 2)
    if timings["total"] > 1:  # 只在有实质工作时打印
        _print_timing_summary(timings)

    result.update({
        "status": "success",
        "indexed": indexed,
        "unchanged": unchanged,
        "removed": removed,
        "total_chunks": total_chunks,
        "xlsx_converted": len(converted.get("converted", [])),
        "xlsx_skipped": len(converted.get("skipped", [])),
        "md_synced": sync_result.get("synced", 0),
        "cleanup": cleanup_result,
        "errors": errors,
        "timings": timings,
    })
    return result


def rebuild_all(full: bool = True, skip_facts: bool = False, clear_canon: bool = False) -> dict:
    """重建索引。

    Args:
        full: True 全量重建（清空后重建），False 增量重建（只处理变更）
        skip_facts: 是否跳过事实提取（加速索引）
        clear_canon: 全量重建时是否同时清空法典（默认 False 保留已有规则）

    Returns:
        重建结果字典
    """
    if not full:
        return incremental_rebuild(skip_facts=skip_facts)

    # 全量重建
    start_time = time.time()
    timings = {}

    # 1. 同步 original_file/ 中的 md 文件到 md_file/
    t0 = time.time()
    sync_result = _sync_md_from_original()
    timings["md_sync"] = round(time.time() - t0, 2)

    # 2. 转换 original_file/ 中的 xlsx 到 md_file/
    t0 = time.time()
    converted = convert_all_xlsx(str(KB_DIR))
    timings["xlsx_convert"] = round(time.time() - t0, 2)

    # 3. 收集 md_file/ 下所有 md 文件（排除 _EXCLUDED_FILE_PATTERNS 命中的文件）
    MD_DIR.mkdir(parents=True, exist_ok=True)
    md_files = [f for f in MD_DIR.rglob("*.md") if _should_index_md(f)]

    # 4. 清空旧索引
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass

    # 重置 facts_store 单例，避免全量重建后仍持有旧实例
    global _facts_store
    _facts_store = None

    # 4a. 可选：清空法典
    if clear_canon:
        try:
            from canon_manager import CanonManager
            canon_mgr = CanonManager()
            empty_data = {"version": "1.0", "rules": []}
            canon_mgr._write_canon(empty_data)
            print("[INDEX] 法典已清空", file=sys.stderr)
        except Exception as e:
            print(f"[WARN] 清空法典失败: {e}", file=sys.stderr)

    # 5. 批量索引所有文件
    t0 = time.time()
    file_paths = [str(f) for f in md_files]
    batch_result = _batch_index_files(file_paths, skip_facts=skip_facts)
    timings["batch_index"] = round(time.time() - t0, 2)
    # 合并内部计时
    timings.update(batch_result.get("timings", {}))

    # 保存增量索引元数据（key 为相对路径）
    # md_files 来自 MD_DIR.rglob，MD_DIR 是 KB_DIR 的子目录，relative_to 不会失败
    index_meta = {}
    for md_file in md_files:
        index_meta[_to_rel_path(md_file)] = _file_signature(md_file)
    _save_index_meta(index_meta)
    _invalidate_bm25()
    _invalidate_metadata_cache()

    elapsed = time.time() - start_time
    print(f"[INDEX] 全量重建完成，耗时 {elapsed:.1f}s", file=sys.stderr)

    result = {
        "status": "success",
        "total_files": batch_result["indexed"],
        "total_chunks": batch_result["total_chunks"],
        "xlsx_converted": len(converted.get("converted", [])),
        "xlsx_skipped": len(converted.get("skipped", [])),
        "md_synced": sync_result.get("synced", 0),
        "elapsed_seconds": round(elapsed, 1),
        "errors": batch_result["errors"],
    }

    # Canon sync：复用 _batch_index_files 已提取的事实，避免重复调用 LLM
    if not skip_facts:
        extracted_facts = batch_result.get("extracted_facts", {})
        canon_result = _canon_sync(extracted_facts, trigger="kb_rebuild_index", timings=timings)
        result.update(canon_result)

    # 打印计时摘要
    timings["total"] = round(time.time() - start_time, 2)
    result["timings"] = timings
    _print_timing_summary(timings)

    return result


# ---------- 处理状态查询 ----------

def _iter_original_sources():
    """遍历 original_file 下所有源文件 (xlsx/xlsm/md)，跳过资源目录。"""
    if not ORIGINAL_DIR.exists():
        return
    for ext in ("*.xlsx", "*.xlsm", "*.md"):
        for f in ORIGINAL_DIR.rglob(ext):
            if any(part in _EXCLUDED_DIR_NAMES for part in f.parts):
                continue
            yield f


def _dst_md_for_source(src: Path) -> Path:
    """推导 source 文件的目标 md 路径。

    - xlsx/xlsm: MD_DIR/<stem>/<stem>[xlsx转换].md（与转换器一致）
    - md: MD_DIR/<src 相对 ORIGINAL_DIR 的路径>（与同步器一致）
    """
    suffix = src.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return target_md_for_xlsx(src, MD_DIR)
    return MD_DIR / src.relative_to(ORIGINAL_DIR)


def compute_processing_status() -> dict:
    """对比 original_file → md_file → 索引，返回每个源文件的处理状态。

    分类（按严重度从高到低）：
        - unprocessed:     原始存在，目标 md 未生成
        - outdated_src:    原始 mtime > 目标 md mtime（改了未重转/重同步）
        - outdated_index:  目标 md 存在但索引缺失/过期
        - ok:              三层签名一致
        - orphan_md:       md_file 存在但无 original 源指向
        - orphan_index:    index_meta 有 key 但对应文件已删

    Returns:
        {
            "summary": {分类: 计数},
            "items":   [{src, src_type, dst, status, src_mtime, dst_mtime, indexed_mtime}],
            "total_sources": int,
        }
    """
    index_meta = _load_index_meta()
    items: list[dict] = []
    seen_dst: set[str] = set()  # 已被 source 指向的 dst 相对路径（用于检测 orphan_md）

    # 1. 扫每个 original 源文件
    for src in _iter_original_sources():
        dst = _dst_md_for_source(src)
        src_rel = _to_rel_path(src)
        dst_rel = _to_rel_path(dst)
        seen_dst.add(dst_rel)

        try:
            src_stat = src.stat()
        except OSError:
            continue
        src_mtime = src_stat.st_mtime
        src_type = src.suffix.lower().lstrip(".")

        # 2. 判断状态
        if not dst.exists():
            status = "unprocessed"
            dst_mtime = None
            indexed_mtime = None
        else:
            dst_mtime = dst.stat().st_mtime
            indexed_info = index_meta.get(dst_rel)
            indexed_mtime = indexed_info.get("mtime") if indexed_info else None

            if src_mtime > dst_mtime + 1:  # 1 秒容差，避免文件系统精度抖动
                status = "outdated_src"
            elif indexed_mtime is None:
                status = "outdated_index"
            elif indexed_mtime + 1 < dst_mtime:
                status = "outdated_index"
            else:
                status = "ok"

        items.append({
            "src": src_rel,
            "src_type": src_type,
            "dst": dst_rel,
            "status": status,
            "src_mtime": src_mtime,
            "dst_mtime": dst_mtime,
            "indexed_mtime": indexed_mtime,
        })

    # 3. orphan_md：md_file 中存在但没有 original 源指向（跳过资源目录里的 md）
    if MD_DIR.exists():
        for md in MD_DIR.rglob("*.md"):
            if any(part in _EXCLUDED_DIR_NAMES for part in md.parts):
                continue
            md_rel = _to_rel_path(md)
            if md_rel in seen_dst:
                continue
            indexed_info = index_meta.get(md_rel)
            items.append({
                "src": None,
                "src_type": None,
                "dst": md_rel,
                "status": "orphan_md",
                "src_mtime": None,
                "dst_mtime": md.stat().st_mtime,
                "indexed_mtime": indexed_info.get("mtime") if indexed_info else None,
            })

    # 4. orphan_index：index_meta 有 key 但实际 md 文件已删
    indexed_paths = {item["dst"] for item in items if item["dst"]}
    for key, info in index_meta.items():
        if key in indexed_paths:
            continue
        if not (KB_DIR / key).exists():
            items.append({
                "src": None,
                "src_type": None,
                "dst": key,
                "status": "orphan_index",
                "src_mtime": None,
                "dst_mtime": None,
                "indexed_mtime": info.get("mtime"),
            })

    # 汇总计数
    summary = {
        "ok": 0, "outdated_src": 0, "outdated_index": 0,
        "unprocessed": 0, "orphan_md": 0, "orphan_index": 0,
    }
    for it in items:
        summary[it["status"]] = summary.get(it["status"], 0) + 1

    # 排序：按状态优先级（问题在前）+ 路径
    _priority = {
        "unprocessed": 0, "outdated_src": 1, "outdated_index": 2,
        "orphan_index": 3, "orphan_md": 4, "ok": 5,
    }
    items.sort(key=lambda it: (_priority.get(it["status"], 9), it["dst"] or ""))

    return {
        "summary": summary,
        "items": items,
        "total_sources": sum(1 for it in items if it["src"] is not None),
    }


def cleanup_orphans(dry_run: bool = True) -> dict:
    """清理孤儿数据。

    扫 compute_processing_status，对 orphan_md / orphan_index：
        - orphan_md:    删 md_file 中文件 + ChromaDB chunks + facts + 废弃 canon 规则 + 删 index_meta key
        - orphan_index: 删 ChromaDB chunks + facts + 废弃 canon 规则 + 删 index_meta key（文件已不存在）

    Args:
        dry_run: True 时只列出待删项不执行（默认安全）

    Returns:
        {
            "dry_run": bool,
            "orphan_md": [{"dst": str, "actions": [...] or "would delete"}],
            "orphan_index": [...],
            "deleted_files": int,
            "deleted_chunks": int,
            "deleted_facts": int,
            "deprecated_rules": int,
            "errors": [...],
        }
    """
    status = compute_processing_status()
    md_orphans = [it for it in status["items"] if it["status"] == "orphan_md"]
    idx_orphans = [it for it in status["items"] if it["status"] == "orphan_index"]

    result = {
        "dry_run": dry_run,
        "orphan_md": [],
        "orphan_index": [],
        "deleted_files": 0,
        "deleted_chunks": 0,
        "deleted_facts": 0,
        "deprecated_rules": 0,
        "errors": [],
    }

    if not md_orphans and not idx_orphans:
        return result

    if dry_run:
        result["orphan_md"] = [{"dst": it["dst"], "action": "would delete"} for it in md_orphans]
        result["orphan_index"] = [{"dst": it["dst"], "action": "would delete"} for it in idx_orphans]
        return result

    # 实际删除：准备共用资源
    collection = get_collection()
    facts_store = _get_facts_store()
    canon_mgr = None
    try:
        from canon_manager import CanonManager
        canon_mgr = CanonManager()
    except Exception as e:
        result["errors"].append(f"canon_manager 不可用: {e}")

    index_meta = _load_index_meta()
    meta_changed = False

    def _purge_source(source_rel: str) -> dict:
        """删除指定 source 在 ChromaDB / facts / canon / index_meta 中的痕迹。"""
        local = {"chunks": 0, "facts": 0, "rules": 0, "errors": []}
        try:
            existing = collection.get(where={"source": source_rel})
            if existing.get("ids"):
                collection.delete(ids=existing["ids"])
                local["chunks"] = len(existing["ids"])
        except Exception as e:
            local["errors"].append(f"chunks: {e}")
        try:
            local["facts"] = facts_store.delete_by_source(source_rel)
        except Exception as e:
            local["errors"].append(f"facts: {e}")
        if canon_mgr:
            try:
                rules = canon_mgr.get_rules()
                for r in rules:
                    if r.get("source") == source_rel and r.get("status") == "active":
                        canon_mgr.deprecate_rule(r["id"])
                        local["rules"] += 1
            except Exception as e:
                local["errors"].append(f"canon: {e}")
        return local

    # 1. orphan_md: 还要删文件
    import shutil  # noqa: F401  (保留以便未来扩展递归删目录)
    for it in md_orphans:
        dst_rel = it["dst"]
        dst_path = KB_DIR / dst_rel
        actions = []
        if dst_path.exists():
            try:
                dst_path.unlink()
                result["deleted_files"] += 1
                actions.append("file")
            except Exception as e:
                result["errors"].append(f"删文件 {dst_rel}: {e}")
        purge = _purge_source(dst_rel)
        result["deleted_chunks"] += purge["chunks"]
        result["deleted_facts"] += purge["facts"]
        result["deprecated_rules"] += purge["rules"]
        result["errors"].extend(purge["errors"])
        if dst_rel in index_meta:
            del index_meta[dst_rel]
            meta_changed = True
        actions.extend([
            f"chunks={purge['chunks']}", f"facts={purge['facts']}", f"rules={purge['rules']}"
        ])
        result["orphan_md"].append({"dst": dst_rel, "actions": actions})

    # 2. orphan_index: 文件已不存在，只清记录
    for it in idx_orphans:
        dst_rel = it["dst"]
        purge = _purge_source(dst_rel)
        result["deleted_chunks"] += purge["chunks"]
        result["deleted_facts"] += purge["facts"]
        result["deprecated_rules"] += purge["rules"]
        result["errors"].extend(purge["errors"])
        if dst_rel in index_meta:
            del index_meta[dst_rel]
            meta_changed = True
        result["orphan_index"].append({
            "dst": dst_rel,
            "actions": [
                f"chunks={purge['chunks']}", f"facts={purge['facts']}", f"rules={purge['rules']}"
            ],
        })

    if meta_changed:
        _save_index_meta(index_meta)
    _invalidate_bm25()
    _invalidate_metadata_cache()

    return result
