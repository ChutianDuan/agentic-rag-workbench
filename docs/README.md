# RAG Gateway Stack 文档导航

[返回项目首页](../README.md)

根 README 介绍整体架构与执行链；本目录提供运行、接口、设计、验证和实验的专题说明。各文档保留原文件名和位置，便于沿用已有链接。

## 快速开始

| 目标 | 阅读顺序 |
| --- | --- |
| 第一次了解项目 | [架构概览](architecture.md) → [项目首页](../README.md) |
| 本地启动与演示 | [运行环境与排障](environment.md) → [演示案例](demo_cases.md) |
| 调用接口或实现客户端 | [API 契约](api_agent.md) → [Gateway 鉴权与限流](gateway_auth_rate_limit.md) |
| 修改 Agent 或检索逻辑 | [Agent 设计](agent_mvp.md) → [Ingest、检索与容量](rag_ingest_retrieval_capacity.md) → [数据库关系](数据库关系.md) |
| 测量性能与定位瓶颈 | [监控指标](monitoring_metrics.md) → [性能测试指南](performance_test_guide.md) |

## 运行与接口

| 文档 | 内容 | 性质 |
| --- | --- | --- |
| [运行环境与排障](environment.md) | Python 环境、Gateway 依赖、模型配置、GPU、启动与故障定位 | 当前说明 |
| [API 契约](api_agent.md) | 文档、任务、会话、RAG / Agent、工具与 SSE 续传 | 当前契约 |
| [Gateway 鉴权与限流](gateway_auth_rate_limit.md) | API Key、principal、IP / 用户限流和 SSE 连接保护 | 当前说明 |
| [演示案例](demo_cases.md) | 成功、无证据、无需检索、文档范围与接口交叉验证 | 操作指南 |

完整启动脚本参数见 [scripts/README.md](../scripts/README.md)。

## 架构与数据

| 文档 | 内容 | 性质 |
| --- | --- | --- |
| [架构概览](architecture.md) | 系统边界、主要流程、架构图与代码入口 | 当前说明 |
| [Agent 设计](agent_mvp.md) | MVP 范围、检索路由、只读工具、Trace 和验收入口 | 当前说明 |
| [数据库关系](数据库关系.md) | 业务表、Agent Trace、外键与非外键逻辑关联 | 结构参考 |
| [Ingest、检索与容量](rag_ingest_retrieval_capacity.md) | 入库与查询成本、向量存储估算、容量边界与工程建议 | 实现说明与容量估算 |

## 监控与性能验证

| 文档 | 内容 | 性质 |
| --- | --- | --- |
| [监控指标](monitoring_metrics.md) | 系统资源、队列、请求、成本和检索质量的来源与限制 | 当前说明 |
| [性能测试指南](performance_test_guide.md) | 固定输入、预热、并发测试、结果判读与留档模板 | 操作指南 |

## 后续规划

| 文档 | 内容 | 性质 |
| --- | --- | --- |
| [双模型 RAG 优化路线图](rag_evaluation_roadmap.md) | 四路选择、回答模型切换、质量与成本对照评测 | 后续规划；当前检索路由见 Agent 设计 |

## 实验与历史记录

| 文档 | 内容 | 性质 |
| --- | --- | --- |
| [Embedding 微调复盘](embedding_finetune.md) | KALM LoRA 指标、召回取舍及接入索引的兼容要求 | 历史实验与接入说明 |
| [Agent MVP 联调报告](agent_mvp_test_report.md) | 工具协议、检索与失败场景的验证证据 | 历史快照，验证记录截至 2026-07-01 |
| [代码审核快照](code_review_completion_assessment.md) | 审核结论、已收敛问题、风险与后续建议 | 历史快照，整理于 2026-07-13 |

## 图片资源

| 文件 | 用途 |
| --- | --- |
| [architecture-overview.png](architecture-overview.png) | 架构与流程图，对应架构概览 |
| [rag-workbench-overview.png](rag-workbench-overview.png) | 工作台概览，供项目首页展示 |
| [项目运行前端界面.png](项目运行前端界面.png) | 前端运行截图参考 |

## 如何理解文档中的结论

阅读时注意内容性质：

- **当前契约**：接口、启动命令、数据结构和运行约束，应与当前代码一致。
- **设计讨论**：解释为什么选择 MySQL、LanceDB、Celery、SSE 或只读工具，以及它们的取舍。
- **后续规划**：描述待实施和待验证的目标；已有基础能力与计划增量分别说明，不能当成已交付功能。
- **历史快照**：测试报告和代码审核只证明当时执行过什么，不代表当前进程仍处于同样状态。

文档不会把默认配置描述成实际运行配置，也不会把“接口存在”描述成“服务健康”。涉及性能、容量和质量的数字都需要结合对应实验条件阅读。

## 文档维护约定

- 外部调用优先写 Gateway `/v1/*`；FastAPI `/internal/*` 只用于服务间调用和调试。
- FastAPI 和 Gateway 的非 SSE API，包括安全错误，均使用 `{code, message, data}`。SSE 使用有 `type` 的编号事件，并以 `done` 或 `error` 结束。
- SSE 续传使用同一次运行的标识和 `Last-Event-ID`；断线后生成继续，续传状态过期时明确失败。最终消息和 citations 落库后才能发出 `done`。
- 启动命令以 `scripts/start_all.sh` 为统一入口；本地 vLLM 保持独立启动。
- 新增真实验证结果时记录日期、命令、环境边界和未执行项。
- 专题文档提供返回本页的链接；长文提供章节目录，标题变化时同步更新锚点。
- 新增文档时更新对应分类；图片也登记用途。历史记录保留原始条件与结果，当前行为以对应专题和代码为准。
- 架构变化先更新根 README，再更新这里对应的专题文章。
