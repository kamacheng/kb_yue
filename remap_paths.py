"""知识库索引路径重映射工具。

将 index_meta.json 和 ChromaDB 中的旧绝对路径前缀批量替换为新路径。
无需重建索引或重新 embedding。

用法:
    python remap_paths.py                          # 全自动
    python remap_paths.py --from D:\\旧 --to F:\\新  # 手动指定
    python remap_paths.py --dry-run                # 预览
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _normalize(path_str: str) -> str:
    """统一为当前 OS 的路径分隔符，避免正/反斜杠混用。"""
    return str(Path(path_str))


def _detect_old_prefix(meta: dict) -> str:
    """从 index_meta.json 的 key 推断旧 KB 根目录。

    策略：找到任意 key 中 'md_file' 部分，取其父目录作为 KB 根目录。
    退化策略：取所有 key 的公共路径的父目录。
    """
    if not meta:
        return ""

    first_key = next(iter(meta))
    parts = Path(first_key).parts

    # 找 md_file 在路径 parts 中的位置
    md_file_idx = next(
        (i for i, p in enumerate(parts) if p.lower() == "md_file"), None
    )
    if md_file_idx is not None and md_file_idx > 0:
        return str(Path(*parts[:md_file_idx]))

    # 退化：取所有 key 的公共路径（如 D:\知识库\design），再上一级得到 KB 根目录
    # 适用于文件都在 KB_ROOT 的某一个子目录内的情况（实际业务均满足）
    keys = list(meta.keys())
    try:
        common = os.path.commonpath(keys)
        return str(Path(common).parent)
    except ValueError:
        # 路径跨越不同驱动器时 commonpath 报错，退化到 parent.parent
        return str(Path(first_key).parent.parent)


def _update_index_meta(
    meta_path: Path,
    old_prefix: str,
    new_prefix: str,
    dry_run: bool,
) -> dict:
    """更新 index_meta.json 中的路径 key。

    Returns:
        {"updated": int, "backup_path": str | None}
    """
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    old_norm = _normalize(old_prefix)
    new_norm = _normalize(new_prefix)

    new_meta = {}
    updated = 0
    for k, v in meta.items():
        k_norm = _normalize(k)
        if k_norm.startswith(old_norm):
            new_k = new_norm + k_norm[len(old_norm):]
            new_meta[new_k] = v
            updated += 1
        else:
            new_meta[k] = v

    if dry_run or updated == 0:
        return {"updated": updated, "backup_path": None}

    # 备份原文件
    backup_path = Path(str(meta_path) + ".bak")
    if not backup_path.exists():
        backup_path.write_text(
            meta_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    meta_path.write_text(
        json.dumps(new_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"updated": updated, "backup_path": str(backup_path)}


_CHROMA_UPDATE_BATCH = 1000


def _update_collection(collection, old_prefix: str, new_prefix: str, dry_run: bool) -> dict:
    """更新 ChromaDB collection 中所有 chunk 的 source 字段。

    只修改 metadata，不动 embeddings/documents/ids。

    Returns:
        {"updated": int}  # dry_run=True 时为将被更新的条数；dry_run=False 时为实际更新的条数
    """
    old_norm = _normalize(old_prefix)
    new_norm = _normalize(new_prefix)

    all_items = collection.get(include=["metadatas"])
    ids_to_update = []
    new_metadatas = []

    for i, meta in enumerate(all_items["metadatas"]):
        old_source = meta.get("source", "")
        old_source_norm = _normalize(old_source)
        if old_source_norm.startswith(old_norm):
            new_source = new_norm + old_source_norm[len(old_norm):]
            ids_to_update.append(all_items["ids"][i])
            updated_meta = dict(meta)
            updated_meta["source"] = new_source
            new_metadatas.append(updated_meta)

    if dry_run or not ids_to_update:
        return {"updated": len(ids_to_update)}

    # 分批更新
    total = len(ids_to_update)
    for i in range(0, total, _CHROMA_UPDATE_BATCH):
        end = min(i + _CHROMA_UPDATE_BATCH, total)
        collection.update(
            ids=ids_to_update[i:end],
            metadatas=new_metadatas[i:end],
        )
        print(f"  已更新 {end}/{total}", file=sys.stderr)

    return {"updated": total}


def remap_paths(
    index_meta_path: Path,
    old_prefix: str | None,
    new_prefix: str,
    dry_run: bool,
    get_kb_collection,
    get_facts_collection,
) -> dict:
    """主编排函数：按顺序更新 index_meta → ChromaDB KB → ChromaDB Facts。

    Args:
        index_meta_path: index_meta.json 的路径
        old_prefix: 旧 KB 根目录（None 则自动检测）
        new_prefix: 新 KB 根目录
        dry_run: 预览模式，不修改任何文件
        get_kb_collection: 无参函数，返回 game_design_kb collection（或 None 跳过）
        get_facts_collection: 无参函数，返回 game_design_facts collection（或 None 跳过）

    Returns:
        {"status": "success"|"skipped"|"error", ...}
    """
    if not index_meta_path.exists():
        return {"status": "error", "message": "index_meta.json 不存在，索引可能尚未建立"}

    meta = json.loads(index_meta_path.read_text(encoding="utf-8"))
    if not meta:
        return {"status": "skipped", "message": "index_meta.json 为空，无需迁移"}

    # 自动检测旧前缀
    if old_prefix is None:
        old_prefix = _detect_old_prefix(meta)
        if not old_prefix:
            return {"status": "error", "message": "无法自动检测旧 KB 路径，请用 --from 明确指定"}

    old_norm = _normalize(old_prefix)
    new_norm = _normalize(new_prefix)

    if old_norm == new_norm:
        return {"status": "skipped", "message": "路径前缀相同，无需迁移"}

    print(f"检测到旧 KB 根目录: {old_norm}", file=sys.stderr)
    print(f"目标新 KB 根目录:   {new_norm}", file=sys.stderr)

    # Phase 1: 更新 index_meta.json
    meta_result = _update_index_meta(
        meta_path=index_meta_path,
        old_prefix=old_prefix,
        new_prefix=new_prefix,
        dry_run=dry_run,
    )
    print(
        f"index_meta.json: {len(meta)} 条记录，{meta_result['updated']} 条需要更新",
        file=sys.stderr,
    )
    if meta_result["updated"] == 0:
        print("[WARN] 未找到匹配条目，请检查 --from 路径是否正确", file=sys.stderr)

    backup_path = meta_result["backup_path"]

    # Phase 2 & 3: 更新 ChromaDB
    kb_updated = 0
    facts_updated = 0

    try:
        kb_col = get_kb_collection()
        if kb_col is not None:
            kb_result = _update_collection(kb_col, old_prefix, new_prefix, dry_run)
            kb_updated = kb_result["updated"]
            print(
                f"ChromaDB [game_design_kb]: {kb_updated} 个 chunks 需要更新",
                file=sys.stderr,
            )

        facts_col = get_facts_collection()
        if facts_col is not None:
            facts_result = _update_collection(facts_col, old_prefix, new_prefix, dry_run)
            facts_updated = facts_result["updated"]
            print(
                f"ChromaDB [game_design_facts]: {facts_updated} 条事实需要更新",
                file=sys.stderr,
            )

    except Exception as e:
        # 回滚 index_meta.json
        if backup_path:
            bak = Path(backup_path)
            if bak.exists():
                index_meta_path.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
                print("[ROLLBACK] 已还原 index_meta.json", file=sys.stderr)
        return {"status": "error", "message": str(e)}

    label = "(dry-run)" if dry_run else ""
    print(
        f"✓ 迁移完成{label}: index_meta {meta_result['updated']} 条, "
        f"kb chunks {kb_updated} 个, facts {facts_updated} 条",
        file=sys.stderr,
    )

    return {
        "status": "success",
        "dry_run": dry_run,
        "old_prefix": old_norm,
        "new_prefix": new_norm,
        "meta_updated": meta_result["updated"],
        "kb_updated": kb_updated,
        "facts_updated": facts_updated,
    }


def _get_kb_collection_real():
    """获取生产环境的 game_design_kb collection。"""
    from embedding import get_collection
    return get_collection()


def _get_facts_collection_real():
    """获取生产环境的 game_design_facts collection。"""
    import chromadb
    from config import CHROMA_DIR
    from facts_store import FACTS_COLLECTION
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    try:
        return client.get_collection(FACTS_COLLECTION)
    except Exception as e:
        print(f"[WARN] facts collection 不存在或获取失败，跳过: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    import argparse
    from config import INDEX_META_PATH, KB_DIR

    parser = argparse.ArgumentParser(
        description="重映射知识库索引中的路径前缀（无需重建索引）"
    )
    parser.add_argument("--from", dest="old_prefix", help="旧 KB 根目录路径（默认自动检测）")
    parser.add_argument(
        "--to", dest="new_prefix", help="新 KB 根目录路径（默认使用 config.json 的 kb_root）"
    )
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不修改文件")
    parser.add_argument("--meta-path", help="指定 index_meta.json 路径（测试用）")
    parser.add_argument("--skip-chroma", action="store_true", help="跳过 ChromaDB 更新（测试用）")
    args = parser.parse_args()

    meta_path = Path(args.meta_path) if args.meta_path else INDEX_META_PATH
    new_prefix = args.new_prefix or str(KB_DIR)

    if args.skip_chroma:
        get_kb = lambda: None
        get_facts = lambda: None
    else:
        get_kb = _get_kb_collection_real
        get_facts = _get_facts_collection_real

    result = remap_paths(
        index_meta_path=meta_path,
        old_prefix=args.old_prefix,
        new_prefix=new_prefix,
        dry_run=args.dry_run,
        get_kb_collection=get_kb,
        get_facts_collection=get_facts,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] in ("success", "skipped") else 1)
