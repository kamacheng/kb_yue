"""统一配置管理 — 配置加载、.env 解析、路径常量。"""

import json
import os
from pathlib import Path

# ---------- .env 加载 ----------

_env_loaded = False
_env_values: dict[str, str] = {}

def _load_env() -> dict[str, str]:
    """从项目目录 .env 文件加载环境变量。只加载一次。"""
    global _env_loaded, _env_values
    if _env_loaded:
        return _env_values

    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                _env_values[key.strip()] = val.strip().strip("\"'")

    _env_loaded = True
    return _env_values


def get_env(key: str, default: str = "") -> str:
    """获取环境变量，优先 os.environ，其次 .env 文件。"""
    val = os.environ.get(key, "")
    if val:
        return val
    return _load_env().get(key, default)


# ---------- 配置加载 ----------

def _resolve_kb_root(kb_root: str, config_path: Path) -> str:
    """如果 kb_root 是相对路径，则相对于 config.json 所在目录解析。"""
    p = Path(kb_root)
    if not p.is_absolute():
        return str((config_path.parent / p).resolve())
    return kb_root


def _load_config() -> dict:
    """加载配置：优先 config.json，其次环境变量，否则报错提示。

    只需配置 KB_ROOT,其余子路径(.kb_index / md_file / original_file)从根目录派生。
    """
    config_path = Path(__file__).parent / "config.json"
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    kb_root_from_config = config.get("kb_root")
    kb_root = kb_root_from_config or get_env("KB_ROOT")
    if not kb_root:
        raise RuntimeError(
            "未配置知识库路径。请在 .env 文件中设置 KB_ROOT,"
            "或在 config.json 中设置 kb_root。详见 README。"
        )

    # 若 kb_root 来自 config.json 且为相对路径,相对于 config.json 目录解析
    # （环境变量中的相对路径语义不明确,不做处理）
    if kb_root_from_config:
        kb_root = _resolve_kb_root(kb_root, config_path)

    kb_root_path = Path(kb_root)
    return {
        "kb_root": kb_root,
        "data_dir": str(kb_root_path / ".kb_index"),
        "md_dir": str(kb_root_path / "md_file"),
        "original_dir": str(kb_root_path / "original_file"),
    }


_config = _load_config()

# ---------- 路径常量 ----------

def _recompute_paths() -> None:
    """根据当前 _config 重新计算所有路径常量（写入本模块 globals）。"""
    g = globals()
    kb_dir = Path(_config["kb_root"])
    data_dir = Path(_config["data_dir"])
    g["KB_DIR"] = kb_dir
    g["DATA_DIR"] = data_dir
    g["CHROMA_DIR"] = data_dir / "chroma_data"
    g["INDEX_META_PATH"] = data_dir / "index_meta.json"
    g["SOURCE_META_PATH"] = data_dir / "source_meta.json"
    g["FACTS_CACHE_DIR"] = data_dir / "cache" / "facts"
    g["AI_FORMAT_CACHE_DIR"] = data_dir / "cache" / "ai_format"
    g["ORIGINAL_DIR"] = Path(_config["original_dir"])
    g["MD_DIR"] = Path(_config["md_dir"])
    g["CONVERTED_XLSX_DIR"] = kb_dir / "_converted_xlsx"
    g["CANON_PATH"] = data_dir / "canon.json"
    g["CANON_CHANGELOG_PATH"] = data_dir / "canon_changelog.json"
    g["CANON_LOCK_PATH"] = data_dir / "canon.lock"


_recompute_paths()


# 已知会通过 `from config import X` 缓存路径常量的下游模块及其引用的名字。
# reload_config() 会把最新值 setattr 回这些模块，确保 .env 改动在运行时生效。
_PATH_CONSUMERS: dict[str, tuple[str, ...]] = {
    "index_manager": (
        "KB_DIR", "DATA_DIR", "CHROMA_DIR", "INDEX_META_PATH", "SOURCE_META_PATH",
        "ORIGINAL_DIR", "MD_DIR",
    ),
    "indexer": ("KB_DIR", "DATA_DIR", "MD_DIR", "ORIGINAL_DIR"),
    "canon_manager": ("CANON_PATH", "CANON_CHANGELOG_PATH", "CANON_LOCK_PATH", "DATA_DIR"),
    "ai_formatter": ("AI_FORMAT_CACHE_DIR",),
    "fact_extractor": ("FACTS_CACHE_DIR",),
}


def reload_config() -> dict:
    """重新读取 .env 与 config.json,刷新所有路径常量并推送给已加载的下游模块。

    供 kb_index 等写入工具在执行前调用,确保用户修改 .env 后无需重启 MCP server。

    Returns:
        新的配置字典(kb_root/data_dir/md_dir/original_dir)。
    """
    import sys

    global _env_loaded, _env_values, _config
    _env_loaded = False
    _env_values = {}
    _config = _load_config()
    _recompute_paths()

    g = globals()
    for mod_name, attrs in _PATH_CONSUMERS.items():
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for attr in attrs:
            if attr in g:
                setattr(mod, attr, g[attr])

    # xlsx_converter.CONVERTED_DIR 在 index_manager.py 模块加载时绑定到 MD_DIR,
    # 同步更新它,否则转换器会写到旧路径。
    xc = sys.modules.get("xlsx_converter")
    if xc is not None:
        xc.CONVERTED_DIR = g["MD_DIR"]

    # 重置 index_manager 中持有路径的单例(facts_store 用 CHROMA_DIR 初始化)。
    im = sys.modules.get("index_manager")
    if im is not None and hasattr(im, "_facts_store"):
        im._facts_store = None

    return dict(_config)

# ---------- API 配置 ----------

EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_API_BASE = "https://api.siliconflow.cn/v1"
EMBEDDING_BATCH_SIZE = 16
EMBEDDING_MAX_TOKENS = 7000  # 安全阈值，SiliconFlow API 限制 8192

COLLECTION_NAME = "game_design_kb"

VECTOR_WEIGHT = 0.7
BM25_WEIGHT = 0.3

RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
USE_RERANKER = True


# ---------- LLM 并发控制 ----------

import threading
# 信号量从 12 降到 8: 减少 API 限流压力,单个请求卡住时阻塞面更小
LLM_API_SEMAPHORE = threading.Semaphore(8)

# OpenAI client 超时/重试配置
LLM_REQUEST_TIMEOUT = 30.0  # 单次请求超时(秒)
LLM_MAX_RETRIES = 2         # SDK 内置重试次数(指数退避)

# ---------- API 客户端工厂 ----------

_siliconflow_client = None
_deepseek_client = None


def get_siliconflow_client():
    """获取 SiliconFlow API 客户端（单例）。"""
    global _siliconflow_client
    if _siliconflow_client is not None:
        return _siliconflow_client

    from openai import OpenAI

    api_key = get_env("SILICONFLOW_API_KEY")
    if not api_key:
        raise ValueError("未找到 SILICONFLOW_API_KEY，请在 .env 文件或环境变量中设置")

    _siliconflow_client = OpenAI(
        base_url=EMBEDDING_API_BASE,
        api_key=api_key,
        timeout=LLM_REQUEST_TIMEOUT,
        max_retries=LLM_MAX_RETRIES,
    )
    return _siliconflow_client


def get_deepseek_client():
    """获取 DeepSeek API 客户端（单例）。"""
    global _deepseek_client
    if _deepseek_client is not None:
        return _deepseek_client

    from openai import OpenAI

    api_key = get_env("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("未找到 DEEPSEEK_API_KEY，请在 .env 文件或环境变量中设置")

    _deepseek_client = OpenAI(
        base_url="https://api.deepseek.com",
        api_key=api_key,
        timeout=LLM_REQUEST_TIMEOUT,
        max_retries=LLM_MAX_RETRIES,
    )
    return _deepseek_client
