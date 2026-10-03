import os

from dotenv import load_dotenv


load_dotenv()
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _resolve_repo_path(path_value: str) -> str:
    if os.path.isabs(path_value):
        return path_value
    return os.path.abspath(os.path.join(REPO_ROOT, path_value))


MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "ai_app")
MYSQL_USER = os.getenv("MYSQL_USER", "ai_user")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "ai_password")

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))

STORAGE_ROOT = _resolve_repo_path(os.getenv("STORAGE_ROOT", "./data"))
UPLOAD_DIR = _resolve_repo_path(os.getenv("UPLOAD_DIR", "./data/uploads"))
MAX_DOCUMENT_SIZE_BYTES = int(os.getenv("MAX_DOCUMENT_SIZE_BYTES", str(100 * 1024 * 1024)))
INGEST_CHUNK_SIZE = int(os.getenv("INGEST_CHUNK_SIZE", "800"))
INGEST_CHUNK_OVERLAP = int(os.getenv("INGEST_CHUNK_OVERLAP", "100"))

REDIS_URL = "redis://{host}:{port}/{db}".format(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
)

if REDIS_PASSWORD:
    REDIS_URL = "redis://:{password}@{host}:{port}/{db}".format(
        password=REDIS_PASSWORD,
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
    )

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_POOL = os.getenv("CELERY_POOL", "threads")
CELERY_CONCURRENCY = int(os.getenv("CELERY_CONCURRENCY", "4"))

CHAT_MAX_CHUNK_CHARS = int(os.getenv("CHAT_MAX_CHUNK_CHARS", "1000"))
_DEFAULT_CHAT_TOP_K = os.getenv("CHAT_TOP_K", "5")
_DEFAULT_CHAT_CANDIDATE_TOP_K = os.getenv("CHAT_CANDIDATE_TOP_K", "50")
RETRIEVAL_DENSE_TOP_K = int(
    os.getenv("RETRIEVAL_DENSE_TOP_K", _DEFAULT_CHAT_CANDIDATE_TOP_K)
)
RETRIEVAL_RERANK_TOP_K = int(
    os.getenv("RETRIEVAL_RERANK_TOP_K", _DEFAULT_CHAT_TOP_K)
)
CHAT_TOP_K = int(os.getenv("CHAT_TOP_K", str(RETRIEVAL_RERANK_TOP_K)))
CHAT_CANDIDATE_TOP_K = int(
    os.getenv("CHAT_CANDIDATE_TOP_K", str(RETRIEVAL_DENSE_TOP_K))
)
CHAT_MIN_RETRIEVAL_SCORE = float(os.getenv("CHAT_MIN_RETRIEVAL_SCORE", "0.0"))
RETRIEVAL_CONTEXT_WINDOW = int(os.getenv("RETRIEVAL_CONTEXT_WINDOW", "1"))
RETRIEVAL_CONTEXT_MAX_CHARS = int(os.getenv("RETRIEVAL_CONTEXT_MAX_CHARS", "3000"))
RETRIEVAL_RECALL_PROVIDER = os.getenv(
    "RETRIEVAL_RECALL_PROVIDER",
    "lancedb",
).strip().lower()
VECTOR_STORE_PROVIDER = os.getenv("VECTOR_STORE_PROVIDER", "lancedb").strip().lower()
LANCEDB_PATH = _resolve_repo_path(
    os.getenv("LANCEDB_PATH", os.getenv("LANCEDB_URI", "./data/lancedb"))
)
LANCEDB_TABLE = os.getenv(
    "LANCEDB_TABLE",
    os.getenv("LANCEDB_TABLE_NAME", "chunk_vectors"),
).strip()
LANCEDB_URI = LANCEDB_PATH
LANCEDB_TABLE_NAME = LANCEDB_TABLE
BM25_K1 = float(os.getenv("BM25_K1", "1.5"))
BM25_B = float(os.getenv("BM25_B", "0.75"))
RRF_K = int(os.getenv("RRF_K", "60"))
STREAM_DELTA_CHARS = int(os.getenv("STREAM_DELTA_CHARS", "20"))
STREAM_MOCK_DELAY_MS = int(os.getenv("STREAM_MOCK_DELAY_MS", "30"))

EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "sentence_transformers")
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "Qwen/Qwen3-Embedding-0.6B",
).strip()
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "").rstrip("/")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "").strip()
EMBEDDING_TIMEOUT_SECONDS = int(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "60"))
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
EMBEDDING_DEVICE = os.getenv("EMBEDDING_DEVICE", "auto").strip()
EMBEDDING_NORMALIZE = os.getenv(
    "EMBEDDING_NORMALIZE",
    "true",
).lower() in ("1", "true", "yes", "on")
EMBEDDING_QUERY_PREFIX = os.getenv(
    "EMBEDDING_QUERY_PREFIX",
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\\nQuery: ",
)
EMBEDDING_DOCUMENT_PREFIX = os.getenv("EMBEDDING_DOCUMENT_PREFIX", "")

RERANK_ENABLE = os.getenv("RERANK_ENABLE", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
RERANK_PROVIDER = os.getenv("RERANK_PROVIDER", "cross_encoder").strip().lower()
RERANK_MODEL = os.getenv(
    "RERANK_MODEL",
    "Qwen/Qwen3-Reranker-0.6B",
).strip()
RERANK_DEVICE = os.getenv("RERANK_DEVICE", EMBEDDING_DEVICE).strip()
RERANK_BATCH_SIZE = int(os.getenv("RERANK_BATCH_SIZE", "16"))
RERANK_LOCAL_FILES_ONLY = os.getenv(
    "RERANK_LOCAL_FILES_ONLY",
    "false",
).lower() in ("1", "true", "yes", "on")
RERANK_CACHE_DIR = os.getenv("RERANK_CACHE_DIR", "").strip() or None
RERANK_DOWNLOAD_IF_MISSING = os.getenv(
    "RERANK_DOWNLOAD_IF_MISSING",
    "true",
).lower() in ("1", "true", "yes", "on")
RERANK_FALLBACK_TO_FAISS = os.getenv(
    "RERANK_FALLBACK_TO_FAISS",
    "true",
).lower() in ("1", "true", "yes", "on")

LLM_ENABLE = os.getenv("LLM_ENABLE", "true").lower() in ("1", "true", "yes", "on")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai_compatible")

LLM_BASE_URL = os.getenv(
    "LLM_BASE_URL",
    "https://open.bigmodel.cn/api/paas/v4",
).rstrip("/")

LLM_API_KEY = (
    os.getenv("MIMO_API_KEY", "").strip()
    or os.getenv("LLM_API_KEY", "").strip()
)

LLM_MODEL = os.getenv("LLM_MODEL", "glm-4.7-flash")

LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "512"))
LLM_TOKEN_LIMIT_FIELD = (
    os.getenv("LLM_TOKEN_LIMIT_FIELD", "max_tokens").strip() or "max_tokens"
)
LLM_MAX_GENERATION_ROUNDS = int(os.getenv("LLM_MAX_GENERATION_ROUNDS", "3"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))

# 分类模型必须显式指定；连接信息可复用主模型，生成参数保持独立。
ROUTER_LLM_MODEL = os.getenv("ROUTER_LLM_MODEL", "").strip()
ROUTER_LLM_BASE_URL = (
    os.getenv("ROUTER_LLM_BASE_URL", "").strip() or LLM_BASE_URL
).rstrip("/")
ROUTER_LLM_API_KEY = os.getenv("ROUTER_LLM_API_KEY", "").strip() or LLM_API_KEY
ROUTER_LLM_TIMEOUT_SECONDS = float(os.getenv("ROUTER_LLM_TIMEOUT_SECONDS", "5"))
ROUTER_LLM_MAX_TOKENS = int(os.getenv("ROUTER_LLM_MAX_TOKENS", "96"))
ROUTER_LLM_TOKEN_LIMIT_FIELD = (
    os.getenv("ROUTER_LLM_TOKEN_LIMIT_FIELD", "").strip() or LLM_TOKEN_LIMIT_FIELD
)


def _optional_float_env(name: str):
    value = os.getenv(name, "").strip()
    if not value:
        return None
    return float(value)


LLM_TOP_P = _optional_float_env("LLM_TOP_P")
LLM_FREQUENCY_PENALTY = _optional_float_env("LLM_FREQUENCY_PENALTY")
LLM_PRESENCE_PENALTY = _optional_float_env("LLM_PRESENCE_PENALTY")
CHAT_ENABLE_MOCK_FALLBACK = os.getenv(
    "CHAT_ENABLE_MOCK_FALLBACK",
    "true",
).lower() in ("1", "true", "yes", "on")

LLM_PROMPT_COST_PER_1K_TOKENS = float(
    os.getenv("LLM_PROMPT_COST_PER_1K_TOKENS", "0"),
)
LLM_COMPLETION_COST_PER_1K_TOKENS = float(
    os.getenv("LLM_COMPLETION_COST_PER_1K_TOKENS", "0"),
)
EMBEDDING_COST_PER_1K_TOKENS = float(
    os.getenv("EMBEDDING_COST_PER_1K_TOKENS", "0"),
)
MONITOR_METRICS_WINDOW_SECONDS = int(
    os.getenv("MONITOR_METRICS_WINDOW_SECONDS", "300"),
)
MONITOR_METRICS_MAX_ROWS = int(
    os.getenv("MONITOR_METRICS_MAX_ROWS", "5000"),
)
MONITOR_GPU_IDS = os.getenv("MONITOR_GPU_IDS", "").strip()
