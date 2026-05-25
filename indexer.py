"""索引管理器 — 兼容入口。

所有实现已拆分到 config.py, embedding.py, search_engine.py, index_manager.py。
此文件仅 re-export 公开 API，确保 server.py 等消费者无需修改。
"""

# 配置
from config import KB_DIR, DATA_DIR

# 搜索
from search_engine import search, _invalidate_bm25

# 索引管理
from index_manager import (
    index_single,
    rebuild_all,
    incremental_rebuild,
    list_modules,
    get_module_relations,
    suggest_queries,
    get_recent_changes,
    compute_processing_status,
    cleanup_orphans,
    _get_facts_store,
)
