# 不要只测 QPS：RAG 系统的性能测试与结果留档

[返回文档地图](README.md)

传统 Web 压测常用 QPS 和平均延迟概括结果，但 RAG 请求同时包含检索、回表、rerank、模型首 token、持续生成和 citations 持久化。只报告一个 QPS，既看不出用户等待在哪里，也无法判断系统是否已经用队列积压换取表面吞吐。

这份指南的目标不是跑出一个最好看的数字，而是建立可重复的基线：固定输入和模型，分离 ingest、异步 Chat 与 SSE，记录 P95/P99、队列、错误、成本和检索质量，并保留足够环境信息供版本回归。

当前项目的性能数据主要来自两处：

- 服务端聚合监控接口：`/internal/monitor/overview`、`/v1/monitor/overview`
- 压测脚本：[scripts/metrics_benchmark.py](../scripts/metrics_benchmark.py)

脚本会串联 `ingest`、异步 `chat`、流式 `chat` 三条链路，并输出以下指标：

- 体验：`TTFT`、`E2E latency P50/P95/P99`、`ingest ready time`
- 成本：`prompt/completion tokens`、`cost per request`、`cost per document`
- 吞吐：`QPS`、`concurrent sessions`、`worker queue depth`、`active SSE connections`
- 稳定性/质量：`error rate`、`timeout rate`、`retrieval_ms`、`citation_count`、`no_context ratio`

## 先写下性能假设

一次测试应该先声明要验证什么，例如：

- 单并发下 TTFT 是否满足交互目标。
- 并发增加时瓶颈先出现在 Worker、reranker、LLM 还是 SSE slot。
- embedding 或模型切换是否改变成本与 P95。
- 队列在停止施压后能否回落，而不是持续积压。

没有假设的压测容易退化成“把并发调高，截一张成功率图”。

## 1. 测试前准备

### 1.1 环境检查

开始测试前，至少确认以下条件：

- MySQL、Redis、FastAPI、Celery Worker、C++ Gateway 已启动
- 如使用真实模型，embedding provider / 本地模型和 LLM / vLLM 已可用
- 已执行数据库初始化和迁移：

```bash
bash scripts/init_db.sh
```

- 已激活项目 Python 虚拟环境并安装依赖：

```bash
source .venv/bin/activate
pip install -r python_rag/requirements.txt
```

### 1.2 成本参数

如果要看真实成本，必须在部署环境配置单价，否则成本相关指标会是 `0`：

```bash
LLM_PROMPT_COST_PER_1K_TOKENS=
LLM_COMPLETION_COST_PER_1K_TOKENS=
EMBEDDING_COST_PER_1K_TOKENS=
```

### 1.3 指标表确认

本轮性能数据会写入 `request_metrics`。如果这个表不存在，监控聚合会退化，只剩系统资源和任务计数。

建表定义见：

- [db/init.sql](../db/init.sql)
- [db/003_schema_upgrade.sql](../db/003_schema_upgrade.sql)

### 1.4 测试输入固定

为了让多轮结果可比较，建议固定以下变量：

- 测试文档
- 提问集合
- `top_k`
- 模型版本
- `CELERY_CONCURRENCY`
- 部署机器规格
- 是否启用 mock fallback

测真实模型性能时应设置 `CHAT_ENABLE_MOCK_FALLBACK=false`。否则模型故障可能被 mock 回答掩盖，延迟和成功率都会失真。

## 2. 观测入口

### 2.1 服务启动

MySQL / Redis 和模型入口可用后，使用统一入口启动被测栈：

```bash
bash scripts/init_db.sh
bash scripts/start_vllm.sh
START_FRONTEND=true bash scripts/start_all.sh start
bash scripts/start_all.sh status
```

需要观察单个进程的前台日志时，再使用 `start_api.sh`、`start_worker.sh` 或 `start_gateway.sh` 单独启动。不要同时保留统一脚本和手工启动的重复进程。

### 2.2 健康检查

```bash
curl http://127.0.0.1:8000/internal/health
curl http://127.0.0.1:8000/internal/monitor/overview
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/v1/monitor/overview
```

### 2.3 监控页面

前端 Monitor 页面已经展示性能聚合结果，可用于肉眼观察系统状态和波动趋势。压测时建议同时开着，重点观察：

- `QPS`
- `worker queue depth`
- `active SSE connections`
- `TTFT / E2E / retrieval` 分位变化
- `error rate / timeout rate`

## 3. 标准测试流程

### 3.1 冒烟测试

目的：确认链路能跑通，不看性能结论。

```bash
# 只验证上传与 ingest
bash scripts/e2e_ingest.sh ./README.md

# 验证 Gateway 上传、ingest、Chat、citations 与 monitor
bash scripts/e2e_all.sh ./README.md
```

检查点：

- 文档能上传并完成 ingest
- 问答任务能成功结束
- Monitor 接口可访问

### 3.2 预热

目的：减少冷启动对结果的污染。

建议先跑 5 到 10 次低并发请求，预热以下组件：

- embedding 模型加载
- LanceDB 向量表查询
- LLM 首次响应
- 流式 SSE 链路

预热阶段的数据不要作为正式基线。

### 3.3 单并发基线

目的：建立部署后的基础延迟和成本基线。

```bash
source .venv/bin/activate
python3 scripts/metrics_benchmark.py \
  --python-base-url http://127.0.0.1:8000 \
  --gateway-base-url http://127.0.0.1:8080 \
  --file ./README.md \
  --async-requests 6 \
  --stream-requests 6 \
  --concurrency 1
```

重点记录：

- `TTFT P50/P95`
- `E2E P50/P95/P99`
- `retrieval_ms`
- `prompt/completion tokens`
- `cost_per_request`
- `no_context_ratio`

### 3.4 并发爬坡

目的：找到系统拐点和瓶颈位置。

推荐按档位逐步提高：

- `1`
- `2`
- `4`
- `8`
- `16`

示例：

```bash
python3 scripts/metrics_benchmark.py \
  --python-base-url http://127.0.0.1:8000 \
  --gateway-base-url http://127.0.0.1:8080 \
  --doc-id 123 \
  --async-requests 40 \
  --stream-requests 40 \
  --concurrency 8 \
  --top-k 3
```

说明：

- `--doc-id` 传已有文档时，会跳过 upload + ingest，只测 chat 链路
- 并发爬坡每档建议至少跑 `20` 到 `50` 个请求
- 若是正式压测，建议每档跑两轮，取更稳定的一轮

### 3.5 Ingest 专项测试

目的：确认文档处理吞吐和 `cost per document`。

建议准备三类文档：

- 小文档
- 中等文档
- 大文档

重点看：

- `ingest ready time`
- `embedding_tokens`
- `cost per document`
- `timings_ms.embedding_ms`
- `timings_ms.index_ms`

注意：`ingest ready time` 受文档大小、切片数、embedding 吞吐和索引构建速度共同影响。

### 3.6 长稳测试

目的：确认系统是否在目标负载下稳定运行。

建议选一个接近目标峰值的并发档位，持续 `15` 到 `60` 分钟。

重点观察：

- `P95/P99` 是否持续恶化
- `timeout rate` 是否抬升
- `worker queue depth` 是否回不去
- CPU / GPU / 内存是否持续爬升

如果长稳阶段的 `queue depth` 一直上升，说明系统已经进入积压状态，不能只看短时 `QPS`。

## 4. 结果判读

### 4.1 体验指标

- `TTFT`：流式首包体验，越接近真实用户感受
- `E2E latency`：完整答案结束时间，重点看 `P95/P99`
- `ingest ready time`：从上传到文档可检索的总时长

### 4.2 成本指标

- `prompt/completion tokens`：优先使用 provider usage，缺失时用估算值
- `cost per request`：按当前 `.env` 单价配置计算
- `cost per document`：按 ingest 的 embedding token 成本计算

### 4.3 吞吐指标

- `QPS` 不再上涨但 `queue depth` 上升，通常意味着 worker 或模型已饱和
- `concurrent sessions` 反映当前并发会话数
- `active SSE connections` 高位不降，说明流式连接占用明显
- `worker queue depth` 是压测时最先观察的积压指标之一

### 4.4 稳定性与质量

- `error rate`：整体失败比例
- `timeout rate`：超时比例，通常早于完全失败出现
- `retrieval_ms`：检索耗时，若异常升高，优先看 query embedding、LanceDB 召回、MySQL chunk 回表和 rerank 路径
- `citation_count`：回答平均引用数量
- `no_context ratio`：检索结果为空或被裁剪为无上下文的比例

## 5. 推荐留档方式

每次测试至少保留以下四组结果：

1. `concurrency = 1` 的单并发基线
2. `concurrency = 目标并发 / 2`
3. `concurrency = 目标并发`
4. `目标并发` 下的长稳测试

如果后续做版本回归，比较时不要只看平均值，优先对比：

- `TTFT P95`
- `E2E P95/P99`
- `error rate`
- `timeout rate`
- `cost per request`
- `worker queue depth`

## 6. 记录模板

下面这段建议每次测试复制一份，直接作为测试记录使用。

```md
# 性能测试记录

## 基本信息

| 项 | 值 |
| --- | --- |
| 测试日期 | |
| 测试人 | |
| 环境 | |
| Git commit | |
| 部署版本 | |
| Python API 地址 | |
| Gateway 地址 | |
| LLM 模型 | |
| Embedding 模型 | |
| Worker 并发 | |
| top_k | |
| 测试文档 | |
| 测试问题集 | |
| MONITOR_METRICS_WINDOW_SECONDS | |

## 预检查

- [ ] `bash scripts/init_db.sh` 已执行
- [ ] `request_metrics` 已创建
- [ ] `/internal/monitor/overview` 可访问
- [ ] `/v1/monitor/overview` 可访问
- [ ] 成本参数已配置
- [ ] 已完成 E2E 冒烟

## 测试批次

| 批次 | 目标 | 文档模式 | async requests | stream requests | concurrency | 结果文件 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| B1 | 基线 | 新文档 / 旧文档 | | | | |
| B2 | 爬坡 | 旧文档 | | | | |
| B3 | 长稳 | 旧文档 | | | | |

## 关键结果

| 批次 | TTFT P50 | TTFT P95 | E2E P50 | E2E P95 | E2E P99 | Ingest Ready | QPS | Error Rate | Timeout Rate | Retrieval P95 | Cost / Request | Cost / Document | No Context Ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| B1 | | | | | | | | | | | | | |
| B2 | | | | | | | | | | | | | |
| B3 | | | | | | | | | | | | | |

## 资源与吞吐观察

| 批次 | Max Concurrent Sessions | Max Worker Queue Depth | Max Active SSE | 备注 |
| --- | ---: | ---: | ---: | --- |
| B1 | | | | |
| B2 | | | | |
| B3 | | | | |

## 现象记录

- 

## 问题定位

- 

## 结论

- 本次是否达到目标：
- 当前瓶颈位置：
- 是否允许上线 / 扩容 / 调参：

## 后续动作

1. 
2. 
3. 
```

## 7. 常用命令

### 7.1 新文档全链路

```bash
python3 scripts/metrics_benchmark.py \
  --python-base-url http://127.0.0.1:8000 \
  --gateway-base-url http://127.0.0.1:8080 \
  --file ./README.md \
  --async-requests 6 \
  --stream-requests 6 \
  --concurrency 1
```

### 7.2 已有文档只测 chat

```bash
python3 scripts/metrics_benchmark.py \
  --python-base-url http://127.0.0.1:8000 \
  --gateway-base-url http://127.0.0.1:8080 \
  --doc-id 123 \
  --async-requests 40 \
  --stream-requests 40 \
  --concurrency 8
```

### 7.3 绕过 RAG 测原始 vLLM

当目标是区分“模型本身慢”还是“检索链路慢”时，直接调用 OpenAI-compatible endpoint：

```bash
python3 scripts/vllm_benchmark.py \
  --base-url http://127.0.0.1:9000/v1 \
  --model Qwen3-14B \
  --mode both \
  --requests 20 \
  --concurrency 5
```

该脚本不经过 Gateway、FastAPI、LanceDB 或 reranker。报告中的 TTFT 和吞吐不能代表完整 RAG 体验，只适合建立模型服务基线。API Key 从 `VLLM_API_KEY` 或 `LLM_API_KEY` 读取，不进入命令参数和 JSON 输出。

### 7.4 多并发档位手动执行

```bash
for c in 1 2 4 8 16; do
  echo "== concurrency=${c} =="
  python3 scripts/metrics_benchmark.py \
    --python-base-url http://127.0.0.1:8000 \
    --gateway-base-url http://127.0.0.1:8080 \
    --doc-id 123 \
    --async-requests 40 \
    --stream-requests 40 \
    --concurrency "${c}"
done
```

## 8. 常见问题

### 8.1 没有成本数据

检查：

- `.env` 是否配置了单价
- LLM provider 是否返回 usage
- 当前是否走了 `mock_fallback`

### 8.2 `no_context ratio` 很高

优先检查：

- 文档是否完成 ingest
- 当前 `top_k` 是否过小
- embedding 模型是否更换后未重新 ingest
- 提问是否超出文档范围

### 8.3 `worker queue depth` 持续升高

优先检查：

- `CELERY_CONCURRENCY` 是否过低
- LLM / embedding 服务是否已饱和
- 请求并发是否已超过部署目标

### 8.4 benchmark 脚本启动失败

优先检查：

- 是否已 `source .venv/bin/activate`
- 是否已安装 `python_rag/requirements.txt`
- `requests` 是否存在
- `--python-base-url` 和 `--gateway-base-url` 是否可达

## 9. 结论应该回答什么

部署后性能验证不要只做一轮短压测。至少保留一轮单并发基线、一轮并发爬坡和一轮长稳结果，后续版本升级、模型切换、Worker 并发调整和机器规格变化，都用同一套模板对比。

最终结论至少回答：瓶颈位于哪一段，目标并发下 P95/P99 是否可接受，停止施压后队列是否恢复，错误是否来自限流、超时还是模型，质量与成本是否发生回退。无法回答这些问题的 QPS 数字，不足以支持上线或扩容决策。
