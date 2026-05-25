"""搜索引擎 — 混合搜索（Vector + BM25）、查询分类、重排序、高亮。

从 indexer.py 提取，统一管理所有搜索相关逻辑。
"""

import sys
from enum import Enum
from pathlib import Path

import numpy as np

from config import (
    VECTOR_WEIGHT, BM25_WEIGHT, RERANKER_MODEL, USE_RERANKER,
    EMBEDDING_API_BASE, get_env, get_deepseek_client,
)
from embedding import get_embedding_function, get_collection


# ---------- 查询分类 ----------

class QueryType(Enum):
    PRECISE = "precise"
    EXPLORE = "explore"
    RELATION = "relation"

_EXPLORE_KEYWORDS = {"相关", "关于", "涉及", "有关", "包含", "有哪些", "哪些文档"}
_RELATION_KEYWORDS = {"依赖", "调用", "关联", "关系", "哪些模块", "什么模块", "影响", "被调用"}


def classify_query(query: str) -> QueryType:
    """根据查询文本分类查询类型（规则，非 LLM）。"""
    for kw in _RELATION_KEYWORDS:
        if kw in query:
            return QueryType.RELATION
    for kw in _EXPLORE_KEYWORDS:
        if kw in query:
            return QueryType.EXPLORE
    if len(query) < 10:
        return QueryType.PRECISE
    return QueryType.EXPLORE


# ---------- BM25 索引 ----------

_bm25_index = None
_bm25_corpus_ids = None
_bm25_valid = False
_bm25_token_cache: dict[str, list[str]] = {}  # {chunk_id: [tokens]}


def _invalidate_bm25():
    """索引变更后使 BM25 缓存失效（保留分词缓存以供复用）。"""
    global _bm25_index, _bm25_corpus_ids, _bm25_valid
    _bm25_index = None
    _bm25_corpus_ids = None
    _bm25_valid = False
    # 注意：不清空 _bm25_token_cache，重建时复用已缓存的分词结果


def _ensure_bm25():
    """延迟构建 BM25 索引（首次搜索时从 ChromaDB 加载）。"""
    global _bm25_index, _bm25_corpus_ids, _bm25_valid, _bm25_token_cache

    if _bm25_valid:
        return _bm25_index, _bm25_corpus_ids

    try:
        import jieba
        from rank_bm25 import BM25Okapi
    except ImportError:
        print("[WARN] jieba 或 rank_bm25 未安装，BM25 搜索不可用", file=sys.stderr)
        _bm25_valid = True
        return None, None

    collection = get_collection()
    count = collection.count()
    if count == 0:
        _bm25_valid = True
        return None, None

    all_data = collection.get(include=["documents"])
    docs = all_data["documents"]
    ids = all_data["ids"]

    # 使用分词缓存：命中复用，未命中调用 jieba.cut
    current_ids = set(ids)
    cache_hits = 0
    tokenized = []
    for chunk_id, doc in zip(ids, docs):
        if chunk_id in _bm25_token_cache:
            tokenized.append(_bm25_token_cache[chunk_id])
            cache_hits += 1
        else:
            tokens = list(jieba.cut(doc))
            _bm25_token_cache[chunk_id] = tokens
            tokenized.append(tokens)

    # 清理缓存中已删除的 chunk_id
    stale_keys = set(_bm25_token_cache.keys()) - current_ids
    for key in stale_keys:
        del _bm25_token_cache[key]

    _bm25_index = BM25Okapi(tokenized)
    _bm25_corpus_ids = ids
    _bm25_valid = True
    print(f"[INFO] BM25 索引构建完成，{len(ids)} 条文档（分词缓存命中 {cache_hits}）", file=sys.stderr)
    return _bm25_index, _bm25_corpus_ids


def _bm25_search(query: str, top_k: int) -> dict[str, float]:
    """BM25 搜索，返回 {chunk_id: score}。"""
    bm25, corpus_ids = _ensure_bm25()
    if bm25 is None or corpus_ids is None:
        return {}

    try:
        import jieba
    except ImportError:
        return {}

    tokenized_query = list(jieba.cut(query))
    scores = bm25.get_scores(tokenized_query)

    # 归一化到 [0, 1]
    max_score = float(np.max(scores)) if len(scores) > 0 else 0
    if max_score > 0:
        norm_scores = scores / max_score
    else:
        norm_scores = scores

    # 取 top_k
    top_indices = np.argsort(norm_scores)[::-1][:top_k]
    result = {}
    for idx in top_indices:
        if norm_scores[idx] > 0:
            result[corpus_ids[idx]] = float(norm_scores[idx])
    return result


# ---------- 查询重写 ----------

def _rewrite_query(query: str) -> str:
    """用 LLM 将查询扩展为同义词组，提升召回。

    使用 config.get_deepseek_client() 获取 DeepSeek API 客户端。
    失败时静默返回原始查询。
    """
    try:
        client = get_deepseek_client()

        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": (
                    "你是搜索查询扩展助手。将用户的查询扩展为包含同义词和相关术语的版本，"
                    "用于在游戏设计知识库中检索文档。直接返回扩展后的查询文本，不要解释。"
                    "保持简洁，不超过50字。"
                )},
                {"role": "user", "content": query},
            ],
            temperature=0.3,
            max_tokens=100,
        )
        rewritten = response.choices[0].message.content.strip()
        if rewritten:
            return rewritten
    except Exception as e:
        print(f"[WARN] 查询重写失败 ({e})，使用原始查询", file=sys.stderr)

    return query


# ---------- Reranker ----------

def _call_reranker_api(query: str, documents: list[str], doc_ids: list[str]) -> dict[str, float]:
    """调用 SiliconFlow Reranker API，返回 {doc_id: relevance_score}。"""
    import requests

    api_key = get_env("SILICONFLOW_API_KEY")

    resp = requests.post(
        f"{EMBEDDING_API_BASE}/rerank",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": RERANKER_MODEL,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    scores = {}
    for item in data.get("results", []):
        idx = item["index"]
        if idx < len(doc_ids):
            scores[doc_ids[idx]] = item["relevance_score"]
    return scores


def _rerank_results(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """用 Reranker 对候选结果重排序。失败时降级到原始分数排序。"""
    if not candidates:
        return []

    if not USE_RERANKER:
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:top_k]

    try:
        doc_ids = [c["id"] for c in candidates]
        documents = [c["text"] for c in candidates]
        reranker_scores = _call_reranker_api(query, documents, doc_ids)

        for c in candidates:
            c["reranker_score"] = reranker_scores.get(c["id"], 0)

        candidates.sort(key=lambda x: x["reranker_score"], reverse=True)
        return candidates[:top_k]

    except Exception as e:
        print(f"[WARN] Reranker 失败 ({e})，降级到原始分数排序", file=sys.stderr)
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:top_k]


# ---------- 高亮 ----------

def _highlight_text(text: str, query: str) -> str:
    """在文本中高亮与查询最相关的句子。使用 jieba 分词匹配。"""
    try:
        import jieba
    except ImportError:
        return text

    query_words = set(jieba.cut(query))
    # 去掉常见停用词
    stopwords = {"的", "了", "在", "是", "和", "与", "有", "为", "等", "及", "或"}
    query_words -= stopwords
    query_words = {w for w in query_words if len(w) > 1}

    if not query_words:
        return text

    sentences = text.replace("\n", "。").split("。")
    highlighted = []
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        sent_words = set(jieba.cut(sent))
        overlap = query_words & sent_words
        if overlap:
            highlighted.append(f"**{sent}**")
        else:
            highlighted.append(sent)

    return "。".join(highlighted)


# ---------- 引用格式化 ----------

def _format_citation(meta: dict) -> dict:
    """从 chunk 元数据生成结构化引用。"""
    source = meta.get("source", "")
    section = meta.get("section", "")
    module = meta.get("module", "")
    doc_version = meta.get("doc_version", "")
    doc_mtime = meta.get("doc_mtime", "")

    # 提取文件名
    source_name = Path(source).name if source else "未知来源"
    # 格式化日期
    date_str = doc_mtime[:10] if doc_mtime else ""

    # 构建 formatted 字符串
    parts = [f"参考自: {source_name}"]
    if section:
        parts[0] += f" > {section}"
    if date_str:
        parts[0] += f" ({date_str})"

    return {
        "source": source,
        "section": section,
        "module": module,
        "doc_version": doc_version,
        "doc_mtime": date_str,
        "formatted": f"[{parts[0]}]",
    }


# ---------- 邻近 chunk 上下文 ----------

def _get_neighbor_chunks_batch(
    collection, items: list[dict], window: int = 1
) -> dict[str, dict]:
    """批量获取多个结果的邻近 chunks。返回 {result_id: {"prev": [...], "next": [...]}}。"""
    from collections import defaultdict

    queries = []  # (result_id, doc_id, target_index, direction)
    for entry in items:
        meta = entry["meta"]
        doc_id = meta.get("doc_id", "")
        chunk_index = meta.get("chunk_index")
        if not doc_id or chunk_index is None:
            continue
        chunk_index = int(chunk_index)
        for offset in range(-window, window + 1):
            if offset == 0:
                continue
            target = chunk_index + offset
            if target < 0:
                continue
            direction = "prev" if offset < 0 else "next"
            queries.append((entry["id"], doc_id, target, direction))

    if not queries:
        return {}

    # 按 doc_id 分组，减少重复查询
    doc_targets = defaultdict(set)
    for _, doc_id, target, _ in queries:
        doc_targets[doc_id].add(target)

    chunk_lookup = {}  # (doc_id, chunk_index) -> text
    for doc_id, target_indices in doc_targets.items():
        for target_idx in target_indices:
            try:
                result = collection.get(
                    where={"$and": [{"doc_id": doc_id}, {"chunk_index": target_idx}]},
                    include=["documents"],
                )
                if result["documents"]:
                    chunk_lookup[(doc_id, target_idx)] = result["documents"][0][:300]
            except Exception:
                pass

    contexts = defaultdict(lambda: {"prev": [], "next": []})
    for result_id, doc_id, target, direction in queries:
        text = chunk_lookup.get((doc_id, target))
        if text:
            contexts[result_id][direction].append(text)

    return dict(contexts)


# ---------- BM25 元数据辅助 ----------

def _fetch_bm25_metadata(
    collection, bm25_scores: dict[str, float],
    module: str | None = None, doc_type: str | None = None,
) -> tuple[dict[str, float], dict[str, dict]]:
    """获取 BM25 命中 chunk 的元数据，按条件过滤。返回 (过滤后分数, chunk数据)。"""
    chunk_ids = list(bm25_scores.keys())
    if not chunk_ids:
        return {}, {}
    try:
        data = collection.get(ids=chunk_ids, include=["documents", "metadatas"])
    except Exception:
        return {}, {}
    # module 过滤展开为别名集合，与 ChromaDB where 的 $in 行为保持一致
    module_aliases: set[str] | None = None
    if module:
        from module_aliases import expand_aliases
        module_aliases = set(expand_aliases(module))

    filtered_scores = {}
    chunk_data = {}
    for i, cid in enumerate(data["ids"]):
        meta = data["metadatas"][i]
        if module_aliases and meta.get("module") not in module_aliases:
            continue
        if doc_type and meta.get("doc_type") != doc_type:
            continue
        filtered_scores[cid] = bm25_scores[cid]
        chunk_data[cid] = {"text": data["documents"][i], "meta": meta}
    return filtered_scores, chunk_data


# ---------- 精确匹配 ----------

EXACT_MATCH_BOOST = 0.3

def _exact_match_filter(entities: list[str], candidates: list[dict]) -> set[str]:
    """对实体进行精确匹配，返回命中的 chunk ID 集合。"""
    if not entities:
        return set()

    matched = set()
    for c in candidates:
        meta = c.get("meta", {})
        for entity in entities:
            if meta.get("doc_id", "") == entity:
                matched.add(c["id"])
                break
            if meta.get("module", "") == entity:
                matched.add(c["id"])
                break
            if entity in meta.get("section", ""):
                matched.add(c["id"])
                break
            if entity in c.get("text", ""):
                matched.add(c["id"])
                break

    return matched


# ---------- 子查询合并 ----------

MULTI_HIT_BOOST = 0.1

def _merge_sub_results(sub_results: list[list[dict]]) -> list[dict]:
    """合并多个子查询的搜索结果。"""
    if len(sub_results) == 1:
        return sub_results[0]

    best: dict[str, dict] = {}
    hit_count: dict[str, int] = {}

    for results in sub_results:
        for item in results:
            cid = item["id"]
            hit_count[cid] = hit_count.get(cid, 0) + 1
            if cid not in best or item["score"] > best[cid]["score"]:
                best[cid] = dict(item)

    for cid, item in best.items():
        extra_hits = hit_count[cid] - 1
        if extra_hits > 0:
            item["score"] += extra_hits * MULTI_HIT_BOOST

    merged = sorted(best.values(), key=lambda x: x["score"], reverse=True)
    return merged


# ---------- 多跳扩展 ----------

import json as _json

def _extract_cross_ref_modules(
    results: list[dict],
    exclude_module: str | None = None,
) -> set[str]:
    """从搜索结果的 cross_refs 中提取被引用的模块名。"""
    modules = set()
    for r in results:
        meta = r.get("meta", {})
        refs_raw = meta.get("cross_refs", "[]")
        try:
            refs = _json.loads(refs_raw) if isinstance(refs_raw, str) else refs_raw
        except (ValueError, TypeError):
            refs = []
        for ref in refs:
            if isinstance(ref, str) and ref:
                modules.add(ref)
    if exclude_module:
        modules.discard(exclude_module)
    return modules


# ---------- 单次查询搜索（向量 + BM25 混合）----------

def _single_query_search(
    vector_query: str,
    bm25_query: str,
    collection,
    ef,
    fetch_k: int,
    where_filter,
    v_weight: float,
    b_weight: float,
    module: str | None,
    doc_type: str | None,
) -> list[dict]:
    """执行单次向量 + BM25 搜索，返回合并后的候选列表。

    Returns:
        [{"id": ..., "text": ..., "meta": ..., "score": ...}, ...]
    """
    # 向量搜索
    query_embedding = ef.embed_query(vector_query)

    query_params = {
        "query_embeddings": [query_embedding],
        "n_results": fetch_k,
        "include": ["documents", "metadatas", "distances"],
    }
    if where_filter:
        query_params["where"] = where_filter

    vector_results = collection.query(**query_params)

    # 收集向量搜索得分 {id: normalized_score}
    vector_scores: dict[str, float] = {}
    vector_data: dict[str, dict] = {}

    if vector_results["documents"] and vector_results["documents"][0]:
        for i, doc in enumerate(vector_results["documents"][0]):
            chunk_id = vector_results["ids"][0][i]
            meta = vector_results["metadatas"][0][i]
            distance = vector_results["distances"][0][i]
            score = round(1 - distance / 2, 4)
            vector_scores[chunk_id] = score
            vector_data[chunk_id] = {"text": doc, "meta": meta}

    # BM25 搜索（使用原始查询保持精确匹配）
    bm25_scores = _bm25_search(bm25_query, fetch_k)

    # 统一获取 BM25 命中的元数据并过滤
    bm25_scores, bm25_chunk_data = _fetch_bm25_metadata(
        collection, bm25_scores, module=module, doc_type=doc_type
    )
    # 合并 BM25 独有的 chunk 数据到 vector_data
    for cid, data in bm25_chunk_data.items():
        if cid not in vector_data:
            vector_data[cid] = data

    # 混合评分
    all_ids = set(vector_scores.keys()) | set(bm25_scores.keys())
    combined: list[dict] = []

    for cid in all_ids:
        v_score = vector_scores.get(cid, 0)
        b_score = bm25_scores.get(cid, 0)
        final = v_weight * v_score + b_weight * b_score
        data = vector_data.get(cid)
        if data:
            combined.append({
                "id": cid,
                "text": data["text"],
                "meta": data["meta"],
                "score": final,
            })

    return combined


# ---------- 主搜索函数 ----------

def search(
    query: str,
    top_k: int = 5,
    include_context: bool = False,
    module: str | None = None,
    doc_type: str | None = None,
    rewrite: bool = False,
    multi_hop: bool = False,
) -> list[dict]:
    """多阶段搜索管道（Vector + BM25 + 实体精确匹配 + Reranker）。

    Args:
        query: 搜索查询文本
        top_k: 返回最相关的 K 条结果
        include_context: 是否包含邻近 chunk 上下文
        module: 按模块过滤
        doc_type: 按文档类型过滤
        rewrite: 是否使用 LLM 重写查询（有 200-500ms 延迟）
        multi_hop: 是否启用多跳扩展（需同时设置 include_context=True）

    Returns:
        [{text, module, doc_type, source, section, score, heading_chain, context?, related_modules?}, ...]
    """
    collection = get_collection()
    count = collection.count()
    if count == 0:
        return []

    # Stage 0: 查询理解（实体提取 + 分解判断）
    from query_analyzer import extract_entities, should_decompose, decompose_query_local
    entities = extract_entities(query)
    sub_queries = None
    if should_decompose(query):
        sub_queries = decompose_query_local(query)

    # 查询分类 → 动态权重
    query_type = classify_query(query)
    if query_type == QueryType.PRECISE:
        v_weight, b_weight = 0.5, 0.5
    elif query_type == QueryType.EXPLORE:
        v_weight, b_weight = 0.8, 0.2
    else:
        v_weight, b_weight = VECTOR_WEIGHT, BM25_WEIGHT

    # 构建 ChromaDB where 过滤条件
    # chunks 层存原始 module，按 module 过滤时展开为全部别名以覆盖同义命名
    from module_aliases import expand_aliases
    where_filter = None
    conditions = []
    if module:
        aliases = expand_aliases(module)
        if len(aliases) == 1:
            conditions.append({"module": aliases[0]})
        else:
            conditions.append({"module": {"$in": aliases}})
    if doc_type:
        conditions.append({"doc_type": doc_type})
    if len(conditions) == 1:
        where_filter = conditions[0]
    elif len(conditions) > 1:
        where_filter = {"$and": conditions}

    fetch_k = min(top_k * 3, count)
    ef = get_embedding_function()

    # Stage 2: 语义召回（含可选的查询分解）
    if sub_queries and len(sub_queries) > 1:
        sub_results_list = []
        for sq in sub_queries:
            sq_vector_query = _rewrite_query(sq.text) if rewrite else sq.text
            sq_results = _single_query_search(
                sq_vector_query, sq.text, collection, ef,
                fetch_k, where_filter, v_weight, b_weight, module, doc_type,
            )
            sub_results_list.append(sq_results)
        combined = _merge_sub_results(sub_results_list)
    else:
        vector_query = _rewrite_query(query) if rewrite else query
        combined = _single_query_search(
            vector_query, query, collection, ef,
            fetch_k, where_filter, v_weight, b_weight, module, doc_type,
        )

    # Stage 1: 精确匹配加分（+0.15）
    if entities:
        exact_ids = _exact_match_filter(entities, combined)
        for c in combined:
            if c["id"] in exact_ids:
                c["score"] += 0.15

    # Stage 3: Reranker 重排序
    if USE_RERANKER and combined:
        combined = _rerank_results(query, combined, top_k)
    else:
        combined.sort(key=lambda x: x["score"], reverse=True)
        combined = combined[:top_k]

    # Stage 4: 上下文组装（批量邻近 chunk 查询）
    neighbor_contexts = {}
    if include_context:
        neighbor_contexts = _get_neighbor_chunks_batch(collection, combined)

    # 加载法典规则（一次性读取，避免循环内重复 IO）
    canon_rules = []
    try:
        from canon_manager import CanonManager
        canon_mgr = CanonManager()
        canon_rules = canon_mgr.get_rules(status="active")
    except Exception:
        pass  # 法典不可用时不影响搜索

    # 构建结果
    items = []
    for entry in combined:
        meta = entry["meta"]
        item = {
            "text": entry["text"][:500],
            "module": meta.get("module", ""),
            "doc_type": meta.get("doc_type", ""),
            "source": meta.get("source", ""),
            "section": meta.get("section", ""),
            "score": round(entry.get("reranker_score", entry["score"]), 4),
            "heading_chain": meta.get("heading_chain", ""),
        }
        item["highlight"] = _highlight_text(entry["text"][:500], query)
        item["citation"] = _format_citation(meta)

        if include_context and entry["id"] in neighbor_contexts:
            ctx = neighbor_contexts[entry["id"]]
            if ctx["prev"] or ctx["next"]:
                item["context"] = ctx

        # 多跳扩展：添加关联模块
        if multi_hop and include_context:
            cross_modules = _extract_cross_ref_modules([entry], exclude_module=module)
            if cross_modules:
                item["related_modules"] = sorted(cross_modules)

        # 法典引用注入
        if canon_rules:
            canon_refs = []
            text_lower = entry["text"].lower()
            section_str = meta.get("section", "")
            for rule in canon_rules:
                if rule["subject"] in text_lower or rule["subject"] in section_str:
                    canon_refs.append({
                        "rule_id": rule["id"],
                        "subject": rule["subject"],
                        "value": rule["value"],
                        "priority": rule["priority"],
                    })
            if canon_refs:
                item["canon_refs"] = canon_refs

        items.append(item)

    return items
