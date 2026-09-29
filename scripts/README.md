# 启动与验证脚本

`scripts/` 只保留三类职责：管理本地进程、初始化运行环境、验证真实链路。公共入口保持稳定，根 README 中的命令可以直接执行。

## 推荐入口

```bash
START_FRONTEND=true bash scripts/start_all.sh start
```

`start_all.sh` 统一管理 FastAPI、Celery Worker、Drogon Gateway 和可选的 Vite 前端。它负责：

- 从仓库根目录读取 `.env`。
- 在 Gateway 二进制缺失时执行 CMake 构建。
- 以独立进程组启动服务，将 PID 写入 `.run/`。
- 将标准输出和错误写入 `logs/<service>.log`。
- 启动后等待 HTTP 端口响应。
- 停止时终止完整进程组，避免遗留子进程。
- 在 `status` 中同时展示 PID 和 HTTP 状态。

命令行显式传入的环境变量优先于 `.env`，因此 `START_FRONTEND=true ...`、`FRONTEND_PORT=5199 ...` 这类临时覆盖不会被 `.env` 中的默认值改回去。

命令格式：

```text
bash scripts/start_all.sh [start|stop|restart|status|logs] [all|api|worker|gateway|frontend]
bash scripts/start_all.sh e2e [test-file]
```

常用示例：

```bash
# API + Worker + Gateway
bash scripts/start_all.sh start

# 加上前端
START_FRONTEND=true bash scripts/start_all.sh start

# 只重启一个服务
bash scripts/start_all.sh restart api

# 查看全部服务或单个服务
bash scripts/start_all.sh status
bash scripts/start_all.sh status gateway

# 查看最近日志；FOLLOW_LOGS=true 时持续跟随
bash scripts/start_all.sh logs worker
FOLLOW_LOGS=true bash scripts/start_all.sh logs

# 停止单服务或整栈
bash scripts/start_all.sh stop frontend
bash scripts/start_all.sh stop
```

## 启动变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `START_FRONTEND` | `false` | `all` 目标是否包含 Vite |
| `START_GATEWAY` | `true` | `all` 目标是否包含 Gateway |
| `START_INIT_DB` | `false` | 启动前是否运行 `init_db.sh` |
| `START_WAIT_SECONDS` | `20` | HTTP 启动等待秒数 |
| `STOP_WAIT_SECONDS` | `8` | 正常退出等待秒数 |
| `FRONTEND_PORT` | `5173` | Vite 固定监听端口；占用时直接失败，不自动换端口 |
| `LOG_LINES` | `80` | `logs` 输出行数 |
| `FOLLOW_LOGS` | `false` | 是否持续跟随日志 |

服务地址继续由 `APP_HOST` / `APP_PORT`、`GATEWAY_LISTEN_HOST` / `GATEWAY_LISTEN_PORT` 控制。`PYTHON_BASE_URL`、`GATEWAY_BASE_URL` 和 `FRONTEND_BASE_URL` 可以覆盖脚本用于健康检查的访问地址。

## 单进程入口

这些脚本适合前台调试，`start_all.sh` 内部也复用它们：

- `start_api.sh`：激活 `rag-api` 环境并启动 Uvicorn。
- `start_worker.sh`：激活 `rag-api` 环境并 `exec` Celery Worker。
- `cpp_gateway/scripts/start_gateway.sh`：检查二进制后前台启动 Gateway。
- `start_vllm.sh`：API 模式检查远端 `/models`；只有 `LLM_RUNTIME=local_vllm` 时启动本地 vLLM。
- `init_db.sh`：创建数据库，执行 `db/init.sql` 和后续 migration。

API 与 Worker 的 CUDA 可见性遵循同一套规则：

```bash
PYTHON_DISABLE_CUDA=true
PYTHON_CUDA_VISIBLE_DEVICES=4,5

# 进程级配置覆盖公共配置
API_DISABLE_CUDA=true
API_CUDA_VISIBLE_DEVICES=4
WORKER_DISABLE_CUDA=false
WORKER_CUDA_VISIBLE_DEVICES=5
```

## 验证脚本

- `ci_smoke.sh`：Python compileall、pytest、全部 shell 语法和前端生产构建。
- `e2e_all.sh [file]`：从 Gateway 创建用户、上传、ingest、创建会话、Chat、citations 到 monitor 的完整链路。
- `e2e_ingest.sh [file]`：只验证 Gateway 上传与 ingest。
- `metrics_benchmark.py`：通过 Gateway 压测 ingest、异步 Chat、流式 Chat 和监控指标。
- `vllm_benchmark.py`：绕过 RAG，直接压测 OpenAI-compatible vLLM 的非流式延迟和流式 TTFT。

```bash
bash scripts/ci_smoke.sh
bash scripts/e2e_all.sh ./README.md

# 原始模型服务，不包含检索和 Gateway 成本
python scripts/vllm_benchmark.py \
  --model Qwen3-14B \
  --mode both \
  --requests 20 \
  --concurrency 5
```

E2E 脚本会创建真实数据库记录，不属于只读 smoke test。不传文件参数时默认使用仓库根目录的 `README.md`，也可以显式传入其他待上传文件。
vLLM benchmark 从 `VLLM_API_KEY` 或 `LLM_API_KEY` 读取认证信息，不会把密钥写入报告。`--requests` 表示每种模式的请求数，因此 `--mode both --requests 20` 会执行 20 个非流式请求和 20 个流式请求。

## 共享 helper

`env.sh` 由其他脚本 `source`，不要单独执行。它集中提供：

- `.env` 加载。
- 布尔变量解析。
- API / Worker CUDA 可见性配置。
- Conda / venv 激活。
- vcpkg toolchain 发现。
- Gateway API Key header 构造。
- JSON 字段读取和异步任务轮询。
