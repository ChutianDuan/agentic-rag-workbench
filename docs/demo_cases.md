# 如何演示一条不造假的 RAG / Agent 链路

[返回文档地图](README.md)

好的演示不是只挑一个肯定能回答的问题。它应该同时展示成功、无证据、不需要检索和工具失败等边界，让观看者能判断系统是在执行真实链路，还是只输出一段看起来合理的文字。

下面的案例先跑完整主线，再用几个对照组证明普通 RAG、Agent 决策、citations 和失败语义彼此独立。

## 先固定演示环境

启动完整工作台并确认服务状态：

```bash
START_FRONTEND=true bash scripts/start_all.sh start
bash scripts/start_all.sh status
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8000/internal/health
```

准备演示文档：

```bash
ls ./README.md
```

`README.md` 是仓库内现有的演示材料；也可以换成任意小型 `.md` 或 `.txt` 文件。

如果启用了 Gateway API Key，下面的 CLI 请求还需要增加 `X-API-Key` header。不要为了演示临时把真实密钥写进文档或命令历史。

## 案例 1：把完整主线摊开

目标：完成验收标准中的完整链路。

### 操作

1. 打开 `http://127.0.0.1:5173`，在 Settings 创建或选择用户。
2. 从 Documents 或左侧文档轨道上传 `README.md`；也可以导入可直接访问的 `http(s)` URL。
3. 观察 Parsing、Chunking、Embedding，等待最终状态变为 Indexed。
4. 进入 Sessions 创建会话，保持 `Agent + RAG` 开启。
5. 在中央 Execution Flow 选择 `CrossEncoder 重排`，准备观察候选数、rank 和 score 变化。
6. 提问：

```text
根据知识库总结这个系统的架构、核心链路和 Agent MVP 能力。
```

### 预期

- 上传后出现 ingest 任务，最终 `SUCCESS`，文档进入 Indexed。
- 中央流程从用户问题推进到保存回答与 citations，不应在工具结果到达前把 LanceDB、MySQL 和 rerank 标成完成。
- Agent Trace 出现执行步骤。项目文档类问题通常会先出现一次 `forced_tool_call`。
- Trace 中出现 `knowledge_search` 的 `tool_call` 和 `tool_result`，工具结果为 `ok/error/data` 结构。
- 回答内容基于上传文档。
- 回答完成后证据区域展示至少 1 条 citation，包含 `doc_id`、`chunk_index`、score 和 snippet。

## 案例 2：用普通 RAG 做稳定对照

目标：确认普通 RAG 基线路径仍可运行。

```bash
bash scripts/e2e_all.sh ./README.md
```

预期：

- 创建用户成功。
- 上传文档成功。
- ingest 任务成功。
- 创建 session 成功。
- `/v1/sessions/{session_id}/messages` 提交普通 RAG Chat 任务成功。
- chat 任务成功。
- 最终消息中 assistant message 带 citations。

## 案例 3：绕过前端观察原始 SSE

目标：不依赖前端，直接展示 Agent SSE 事件。

先准备 `SESSION_ID`。如果已经通过前端创建过会话，可直接使用对应 ID；也可以用接口创建，并从响应的 `data.session_id` 读取实际 ID。后续命令中的 `1`、`doc_ids:[1]` 和 Run ID 仅为示例，执行时应替换为本次创建的值：

```bash
curl -X POST http://127.0.0.1:8080/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"user_id":1,"title":"Agent CLI Demo"}'
```

调用 Agent：

```bash
curl -N -X POST http://127.0.0.1:8080/v1/agent/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"session_id":1,"message":"根据知识库总结这个系统的架构和核心链路","trace_id":"demo-cli-agent"}'
```

预期 SSE 事件顺序通常为：

```text
agent_step -> tool_call -> tool_result -> agent_step -> delta -> final -> done
```

其中第一个 `agent_step` 可能是后端强制首轮检索产生的 `forced_tool_call`，随后才是 LLM 决策步骤。

每个可续传数据事件还应带递增 `id`。`gateway_metrics` 属于 Gateway transport metadata，不进入 Agent 事件编号。

记录 `done.meta.agent_run_id` 或 `final.run_id`，查询 Trace：

```bash
curl http://127.0.0.1:8000/internal/agent/runs/1
curl http://127.0.0.1:8000/internal/agent/runs/1/steps
```

查询 citations：

```bash
curl http://127.0.0.1:8080/v1/sessions/1/messages
```

## 案例 4：没有证据时，空 citations 是正确结果

目标：展示 Agent 在检索不到证据时不会编造。

提问：

```text
文档里有没有区块链支付清结算模块？请只根据知识库回答。
```

预期：

- Agent 仍会调用 `knowledge_search`。
- 如果知识库没有相关片段，`tool_result.result.ok=true` 且 `tool_result.result.data.total` 为 `0`。
- 最终回答应说明当前知识库证据不足。
- citations 为空是合理结果。

## 案例 5：无需检索时，不调用工具才是正确行为

目标：展示 Agent 可直接回答简单闲聊，不调用工具。

提问：

```text
你好
```

预期：

- Trace 中只有 final answer 决策步骤，或无 `tool_call`。
- 回答不应伪造引用。
- citations 为空。

## 案例 6：限定普通 RAG 的证据范围

目标：确认普通 RAG 兼容显式文档范围。

```bash
curl -X POST http://127.0.0.1:8080/v1/sessions/1/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"只根据这个文档总结核心内容","top_k":5,"doc_ids":[1]}'
```

预期：

- 返回 chat `task_id`。
- 任务成功后，消息 citations 的 `doc_id` 应来自指定范围。

## 不要只看页面：用接口交叉验证

查看已经可检索的文档：

```bash
curl "http://127.0.0.1:8080/v1/documents?status=READY&limit=20"
```

查看任务：

```bash
curl "http://127.0.0.1:8080/v1/tasks?limit=20"
```

查看监控概览：

```bash
curl http://127.0.0.1:8080/v1/monitor/overview
```

查看 FastAPI OpenAPI：

```bash
curl http://127.0.0.1:8000/openapi.json
```

## 演示时应该明确说出的边界

- 普通 RAG 是稳定基线：用户问题直接进入检索、Prompt 组装和回答生成。
- Agent 在原有检索能力上增加工具决策层；项目文档、代码、架构、能力、上传文档、网页导入等意图会先由后端轻量路由强制执行一次 `knowledge_search`，再交给 LLM 总结。
- 当前工具只读，避免演示环境中出现写操作风险。
- Citations 统一复用消息引用能力，因此普通 RAG 和 Agent 使用相同证据数据结构。
- 工具失败、无检索结果和服务 degraded 都是应当展示的真实状态，不要在演示前端用固定成功数据替代。
