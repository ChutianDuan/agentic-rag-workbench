# 让本地 RAG 栈稳定运行：环境拆分与排障路径

[返回文档地图](README.md)

本地 RAG 系统的问题通常不是“某个进程没启动”这么简单。同一个页面背后同时有 Gateway、FastAPI、Worker、MySQL、Redis、向量索引、reranker 和 LLM；一条请求停在 `PROCESSING`，可能是检索、模型下载、代理、SSE 或残留进程中的任何一处。

这篇文章不按配置文件顺序罗列变量，而是先建立进程模型，再给出一条由外向内的排障路径。

## 先分清哪些进程参与当前请求

| 组件 | 默认地址 | 推荐管理方式 | 当前职责 |
| --- | --- | --- | --- |
| React Workbench | `127.0.0.1:5173` | `start_all.sh ... frontend` | 文档、Execution Flow、回答证据与 Trace |
| Drogon Gateway | `127.0.0.1:8080` | `start_all.sh ... gateway` | `/v1/*`、上传、CORS、安全、健康聚合与 SSE 代理 |
| FastAPI | `127.0.0.1:8000` | `start_all.sh ... api` | 检索、Chat、Agent、Prompt、监控与内部 API |
| Celery Worker | Redis broker | `start_all.sh ... worker` | ingest、embedding 和非流式 Chat |
| MySQL | `.env` | 外部服务 | 文档、chunk、消息、citations、任务和 Trace |
| Redis | `.env` | 外部服务 | Celery broker / backend 与可选 Gateway 限流 |
| Remote LLM / vLLM | provider URL / `:9000` | `start_vllm.sh` | OpenAI-compatible generation |

先判断请求类型：

- `POST /v1/sessions/{id}/messages` 是非流式 Chat，会创建 Celery `chat_generate` task。
- `POST /v1/chat/stream` 是普通 RAG SSE，不经过 Celery。
- `POST /v1/agent/chat/stream` 是 Agent SSE，也不经过 Celery。
- 文档解析和 embedding 始终由 Worker 执行。

因此，“Worker 没收到 chat task”对流式请求是正常行为，不应被当成故障。

## 为什么 Python 环境要拆开

默认业务环境是 `rag-api`。只有显式使用本地 vLLM 时才需要第二个环境：

| 环境 | 进程 | 主要依赖 |
| --- | --- | --- |
| `rag-api` | FastAPI、Worker、pytest | Web、文档解析、sentence-transformers、Celery、LanceDB |
| `vllm-qwen3` | `vllm serve` | PyTorch、CUDA、vLLM 与模型运行依赖 |

vLLM 对 CUDA、PyTorch 和 attention 实现的版本更敏感；把它和文档解析、RAG API 全部装进一个环境，会扩大依赖冲突面。

```bash
conda create -n rag-api python=3.10
conda activate rag-api
pip install -r python_rag/requirements.txt
pip install -r python_rag/requirements-dev.txt

# 只有 local_vllm 模式需要
conda create -n vllm-qwen3 python=3.10
conda activate vllm-qwen3
pip install -r python_rag/requirements-vllm.txt
```

也可以使用仓库内 `.venv`：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r python_rag/requirements.txt
pip install -r python_rag/requirements-dev.txt
```

## 配置分三层理解

### 1. 基础设施

MySQL、Redis、存储路径和监听端口来自根目录 `.env`。命令行显式传入的环境变量优先于 `.env`，适合临时切换端口或启动范围。

```bash
cp .env.example .env
bash scripts/init_db.sh
```

### 2. 检索模型

当前默认使用 LanceDB、Qwen embedding 和 Qwen CrossEncoder：

```bash
RETRIEVAL_RECALL_PROVIDER=lancedb
VECTOR_STORE_PROVIDER=lancedb
LANCEDB_PATH=./data/lancedb
LANCEDB_TABLE=chunk_vectors
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
RERANK_ENABLE=true
RERANK_PROVIDER=cross_encoder
RERANK_MODEL=Qwen/Qwen3-Reranker-0.6B
RETRIEVAL_DENSE_TOP_K=50
RETRIEVAL_RERANK_TOP_K=5
```

`RERANK_FALLBACK_TO_FAISS` 是历史配置名。在当前 LanceDB 路径中，它实际表示 reranker 不可用时保留向量召回顺序，并不会切回一个完整的 FAISS 主路径。

网络受限且模型已经缓存时，可以快速失败并回退：

```bash
RERANK_LOCAL_FILES_ONLY=true
RERANK_DOWNLOAD_IF_MISSING=false
RERANK_FALLBACK_TO_FAISS=true
```

切换 embedding 模型后必须重建已有文档的向量索引。旧索引和新 query 向量不属于同一个向量空间，系统会用 `document_indexes.embedding_model` 检查这类冲突；已有 chunks 可通过 `POST /internal/lancedb/documents/{doc_id}/rebuild` 重新计算 embedding。

### 3. 生成模型

默认使用远端 OpenAI-compatible API：

```bash
LLM_RUNTIME=api
LLM_ENABLE=true
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://provider.example/v1
LLM_API_KEY=your-api-key
LLM_MODEL=your-model
```

`bash scripts/start_vllm.sh` 在 API 模式下只检查 `${LLM_BASE_URL}/models`，不会启动本地模型。

本地 vLLM 需要显式切换：

```bash
LLM_RUNTIME=local_vllm
LLM_BASE_URL=http://127.0.0.1:9000/v1
LLM_MODEL=Qwen3-14B
VLLM_MODEL_PATH=/path/to/Qwen3-14B
VLLM_SERVED_MODEL_NAME=Qwen3-14B
```

`LLM_MODEL` 必须与 `VLLM_SERVED_MODEL_NAME` 一致。

## GPU 分配：先隔离，再谈利用率

在没有其他项目约束时，本机 GPU 使用 4、5 号卡。不要在 Python 业务代码中写死物理编号；启动脚本通过 `CUDA_VISIBLE_DEVICES` 暴露设备，进程内部仍从 `cuda:0` 开始编号。

双卡 vLLM，RAG API 留在 CPU：

```bash
VLLM_CUDA_VISIBLE_DEVICES=4,5
VLLM_TENSOR_PARALLEL_SIZE=2
PYTHON_DISABLE_CUDA=true
```

单卡 vLLM 与单卡检索模型分离：

```bash
VLLM_CUDA_VISIBLE_DEVICES=4
VLLM_TENSOR_PARALLEL_SIZE=1
API_CUDA_VISIBLE_DEVICES=5
WORKER_CUDA_VISIBLE_DEVICES=5
```

如果显存不足，优先让 embedding / rerank 回到 CPU，而不是让多个进程争抢同一张卡导致随机 OOM。

## 推荐启动顺序

先确认外部依赖和模型入口，再启动应用栈：

```bash
# MySQL / Redis 已可用后
bash scripts/init_db.sh

# 远端模式做连通性检查；本地模式会前台启动 vLLM
bash scripts/start_vllm.sh

# API + Worker + Gateway + Frontend
START_FRONTEND=true bash scripts/start_all.sh start
```

统一管理命令：

```bash
bash scripts/start_all.sh status
bash scripts/start_all.sh logs api
bash scripts/start_all.sh restart worker
bash scripts/start_all.sh stop
```

前端端口由 `FRONTEND_PORT` 真正传给 Vite，并启用 `--strictPort`。端口被占用时启动会失败，而不是悄悄切到 5174：

```bash
FRONTEND_PORT=5199 bash scripts/start_all.sh start frontend
```

需要前台调试时仍可直接运行单进程脚本：

```bash
bash scripts/start_api.sh
bash scripts/start_worker.sh
bash cpp_gateway/scripts/start_gateway.sh
```

## 健康检查要分层阅读

```bash
curl http://127.0.0.1:8000/internal/health
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/v1/monitor/overview
```

- FastAPI health 证明 Python API 及其依赖视角。
- Gateway health 聚合 Gateway、FastAPI、MySQL、Redis。
- Monitor 提供资源、队列和 RAG 指标。
- 这些接口都不能证明 LLM 一定可用。
- 当前没有独立的 LanceDB 实时健康接口。

模型入口需要单独验证：

```bash
curl -H "Authorization: Bearer ${LLM_API_KEY}" \
  "${LLM_BASE_URL%/}/models"
```

`401` 通常说明网络可达但凭据错误；超时或连接失败才指向地址、代理或进程问题。

## 流式请求卡住时，从外向内定位

先直接调用 Gateway，绕开前端状态管理：

```bash
curl -N -X POST http://127.0.0.1:8080/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"session_id":1,"content":"ping","top_k":3}' \
  --max-time 30
```

然后按顺序检查：

| 层级 | 证据 | 典型判断 |
| --- | --- | --- |
| Browser | Network / Agent Trace | 请求是否发出，是否收到 event ID |
| Gateway | `logs/gateway.log` | 是否接受请求、是否连接 Python SSE |
| FastAPI | `logs/api.log` | 是否进入 `/internal/chat/stream` 或 Agent stream |
| Retrieval | API 日志与 `retrieval_ms` | 是否卡在 embedding、LanceDB、回表或 rerank |
| LLM | provider / vLLM 日志 | 是否出现 Chat Completion 请求 |
| Persistence | MySQL messages / citations | 最终回答是否在 `done` 前保存 |

常见状态：

- user message 仍是 `PENDING`：流没有进入 Python，或在开始前失败。
- message 为 `PROCESSING`，LLM 没请求：优先检查检索和 reranker。
- LLM 有请求但浏览器没有 delta：检查 Gateway 代理和前端 SSE parser。
- Worker 没有 `chat_generate`：流式模式下正常。

## 代理为什么会影响本地调用

设置 `HTTP_PROXY` / `HTTPS_PROXY` 后，HuggingFace 下载、远端模型和本地服务可能走不同路径。当前 Python LLM 客户端会绕过 localhost / 私网地址，Gateway 的 Python SSE client 也禁用了本地代理。

如果看到 upstream `502`：

1. 直接访问 FastAPI 对应内部接口。
2. 检查 `PYTHON_INTERNAL_BASE_URL`。
3. 确认 Gateway 使用了最新构建。
4. 再检查代理变量和 NO_PROXY，而不是先修改 RAG 业务代码。

## 常见问题

### 前端一直显示生成中，但模型服务没有请求

先确认是流式还是非流式路径。流式请求不看 Worker；检查 `logs/api.log` 中是否进入 stream，再看 embedding、LanceDB 和 reranker 初始化。

### vLLM 已启动，但顶部 health 不是 ok

Gateway health 不检查 vLLM。分别验证 `/health` 和 `${LLM_BASE_URL}/models`。

### reranker 出现 SSL、proxy 或 cache 错误

模型没有完整缓存时，`RERANK_LOCAL_FILES_ONLY=true` 会立即失败。演示环境可以启用回退；要验证真实 rerank，则应先准备完整模型并在 Trace 中确认 `rerank_used=true` 和实际模型名。

### SSE 断线后出现重复消息

普通 Chat 续传必须复用 Gateway 返回的 `X-User-Message-ID`，并携带 Last-Event-ID；Agent 续传必须复用同一 `trace_id`。如果自定义客户端重发原始 `content`，会有重复创建消息的风险。

### 修改 `.env` 后仍像旧配置

运行中的进程不会自动重新读取 `.env`。使用统一入口重启对应服务，并检查状态与日志：

```bash
bash scripts/start_all.sh restart api
bash scripts/start_all.sh restart worker
bash scripts/start_all.sh status
bash scripts/start_all.sh logs
```

不要直接删除 PID 文件或手工清理未知进程；先让管理脚本按进程组正常退出。
