"""模块别名归一化 — 单一配置源 (module_aliases.json)。

事实层（chunks / facts store）仍存原始 module，保留可追溯到具体目录的能力。
法典写入与对外聚合（list_modules / get_module_relations / search by module）
统一调用 normalize_module()，让 canon 与查询结果按规范名合并。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_ALIAS_PATH = Path(__file__).parent / "module_aliases.json"


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, str], dict[str, list[str]]]:
    """加载别名表，返回 (alias->canonical, canonical->aliases)。

    Returns:
        alias_to_canonical: 别名 → 规范名 的反向索引
        canonical_to_aliases: 规范名 → 别名列表 的正向索引
    """
    alias_to_canonical: dict[str, str] = {}
    canonical_to_aliases: dict[str, list[str]] = {}

    if not _ALIAS_PATH.exists():
        return alias_to_canonical, canonical_to_aliases

    try:
        data = json.loads(_ALIAS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return alias_to_canonical, canonical_to_aliases

    aliases_map = data.get("aliases", {})
    for canonical, alias_list in aliases_map.items():
        canonical_to_aliases[canonical] = list(alias_list)
        for alias in alias_list:
            alias_to_canonical[alias] = canonical

    return alias_to_canonical, canonical_to_aliases


def normalize_module(name: str) -> str:
    """把任意 module 名归一化为规范名。不在别名表里的名字按原样返回。"""
    if not name:
        return name
    alias_to_canonical, _ = _load()
    return alias_to_canonical.get(name, name)


def expand_aliases(name: str) -> list[str]:
    """把 module 名展开为所有等价名列表（含自身）。用于按 module 过滤时构造 $in 查询。

    传入规范名 → 返回该规范名的全部别名
    传入别名   → 先归一化再展开
    传入未登记 → 返回 [name] 自身
    """
    if not name:
        return [name]
    alias_to_canonical, canonical_to_aliases = _load()
    canonical = alias_to_canonical.get(name, name)
    return canonical_to_aliases.get(canonical, [canonical])


def reload_aliases() -> None:
    """清空缓存，重新加载。仅用于测试或手动改了 json 文件后。"""
    _load.cache_clear()
