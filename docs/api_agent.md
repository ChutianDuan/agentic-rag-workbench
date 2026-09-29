# 在 JSON 与 SSE 之间：RAG / Agent API 契约

[返回文档地图](README.md)

RAG 客户端真正需要稳定的不是 URL 数量，而是三类契约：普通请求怎样表达成功和失败，异步任务怎样确认完成，SSE 断线后怎样继续同一次生成。

外部客户端优先使用 Drogon Gateway `/v1/*`；FastAPI `/internal/*` 只用于服务间调用和调试。兼容 `/api/agent/*` 不是推荐的新客户端入口。

## 先选择正确的交互模式

| 需求 | 推荐入口 | 完成依据 |
| --- | --- | --- |
| 上传并建库 | `POST /v1/documents` / `documents/web` | 轮询 task 到 `SUCCESS`，文档最终 indexed |
| 非流式普通 RAG | `POST /v1/sessions/{id}/messages` | task 成功后重新查询 messages |
| 流式普通 RAG | `POST /v1/chat/stream` | SSE `done` |
| 流式 Agent | `POST /v1/agent/chat/stream` | SSE `final` 后的 `done` |
| Trace 调试 | `/internal/agent/runs/{id}` | 持久化 run / steps / tool calls |

## JSON envelope 与 SSE

普通 JSON 接口统一返回：

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

FastAPI 验证错误、业务错误，以及 Gateway 参数校验、鉴权、限流错误都使用相同 envelope，并通过 HTTP status 表达传输层语义。SSE 错误通过 `type=error` 事件表达。

SSE 接口返回 `text/event-stream`。可续传事件同时在 SSE `id` 和 JSON `event_id` 中带编号：

```text
id: 7
event: agent_step
data: {"type":"agent_step","event_id":7,"run_id":1}

```

## 文档与建库

### 上传文档并提交 ingest

```http
POST /v1/documents
Content-Type: multipart/form-data
```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 是 | 支持 `.md`、`.txt`、`.json`、`.csv`、`.pdf`、`.docx`、`.xlsx`。 |
| `user_id` | int | 否 | 默认 `1`，用于归档和审计。 |

示例：

```bash
curl -X POST http://127.0.0.1:8080/v1/documents \
  -F "user_id=1" \
  -F "file=@./README.md"
```

响应重点字段：

```json
{
  "data": {
    "doc_id": 12,
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "status_url": "/v1/tasks/550e8400-e29b-41d4-a716-446655440000"
  }
}
```

### 从网页 URL 创建文档并提交 ingest

```http
POST /v1/documents/web
Content-Type: application/json
```

请求：

```json
{
  "user_id": 1,
  "url": "https://example.com/page"
}
```

说明：

- `url` 必须是 `http://` 或 `https://`。
- Gateway 会转发到 FastAPI 的 `/internal/documents/web/ingest`。
- 服务会抓取网页正文，保存为 Markdown 文档，再提交同一条 ingest / embedding / LanceDB 索引流程。
- 当前不处理网页登录、鉴权页面和复杂反爬场景。

响应重点字段：

```json
{
  "data": {
    "doc_id": 13,
    "filename": "web_example.com_example_page.md",
    "task_id": "550e8400-e29b-41d4-a716-446655440001",
    "status_url": "/internal/tasks/550e8400-e29b-41d4-a716-446655440001",
    "source_url": "https://example.com/page",
    "final_url": "https://example.com/page",
    "title": "Example Page"
  }
}
```

网页导入由 FastAPI 返回的 `status_url` 指向内部路径；外部客户端应使用 `task_id` 请求 Gateway 的 `/v1/tasks/{task_id}`。

### 查询任务状态

```http
GET /v1/tasks/{task_id}
```

任务成功时 `data.state` 为 `SUCCESS`。文档只有在后续 embedding 任务完成、`index_status=indexed` 后才真正可检索；解析 task 成功不等于索引已经完成。

### 查询文档

```http
GET /v1/documents?status=READY&limit=100
GET /v1/documents/{doc_id}
```

## 会话与消息

### 创建会话

```http
POST /v1/sessions
Content-Type: application/json
```

请求：

```json
{
  "user_id": 1,
  "title": "MVP Demo"
}
```

响应重点字段：

```json
{
  "data": {
    "session_id": 3,
    "user_id": 1,
    "title": "MVP Demo"
  }
}
```

### 查询消息与 citations

```http
GET /v1/sessions/{session_id}/messages
```

响应中的每条 assistant message 都包含 `citations`：

```json
{
  "message_id": 22,
  "role": "assistant",
  "content": "回答正文",
  "citations": [
    {
      "citation_id": 1,
      "doc_id": 12,
      "chunk_id": 45,
      "chunk_index": 3,
      "score": 0.91,
      "snippet": "引用片段"
    }
  ],
  "meta": {
    "answer_source": "agent",
    "agent_run_id": 8,
    "citation_count": 1
  }
}
```

## 普通 RAG 接口

### 异步普通 RAG 问答

```http
POST /v1/sessions/{session_id}/messages
Content-Type: application/json
```

请求：

```json
{
  "content": "这份文档讲了什么？",
  "top_k": 5
}
```

可选限定文档范围：

```json
{
  "content": "只根据指定文档回答",
  "top_k": 5,
  "doc_ids": [12, 13]
}
```

响应重点字段：

```json
{
  "data": {
    "message_id": 21,
    "task_id": "550e8400-e29b-41d4-a716-446655440002",
    "state": "PENDING",
    "status_url": "/internal/tasks/550e8400-e29b-41d4-a716-446655440002"
  }
}
```

普通 Chat 的 `status_url` 由 FastAPI 生成，指向内部路径；外部客户端用 `task_id` 轮询 `/v1/tasks/{task_id}`，成功后调用 `/v1/sessions/{session_id}/messages` 展示回答与 citations。

### 流式普通 RAG 问答

```http
POST /v1/chat/stream
Accept: text/event-stream
Content-Type: application/json
```

请求：

```json
{
  "session_id": 3,
  "content": "总结系统架构",
  "top_k": 5
}
```

SSE 事件：

| `type` | 说明 |
| --- | --- |
| `delta` | 回答增量文本。 |
| `done` | 结束事件，`meta` 包含 `assistant_message_id`、`citation_count`、`retrieval_ms` 等。 |
| `error` | 流式失败。 |

首次请求携带 `content`，Gateway 创建 user message 后通过响应头返回：

```http
X-User-Message-ID: 21
```

连接中断后，客户端使用同一个 `user_message_id`，移除 `content`，并携带最后确认的事件 ID：

```http
POST /v1/chat/stream
Last-Event-ID: 18
Content-Type: application/json

{"session_id":3,"user_message_id":21,"top_k":5}
```

如果重新发送原始 `content`，Gateway 会把它当成新消息，存在重复生成风险。续传状态默认保留 15 分钟；过期时返回明确 `error`，不会静默重启任务。

## 新 Agent 接口

### 网关流式 Agent 问答

```http
POST /v1/agent/chat/stream
Accept: text/event-stream
Content-Type: application/json
```

请求：

```json
{
  "session_id": 3,
  "message": "根据知识库总结这个系统的架构和核心链路",
  "trace_id": "demo-agent-001"
}
```

SSE 事件：

| `type` | 说明 |
| --- | --- |
| `gateway_metrics` | Gateway transport metadata，例如 Gateway 视角 TTFT；不参与 Agent event ID。 |
| `agent_step` | Agent 决策步骤状态。 |
| `tool_call` | 工具开始调用，工具名来自当前可用只读工具列表。 |
| `tool_result` | 工具返回结果或失败信息，`result` 使用 `ok/error/data` 结构。 |
| `delta` | 最终答案文本。MVP 当前在 Agent 完成后一次性输出。 |
| `final` | 最终回答，包含 `run_id`、`message_id`、`citations`。 |
| `done` | 流结束，`meta` 包含 `agent_run_id`、`steps_used`、`citation_count`。 |
| `error` | Agent 执行失败。 |

成功示例片段：

```text
event: tool_call
data: {"type":"tool_call","tool_name":"knowledge_search","status":"RUNNING"}

event: tool_result
data: {"type":"tool_result","tool_name":"knowledge_search","status":"SUCCESS","result":{"ok":true,"error":null,"data":{"total":5}}}

event: done
data: {"type":"done","meta":{"agent_run_id":8,"citation_count":5}}

```

Agent 续传请求必须复用相同 `trace_id` 并携带 Last-Event-ID。后端以 session + trace ID 识别同一次运行；客户端不应在重连时生成新的 trace ID。

### FastAPI 非流式 Agent 问答

```http
POST /internal/agent/chat
Content-Type: application/json
```

兼容路径：

```http
POST /api/agent/chat
```

请求：

```json
{
  "session_id": 3,
  "message": "根据知识库总结系统架构",
  "stream": false,
  "trace_id": "demo-agent-002"
}
```

响应：

```json
{
  "data": {
    "run_id": 8,
    "message_id": 22,
    "answer": "回答正文",
    "citations": [
      {
        "doc_id": 12,
        "chunk_id": 45,
        "chunk_index": 3,
        "score": 0.91,
        "snippet": "引用片段"
      }
    ]
  }
}
```

### 查询 Agent Run

```http
GET /internal/agent/runs/{run_id}
```

响应中的 `data.run` 包含：

- `status`: `RUNNING`、`SUCCESS`、`FAILED` 等。
- `input_json`: 用户问题。
- `output_json`: 最终答案、observations、citations。
- `total_steps` / `total_tool_calls`: 执行统计。
- `agent_version` 和 `meta_json.prompt_version`: Agent / Prompt 版本。
- `meta_json.retrieval_router`: 是否触发强制首轮 `knowledge_search` 以及触发原因。
- `prompt_tokens` / `completion_tokens` / `total_tokens`: run 级 token 汇总。
- `error_message`: 失败原因。

### 查询 Agent Steps 与工具调用

```http
GET /internal/agent/runs/{run_id}/steps
```

响应中的 `data.steps[]` 包含：

- `step_index`: 步骤序号。
- `decision`: `forced_tool_call`、`tool_call`、`final_answer` 或 `max_steps_final_answer`。
- `input_json` / `output_json`: LLM 输入输出摘要。
- `tool_calls[]`: 当前步骤下的工具调用，包含 `arguments_json`、`result_json`、`latency_ms`、`error_message`。

## Agent 工具

Agent 当前只允许只读工具。入口会先做轻量检索意图判断：问题命中项目文档、代码、架构、能力、上传文档、网页导入、embedding、索引等意图时，后端会先执行一次 `knowledge_search`，Trace 决策记为 `forced_tool_call`，再让 LLM 基于工具结果总结。

未命中强制检索路由，或强制检索后仍需要补充上下文时，Agent 继续按循环方式工作：LLM 返回 `tool_calls` 时，后端先按工具 schema 做轻量参数校验，再用工具自身 `timeout_ms` 执行工具并把结果写回上下文；LLM 不再返回 `tool_calls` 时，该轮内容作为最终答案。重复的同名同参数工具调用会被跳过并记录为失败工具结果。

工具结果统一为：

```json
{
  "ok": true,
  "error": null,
  "data": {}
}
```

`ok=true` 时 Agent 只使用 `data` 作为证据；`ok=false` 或 `error` 非空时视为工具失败。达到 `max_steps` 时，Agent 会追加一次无工具最终回答阶段，基于已有观察返回降级结论，不再把该情况作为 API 失败。

### `knowledge_search`

检索 indexed 知识库并返回相关 chunk。

输入 schema：

```json
{
  "query": "检索问题或改写后的查询",
  "top_k": 5
}
```

输出：

```json
{
  "ok": true,
  "error": null,
  "data": {
    "results": [
      {
        "doc_id": 12,
        "document_id": 12,
        "chunk_id": 45,
        "chunk_index": 3,
        "title": "README.md",
        "content": "截断后的 chunk 内容",
        "snippet": "引用片段",
        "score": 0.91,
        "lancedb_score": 0.82,
        "rerank_score": 0.91,
        "lancedb_rank": 4,
        "original_rank": 4
      }
    ],
    "total": 1,
    "retrieval": {
      "provider": "lancedb",
      "candidate_count": 50,
      "mysql_hydrated_candidate_count": 50,
      "rerank_used": true,
      "rerank_model": "Qwen/Qwen3-Reranker-0.6B",
      "vector_search_latency_ms": 12,
      "rerank_latency_ms": 38,
      "retrieval_latency_ms": 57
    }
  }
}
```

错误时：

```json
{
  "ok": false,
  "error": "retrieval backend unavailable",
  "data": {
    "results": [],
    "total": 0
  }
}
```

Agent 会把带有 `doc_id`、`chunk_id`、`chunk_index` 的结果转换为 citations；缺少这些字段的工具结果只进入 Trace，不写入引用表。

### 其他只读工具

| 工具 | 说明 |
| --- | --- |
| `get_document_detail` | 根据 `document_id` 查询文档元数据。 |
| `list_ready_documents` | 列出当前可检索的 indexed 文档。 |
| `list_message_citations` | 根据 assistant `message_id` 查询已保存 citations。 |

## 常见错误

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `session not found` | `session_id` 不存在。 | 先调用 `/v1/sessions` 创建会话。 |
| `no ready document index found` | 没有 indexed 文档，或 embedding 模型切换后旧索引不可用。 | 上传文档并等待 embedding 完成；已有文档可通过内部重建接口重新计算向量。 |
| Agent 没有工具调用 | 问题未命中强制检索路由，且被 LLM 判断为闲聊或不依赖文档。 | 提问中明确要求“根据知识库/项目文档/代码/架构/上传文档”。 |
| citations 为空 | 工具未检索到结果，或工具结果缺少 chunk 元数据。 | 检查 `tool_result.result.data.total` 和 `/internal/agent/runs/{run_id}/steps`。 |
| Agent 返回“已达到工具调用上限” | 达到 `max_steps` 安全上限。 | 查看 Trace 中 `termination_reason=max_steps`，必要时缩小问题或提高 `max_steps`。 |

## 客户端实现检查清单

- 同时检查 HTTP status 和 JSON `code`；非 SSE 错误使用统一 envelope。
- 异步路径以 task 状态为准，不把提交成功当成业务完成。
- SSE parser 保留 `id`、`event` 和多行 `data`，忽略 heartbeat comment。
- 只在收到 `done` 后认为流完整结束；`final` 表示答案已保存，但传输仍有终止事件。
- 普通 Chat 续传复用 `X-User-Message-ID`；Agent 续传复用 `trace_id`。
- UI 不要从缺失字段推断健康、逐节点耗时或句子级 citation span。
