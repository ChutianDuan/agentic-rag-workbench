# 从 Demo 到工程系统：一次 RAG 代码审核的历史快照

[返回文档地图](README.md)

> 审核基线：2026-05-11。本文在 2026-07-13 按当前仓库重新整理结构，并标注后续已经发生的变化。它不是当前线上健康报告，也不使用主观百分比代替验收标准。

“这个项目是不是还只是 Demo”不能只看页面能否回答问题。更有区分度的判断是：数据有没有明确事实来源，失败能不能回查，异步状态是否可恢复，接口边界是否稳定，系统能否在没有模型输出时仍然解释发生了什么。

## 审核结论

项目已经越过了单文件 RAG Demo 的边界：文档 ingest、LanceDB 召回、MySQL 回表、CrossEncoder rerank、异步与流式问答、citations、Agent Trace、监控、鉴权和限流形成了闭环。

但“形成闭环”不等于“生产就绪”。当前更准确的定位是：适合本地工程验证、作品展示和小团队 alpha；如果进入生产，还需要补齐租户边界、审计、部署编排、Gateway 自动化测试、向量库运维和高并发 SSE 模型。

## 从最初审核到现在，哪些问题已经收敛

| 主题 | 2026-05 的风险 | 当前仓库状态 |
| --- | --- | --- |
| Chat message 校验 | 异步与流式路径可能出现规则漂移 | 校验逻辑已进入共享业务模块，并有回归测试 |
| 检索 fan-out | 逐文档读取旧 FAISS 文件会放大 I/O | 默认改为 LanceDB 全局召回，再按 `chunk_id` 回 MySQL |
| Gateway query 转发 | 手工拼 query 容易破坏特殊字符 | 已统一 URL encode helper |
| SSE 中断 | 客户端断开后容易重启生成或丢事件 | 普通 Chat 与 Agent 都有编号事件、Last-Event-ID 和过期失败语义 |
| Agent 可解释性 | 只有最终答案，难以证明工具行为 | run、step、tool call、event ID 和 citations 均可回查 |
| 本地进程管理 | 启停、端口和日志分散 | `scripts/start_all.sh` 已统一服务目标、健康检查、日志和进程组退出 |
| 前端观测 | 以聊天与卡片为中心 | 当前工作台以 Execution Flow、证据关系和 Agent Trace 为中心 |

这些改动说明架构正在从“功能存在”转向“行为可证明”。

## 当前仍然值得优先处理的风险

### 1. Gateway 错误契约已统一，仍需合约测试

FastAPI 与 Gateway 的非 SSE 成功和错误响应均使用 `{code, message, data}`；SSE 使用带 `type`、递增 `id` 和终止事件的独立协议。

### 2. SSE 并发已经有限制，但仍按流占用线程

Gateway 使用 `GATEWAY_MAX_STREAMS` 限制活跃 SSE 数量，避免线程无限增长；实现仍然是 detached OS thread 加阻塞 libcurl。中高并发前应验证线程、连接、内存和慢客户端回收，长期可考虑受控线程池或异步 HTTP 客户端。

### 3. LanceDB 是可重建索引，但运维流程仍需补齐

当前路径已经能写入、过滤和召回，但 orphan vector 清理、compaction、备份恢复、模型切换重建和百万级数据验证仍需要明确 Runbook。MySQL 是事实来源并不意味着可以忽略索引恢复时间。

### 4. 自动化覆盖在 Python 侧更强，Gateway 侧仍偏弱

Python 已有 Agent、SSE、API envelope、检索与工具回归测试；C++ Gateway 可以完成构建验证，但仓库当前没有可由 `ctest` 执行的 handler 或集成测试。鉴权、限流、错误 envelope、SSE header 与断线代理仍主要依赖代码审计和真实环境验证。

### 5. 对外查询能力还不完整

外部 API 可以创建 session 并处理当前 run，但尚无完整的历史 Sessions / Agent Runs 列表接口。前端因此不会伪造刷新后不存在的历史数据。真正的工作台恢复能力需要补 Gateway 列表和详情代理。

## 按模块看完成度

### Drogon Gateway

已经形成稳定入口：`/v1/*`、上传、CORS、API Key、Redis 限流、健康聚合、SSE 代理和并发 slot。下一阶段重点不是继续增加业务路由，而是统一安全错误、补 request ID / 审计和自动化 handler 测试。

### FastAPI 与 Celery

文档、检索、Chat、Agent、Trace、监控和统一异常处理已有清晰模块边界；解析与 embedding 使用 Celery 拆分任务。下一阶段应增加真实业务 QA 集、故障注入和索引恢复验证，而不是继续堆更多工具。

### 前端与文档

工作台已能呈现文档状态、执行河流、回答证据、SSE Trace 和依赖基线。文档现在按架构、运行、API、演示、容量和历史证据分层。仍受后端字段限制的内容需要继续明确显示 unknown，而不是用前端推断替代接口。

## 一条更可信的后续路线

| 优先级 | 目标 | 可验证交付物 |
| --- | --- | --- |
| P1 | 验证协议边界 | Gateway 安全错误的 contract tests；request ID 全链路透传 |
| P1 | 建立恢复能力 | LanceDB cleanup / backup / restore Runbook；embedding 切换重建演练 |
| P1 | 补 Gateway 测试 | 鉴权、限流、query encode、SSE header 和断线的自动化测试 |
| P2 | 建真实质量基线 | 固定 QA 集、Recall@K / MRR / NDCG、答案引用人工验收 |
| P2 | 验证容量 | 并发 SSE、百万 chunk、MySQL 回表和 rerank 候选数基准 |
| P3 | 生产化 | 多租户隔离、密钥管理、审计、部署编排和灰度模型切换 |

## 如何重新执行当前审核

代码级基线：

```bash
python -m compileall python_rag tests
python -m pytest tests
bash scripts/ci_smoke.sh
cmake --build cpp_gateway/build
```

运行级基线：

```bash
START_FRONTEND=true bash scripts/start_all.sh start
bash scripts/start_all.sh status
bash scripts/e2e_all.sh ./README.md
```

完整 E2E 会写入真实数据库并调用当前模型服务，不能用一次历史通过结果代替重新执行。审核也不应再使用“85% 完成度”这类难以复现的数字；每个结论都应该对应代码、测试、接口响应或运行记录。
