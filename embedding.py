"""嵌入向量管理 — SiliconFlow API 嵌入函数与 ChromaDB 集合管理。"""

import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import chromadb

from config import (
    get_siliconflow_client, EMBEDDING_MODEL, EMBEDDING_BATCH_SIZE,
    EMBEDDING_MAX_TOKENS, CHROMA_DIR, COLLECTION_NAME,
)


def _estimate_tokens(text: str) -> int:
    """保守估算文本 token 数（中文约 0.7 token/char）。"""
    return int(len(text) * 0.7)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """截断文本使其不超过 max_tokens（按字符反推）。"""
    max_chars = int(max_tokens / 0.7)  # 反推字符上限
    if len(text) <= max_chars:
        return text
    print(f"[WARN] 文本过长 ({len(text)} chars, ~{_estimate_tokens(text)} tokens)，截断至 {max_chars} chars", file=sys.stderr)
    return text[:max_chars]


def _build_dynamic_batches(texts: list[str]) -> list[list[str]]:
    """按 token 累计分批，每批不超过 EMBEDDING_MAX_TOKENS 且不超过 EMBEDDING_BATCH_SIZE 条。

    单条超过 MAX_TOKENS 的文本会被截断。
    """
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_tokens = 0

    for text in texts:
        # 超长单条截断保护
        text = _truncate_to_tokens(text, EMBEDDING_MAX_TOKENS)
        tokens = _estimate_tokens(text)
        if current_batch and (
            current_tokens + tokens > EMBEDDING_MAX_TOKENS
            or len(current_batch) >= EMBEDDING_BATCH_SIZE
        ):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
        current_batch.append(text)
        current_tokens += tokens

    if current_batch:
        batches.append(current_batch)

    return batches


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """调用 SiliconFlow API 获取文本嵌入向量，动态分批。"""
    client = get_siliconflow_client()
    all_embeddings = []
    for batch in _build_dynamic_batches(texts):
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        sorted_data = sorted(resp.data, key=lambda x: x.index)
        all_embeddings.extend([item.embedding for item in sorted_data])
    return all_embeddings


def _embed_texts_parallel(texts: list[str], max_workers: int = 8) -> list[list[float]]:
    """并行分批调用嵌入 API，使用动态分批。"""
    client = get_siliconflow_client()
    batches = _build_dynamic_batches(texts)

    if len(batches) <= 1:
        return _embed_texts(texts)

    total = len(batches)
    results = [None] * total

    def _embed_batch(idx: int, batch: list[str]):
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        sorted_data = sorted(resp.data, key=lambda x: x.index)
        return [item.embedding for item in sorted_data]

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_embed_batch, idx, batch): idx
            for idx, batch in enumerate(batches)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()
            completed += 1
            print(f"[INDEX] 生成嵌入: 批次 {completed}/{total} 完成", file=sys.stderr)

    return [emb for batch_result in results for emb in batch_result]


_embedding_content_cache: dict[str, list[float]] = {}  # {content_hash: embedding}


def _embed_texts_cached(texts: list[str]) -> list[list[float]]:
    """带内容缓存的嵌入函数，用 sha256(text)[:16] 做缓存键。

    缓存命中直接返回，未命中的批量调用 _embed_texts_parallel。
    """
    results: list[list[float] | None] = [None] * len(texts)
    miss_indices: list[int] = []
    miss_texts: list[str] = []

    for i, text in enumerate(texts):
        cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        cached = _embedding_content_cache.get(cache_key)
        if cached is not None:
            results[i] = cached
        else:
            miss_indices.append(i)
            miss_texts.append(text)

    if miss_texts:
        new_embeddings = _embed_texts_parallel(miss_texts)
        for j, idx in enumerate(miss_indices):
            cache_key = hashlib.sha256(miss_texts[j].encode("utf-8")).hexdigest()[:16]
            _embedding_content_cache[cache_key] = new_embeddings[j]
            results[idx] = new_embeddings[j]

    hits = len(texts) - len(miss_texts)
    if hits > 0:
        print(f"[INDEX] 嵌入缓存: {hits}/{len(texts)} 命中", file=sys.stderr)

    return results  # type: ignore[return-value]


class SiliconFlowEmbeddingFunction:
    """ChromaDB 嵌入函数，使用 SiliconFlow API。"""

    @staticmethod
    def name() -> str:
        return "siliconflow-bge-m3"

    def get_config(self) -> dict:
        return {"model_name": EMBEDDING_MODEL}

    @staticmethod
    def build_from_config(config: dict) -> "SiliconFlowEmbeddingFunction":
        return SiliconFlowEmbeddingFunction()

    def __call__(self, input: list[str]) -> list[list[float]]:
        return _embed_texts(input)

    def embed_query(self, query: str) -> list[float]:
        return _embed_texts([query])[0]


_embedding_fn = None


def get_embedding_function():
    """获取嵌入函数单例。"""
    global _embedding_fn
    if _embedding_fn is None:
        _embedding_fn = SiliconFlowEmbeddingFunction()
    return _embedding_fn


def get_collection():
    """获取或创建 ChromaDB collection（含损坏自愈）。"""
    import shutil

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    ef = get_embedding_function()

    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        return client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception as e:
        # sqlite3 损坏，删除后重建
        print(f"[WARN] ChromaDB 损坏 ({e})，自动重建", file=sys.stderr)
        try:
            shutil.rmtree(CHROMA_DIR)
        except Exception:
            # Windows 下文件可能被锁定，尝试仅删除 sqlite3
            for f in CHROMA_DIR.glob("*.sqlite3"):
                try:
                    f.unlink()
                except Exception:
                    pass
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        return client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
