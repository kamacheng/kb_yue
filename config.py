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
    """加载配置：优先 config.json，其次环境变量，否则报错提示。"""
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
            "未配置知识库路径。请在 .env 文件中设置 KB_ROOT，"
            "或在 config.json 中设置 kb_root。详见 README。"
        )

    # 若 kb_root 来自 config.json 且为相对路径，相对于 config.json 目录解析
    # （环境变量中的相对路径语义不明确，不做处理）
    if kb_root_from_config:
        kb_root = _resolve_kb_root(kb_root, config_path)

    data_dir = config.get("data_dir") or get_env("KB_DATA_DIR")
    if not data_dir or data_dir.startswith("可选"):
        data_dir = str(Path(kb_root) / ".kb_index")

    return {"kb_root": kb_root, "data_dir": data_dir}


_config = _load_config()

# ---------- 路径常量 ----------

KB_DIR = Path(_config["kb_root"])
DATA_DIR = Path(_config["data_dir"])
CHROMA_DIR = DATA_DIR / "chroma_data"
INDEX_META_PATH = DATA_DIR / "index_meta.json"
SOURCE_META_PATH = DATA_DIR / "source_meta.json"
FACTS_CACHE_DIR = DATA_DIR / "cache" / "facts"
AI_FORMAT_CACHE_DIR = DATA_DIR / "cache" / "ai_format"
ORIGINAL_DIR = KB_DIR / "original_file"
MD_DIR = KB_DIR / "md_file"
CONVERTED_XLSX_DIR = KB_DIR / "_converted_xlsx"  # 兼容旧路径，新流程使用 MD_DIR
CANON_PATH = DATA_DIR / "canon.json"
CANON_CHANGELOG_PATH = DATA_DIR / "canon_changelog.json"
CANON_LOCK_PATH = DATA_DIR / "canon.lock"

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
LLM_API_SEMAPHORE = threading.Semaphore(12)

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

    _siliconflow_client = OpenAI(base_url=EMBEDDING_API_BASE, api_key=api_key)
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

    _deepseek_client = OpenAI(base_url="https://api.deepseek.com", api_key=api_key)
    return _deepseek_client
