"""事实存储 — 使用 ChromaDB 存储和检索结构化设计事实。"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import chromadb

from fact_extractor import DesignFact

FACTS_COLLECTION = "game_design_facts"


class FactsStore:
    def __init__(self, chroma_dir: str | Path | None = None):
        if chroma_dir is None:
            self._client = chromadb.EphemeralClient()
        else:
            self._client = chromadb.PersistentClient(path=str(chroma_dir))

        self._collection = self._client.get_or_create_collection(
            name=FACTS_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    @staticmethod
    def _fact_content_id(f: DesignFact) -> str:
        """基于事实内容生成稳定 ID，相同三元组自然去重。"""
        key = f"{f.type}|{f.subject}|{f.predicate}|{f.value}"
        return "fact_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:12]

    def _build_existing_index(self) -> dict[str, list[dict]]:
        """一次性读取全部 metadata，构建 {type: [{subject, predicate, value}]} 内存索引。"""
        index: dict[str, list[dict]] = {}
        try:
            all_data = self._collection.get(include=["metadatas"])
            for meta in all_data.get("metadatas", []):
                t = meta.get("type", "")
                entry = {
                    "subject": meta.get("subject", ""),
                    "predicate": meta.get("predicate", ""),
                    "value": meta.get("value", ""),
                }
                index.setdefault(t, []).append(entry)
        except Exception as e:
            print(f"[WARN] 构建事实内存索引失败，语义去重将不生效: {e}", file=sys.stderr)
        return index

    @staticmethod
    def _normalize_predicate(pred: str) -> str:
        """去标点归一化 predicate。"""
        return re.sub(r'[\s，。、：:；;！!？?\-—]+', '', pred)

    @staticmethod
    def _check_semantic_duplicate_inmemory(fact: DesignFact, index: dict[str, list[dict]]) -> bool:
        """纯内存语义去重：同 type + subject 子串匹配 + predicate 归一化匹配。"""
        # 空 subject 或 predicate 的事实跳过去重（无法可靠匹配）
        if not fact.subject or not fact.predicate:
            return False

        entries = index.get(fact.type, [])
        if not entries:
            return False

        norm_new_pred = FactsStore._normalize_predicate(fact.predicate)
        if not norm_new_pred:
            return False

        for entry in entries:
            existing_subject = entry["subject"]
            # 跳过空 subject 的已有条目
            if not existing_subject:
                continue
            # subject 子串匹配（任一方包含另一方，且短方至少2字符）
            shorter = min(len(fact.subject), len(existing_subject))
            if shorter < 2:
                continue
            if not (fact.subject in existing_subject or existing_subject in fact.subject):
                continue

            # predicate 归一化匹配
            norm_existing_pred = FactsStore._normalize_predicate(entry["predicate"])
            if not norm_existing_pred:
                continue
            if norm_existing_pred == norm_new_pred:
                return True
            # predicate 子串匹配（短方至少2字符）
            pred_shorter = min(len(norm_new_pred), len(norm_existing_pred))
            if pred_shorter >= 2 and (norm_new_pred in norm_existing_pred or norm_existing_pred in norm_new_pred):
                return True

        return False

    def _check_semantic_duplicate(self, fact: DesignFact) -> bool:
        """检查事实库中是否已存在语义重复的事实。

        条件：同 type + 余弦相似度 ≥ 0.85 + subject 子串匹配
        """
        if self._collection.count() == 0:
            return False

        try:
            results = self._collection.query(
                query_texts=[fact.to_text()],
                n_results=min(3, self._collection.count()),
                where={"type": fact.type},
                include=["metadatas", "distances"],
            )
        except Exception:
            return False

        if not results["metadatas"] or not results["metadatas"][0]:
            return False

        for i, meta in enumerate(results["metadatas"][0]):
            # ChromaDB cosine distance: 0 = 完全相同, 2 = 完全相反
            # 相似度 = 1 - distance/2; 阈值 0.85 → distance < 0.30
            distance = results["distances"][0][i] if results["distances"] else 1.0
            if distance >= 0.30:
                continue

            existing_subject = meta.get("subject", "")
            # subject 子串匹配（任一方包含另一方）
            if (fact.subject in existing_subject or existing_subject in fact.subject):
                return True

        return False

    def add_facts(self, facts: list[DesignFact], source: str, module: str,
                  semantic_dedup: bool = True) -> int:
        if not facts:
            return 0

        # batch 内去重：相同内容只保留第一条
        seen_ids: dict[str, int] = {}
        unique_facts: list[DesignFact] = []
        for f in facts:
            fid = self._fact_content_id(f)
            if fid not in seen_ids:
                seen_ids[fid] = len(unique_facts)
                unique_facts.append(f)

        # 语义去重：使用内存索引，避免边读边写导致 HNSW 损坏
        if semantic_dedup:
            mem_index = self._build_existing_index()
            non_dup_facts = []
            for f in unique_facts:
                if not self._check_semantic_duplicate_inmemory(f, mem_index):
                    non_dup_facts.append(f)
                    # 实时加入索引，避免同批次内的重复
                    mem_index.setdefault(f.type, []).append({
                        "subject": f.subject,
                        "predicate": f.predicate,
                        "value": f.value,
                    })
            skipped = len(unique_facts) - len(non_dup_facts)
            if skipped > 0:
                print(f"[DEDUP] 语义去重跳过 {skipped} 条重复事实", file=sys.stderr)
            unique_facts = non_dup_facts

        if not unique_facts:
            return 0

        ids = [self._fact_content_id(f) for f in unique_facts]
        documents = [f.to_text() for f in unique_facts]
        metadatas = [
            {
                "type": f.type,
                "subject": f.subject,
                "predicate": f.predicate,
                "value": f.value,
                "confidence": f.confidence,
                "source": source,
                "module": module,
            }
            for f in unique_facts
        ]

        # upsert 替代 add：相同内容哈希 ID 自动覆盖，实现跨文件去重
        self._collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        return len(unique_facts)

    def add_facts_batch(self, fact_items: list[dict], semantic_dedup: bool = True) -> int:
        """批量写入多文件事实，单次内存去重 + 单次 upsert。

        Args:
            fact_items: [{"fact": DesignFact, "source": str, "module": str}, ...]
            semantic_dedup: 是否进行语义去重

        Returns:
            实际写入的事实数
        """
        if not fact_items:
            return 0

        # 1. 内容 ID 去重
        seen_ids: dict[str, int] = {}
        unique_items: list[dict] = []
        for item in fact_items:
            fid = self._fact_content_id(item["fact"])
            if fid not in seen_ids:
                seen_ids[fid] = len(unique_items)
                unique_items.append(item)

        # 2. 内存语义去重
        if semantic_dedup:
            mem_index = self._build_existing_index()
            non_dup_items = []
            for item in unique_items:
                f = item["fact"]
                if not self._check_semantic_duplicate_inmemory(f, mem_index):
                    non_dup_items.append(item)
                    mem_index.setdefault(f.type, []).append({
                        "subject": f.subject,
                        "predicate": f.predicate,
                        "value": f.value,
                    })
            skipped = len(unique_items) - len(non_dup_items)
            if skipped > 0:
                print(f"[DEDUP] 批量语义去重跳过 {skipped} 条重复事实", file=sys.stderr)
            unique_items = non_dup_items

        if not unique_items:
            return 0

        # 3. 单次 upsert
        ids = [self._fact_content_id(item["fact"]) for item in unique_items]
        documents = [item["fact"].to_text() for item in unique_items]
        metadatas = [
            {
                "type": item["fact"].type,
                "subject": item["fact"].subject,
                "predicate": item["fact"].predicate,
                "value": item["fact"].value,
                "confidence": item["fact"].confidence,
                "source": item["source"],
                "module": item["module"],
            }
            for item in unique_items
        ]

        # 分批 upsert，ChromaDB 单次上限约 5000
        BATCH = 5000
        for i in range(0, len(ids), BATCH):
            end = min(i + BATCH, len(ids))
            self._collection.upsert(
                ids=ids[i:end], documents=documents[i:end], metadatas=metadatas[i:end]
            )

        return len(unique_items)

    def search_facts(self, query: str, top_k: int = 10,
                     module: str | None = None,
                     fact_type: str | None = None) -> list[dict]:
        from module_aliases import expand_aliases

        where_filter = None
        conditions = []
        if module:
            # 事实存的是原始 module，查询时展开为全部别名以覆盖同义命名
            aliases = expand_aliases(module)
            if len(aliases) == 1:
                conditions.append({"module": aliases[0]})
            else:
                conditions.append({"module": {"$in": aliases}})
        if fact_type:
            conditions.append({"type": fact_type})
        if len(conditions) == 1:
            where_filter = conditions[0]
        elif len(conditions) > 1:
            where_filter = {"$and": conditions}

        query_params = {
            "query_texts": [query],
            "n_results": min(top_k, self._collection.count() or 1),
        }
        if where_filter:
            query_params["where"] = where_filter

        try:
            results = self._collection.query(**query_params)
        except Exception:
            return []

        items = []
        if results["metadatas"] and results["metadatas"][0]:
            for i, meta in enumerate(results["metadatas"][0]):
                items.append({
                    "subject": meta.get("subject", ""),
                    "predicate": meta.get("predicate", ""),
                    "value": meta.get("value", ""),
                    "type": meta.get("type", ""),
                    "module": meta.get("module", ""),
                    "source": meta.get("source", ""),
                    "confidence": meta.get("confidence", 0),
                    "text": results["documents"][0][i] if results["documents"] else "",
                })
        return items

    def get_facts_by_module(self, module: str) -> list[dict]:
        from module_aliases import expand_aliases

        aliases = expand_aliases(module)
        where = {"module": aliases[0]} if len(aliases) == 1 else {"module": {"$in": aliases}}
        try:
            results = self._collection.get(where=where, include=["metadatas"])
        except Exception:
            return []

        return [
            {
                "subject": m.get("subject", ""),
                "predicate": m.get("predicate", ""),
                "value": m.get("value", ""),
                "type": m.get("type", ""),
                "module": m.get("module", ""),
                "source": m.get("source", ""),
                "confidence": m.get("confidence", 0),
            }
            for m in results.get("metadatas", [])
        ]

    def get_facts_by_subject(self, subject: str) -> list[dict]:
        try:
            results = self._collection.get(
                where={"subject": subject}, include=["metadatas"]
            )
        except Exception:
            return []

        return [
            {
                "subject": m.get("subject", ""),
                "predicate": m.get("predicate", ""),
                "value": m.get("value", ""),
                "type": m.get("type", ""),
                "module": m.get("module", ""),
                "source": m.get("source", ""),
                "confidence": m.get("confidence", 0),
            }
            for m in results.get("metadatas", [])
        ]

    def count(self) -> int:
        return self._collection.count()

    def delete_by_source(self, source: str) -> int:
        """删除指定 source 的所有事实，返回删除数量。"""
        try:
            existing = self._collection.get(where={"source": source})
            ids = existing.get("ids", [])
            if ids:
                self._collection.delete(ids=ids)
            return len(ids)
        except Exception as e:
            print(f"[WARN] 删除 source={source} 的事实失败: {e}", file=sys.stderr)
            return 0

    def delete_by_sources(self, sources: list[str]) -> int:
        """批量删除多个 source 的事实，返回总删除数量。"""
        total = 0
        for s in sources:
            total += self.delete_by_source(s)
        return total
