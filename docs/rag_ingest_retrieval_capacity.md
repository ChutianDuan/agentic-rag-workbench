# 从一个文件到百万 Chunk：RAG Ingest、检索与容量边界

[返回文档地图](README.md)

RAG 容量问题经常被简化成“向量库能存多少条”。实际系统中，向量只是其中一层：原文和 chunk 在 MySQL，索引在 LanceDB，embedding 与 reranker 消耗计算，LLM 又受上下文和生成吞吐影响。最先成为瓶颈的通常不是纯向量磁盘。

本文沿 ingest 和 query 两条链路拆开成本，再用可替换公式估算 chunk、向量、回表和模型负载。所有容量数字都是规划值，不是当前机器的 benchmark 结果。

为了让计算直观，示例统一假设向量维度 `d=512`、类型 `float32`。实际维度由当前 embedding 模型决定；切换到其他维度时，纯向量存储和暴力距离计算会近似按 `d / 512` 线性缩放。当前默认存储配置是 LanceDB，chunk 参数为 `INGEST_CHUNK_SIZE=800`、`INGEST_CHUNK_OVERLAP=100`。

## 1. 先把两条成本链分开

```text
文件上传 / 网页 URL
  |
  v
文件落盘或网页正文保存 + documents 元数据入库
  |
  v
Celery ingest 异步任务
  |
  |-- 文档解析 / 文本抽取
  |-- chunk 切片
  |-- doc_chunks 入库
  |-- chunk embedding 向量化
  |-- LanceDB 向量写入
  |-- document_indexes 写入索引元数据
  v
文档 indexed

用户提问
  |
  v
问题 embedding 向量化
  |
  v
LanceDB Top-K 或 Candidate-K 召回
  |
  v
重排序 rerank
  |
  v
选择最终上下文 chunks
  |
  v
拼接 prompt
  |
  v
发送给 OpenAI-compatible LLM / vLLM
  |
  v
回答 + citations 落库
```

整个系统可以拆成两条主链路：

| 链路 | 触发时机 | 主要成本 | 主要产物 |
| --- | --- | --- | --- |
| Ingest 建库链路 | 文件上传或网页导入后 | 文档解析、chunk、embedding、LanceDB 写入 | `doc_chunks`、LanceDB `chunk_vectors`、`document_indexes` |
| Query 问答链路 | 用户提问时 | query embedding、LanceDB 召回、MySQL 回表、rerank、LLM 推理 | answer、citations、messages |

## 2. 上传与 Ingest 过程

### 2.1 文档入口

用户通过前端或 API 上传文件后，外部入口会完成几件事：

1. 校验文件类型和基本请求参数。
2. 计算文件 `sha256`，用于去重和追踪。
3. 将原始文件保存到本地 `data/uploads`。
4. 在 MySQL `documents` 表中写入文件元数据，包括文件名、MIME、大小、存储路径、状态等。
5. 提交 Celery ingest 异步任务，避免上传接口被后续耗时处理阻塞。

网页 URL 入口使用同一套后续流程。`POST /v1/documents/web` 接收 `http(s)` URL，Gateway 转发到 FastAPI 的 `/internal/documents/web/ingest`；服务抓取网页正文，保存为 Markdown 文档，写入 `documents` 记录，然后提交 Celery ingest 任务。当前默认不处理网页登录、鉴权页面和复杂反爬限制。

### 2.2 文本抽取

Celery Worker 会根据文件类型抽取纯文本。抽取结果必须非空，否则文档会进入失败状态。

文本抽取完成后，系统进入切片阶段。

### 2.3 Chunk 切片

当前项目使用简单字符切片：

- `chunk_size = 800`
- `chunk_overlap = 100`
- 实际步长 `step = chunk_size - chunk_overlap = 700`

切片的目的不是压缩文本，而是让长文档变成适合 embedding 和 LLM 上下文拼接的小片段。overlap 的作用是保留相邻片段之间的上下文连续性，减少答案刚好跨 chunk 边界时的召回失败。

粗略估算 chunk 数：

```text
如果文档字符数 <= chunk_size:
  chunk_count = 1

否则:
  chunk_count ≈ ceil((字符数 - chunk_size) / (chunk_size - overlap)) + 1
```

在默认配置下，可以近似写成：

```text
chunk_count ≈ 文档字符数 / 700
```

示例：

| 文档纯文本规模 | 估算 chunk 数 |
| ---: | ---: |
| 10,000 字符 | 约 15 |
| 100,000 字符 | 约 143 |
| 1,000,000 字符 | 约 1,429 |
| 10,000,000 字符 | 约 14,286 |

切片完成后，每个 chunk 会写入 `doc_chunks` 表，保留 `doc_id`、`chunk_index`、`text`、`tokens_est` 等字段。

### 2.4 Chunk embedding 向量化

每个 chunk 会被送入 embedding 模型，生成一个固定长度向量。以下以 `512` 维举例；程序实际使用模型输出的维度：

```text
chunk_text -> embedding_model -> float32[d]  # 示例 d=512
```

向量化阶段有几个关键点：

| 关键点 | 说明 |
| --- | --- |
| 同一模型 | 文档 chunk 和用户问题必须使用同一个 embedding 模型。 |
| 同一向量空间 | 切换模型后，旧索引必须重建，否则 query 向量和 doc 向量不在同一语义空间。 |
| 同一归一化策略 | 当前支持 `EMBEDDING_NORMALIZE=true`，归一化后使用内积检索基本等价于余弦相似度。 |
| query/document prefix | 如果模型需要区分检索任务中的 query 和 document，应固定配置 `EMBEDDING_QUERY_PREFIX` 和 `EMBEDDING_DOCUMENT_PREFIX`。 |
| 批处理 | 当前 `EMBEDDING_BATCH_SIZE=32`，批量 embedding 可以显著提高吞吐。 |

512 维 `float32` 向量的原始大小：

```text
512 * 4 bytes = 2048 bytes ≈ 2 KB / chunk
```

所以仅向量原始数据部分的存储规模为：

| chunk 数 | 向量原始大小 |
| ---: | ---: |
| 10,000 | 约 19.5 MB |
| 100,000 | 约 195 MB |
| 1,000,000 | 约 1.91 GB |
| 10,000,000 | 约 19.1 GB |

注意：这是纯向量大小，不包含 MySQL 中的 chunk 文本、LanceDB 表结构、索引元数据、日志和任务元数据。

### 2.5 LanceDB 向量索引

当前默认配置使用 LanceDB 作为向量存储：

```text
VECTOR_STORE_PROVIDER=lancedb
RETRIEVAL_RECALL_PROVIDER=lancedb
LANCEDB_PATH=./data/lancedb
LANCEDB_TABLE=chunk_vectors
```

含义：

- ingest 阶段把每个 chunk 的向量、`doc_id`、`chunk_id`、`chunk_index` 和必要元数据写入 LanceDB。
- `doc_chunks` 仍然是 chunk 文本的主存储；查询时 LanceDB 先返回 chunk 标识，再按 `chunk_id` 回 MySQL 补齐文本。
- 当 embedding 已 L2 normalize 时，向量相似度排序可按 cosine / inner product 语义理解，具体取决于 LanceDB 查询实现和当前版本。

系统会在本地维护一个 LanceDB 数据目录：

| 文件 | 作用 |
| --- | --- |
| `data/lancedb/chunk_vectors` | LanceDB 向量表，保存 chunk 向量和检索元数据 |
| MySQL `doc_chunks` | 保存 chunk 文本、token 估算和索引状态 |
| MySQL `document_indexes` | 保存每个文档的索引状态、模型、维度和 chunk 数 |

同时，`document_indexes` 表会记录：

- `doc_id`
- `index_type`
- `embedding_model`
- `dimension`
- `index_path` / `mapping_path`（LanceDB 路径下通常为空，保留兼容旧 FAISS 字段）
- `chunk_count`
- `status`

这里保存 `embedding_model` 很重要。查询时系统会检查当前 embedding 模型是否和建索引时一致。如果不一致，会拒绝查询；已有文档可通过 `POST /internal/lancedb/documents/{doc_id}/rebuild` 重建向量。

FAISS 相关代码仍保留在项目中，可作为历史实现或回退参考；但当前默认召回路径已经是 LanceDB。

## 3. 提问与检索过程

### 3.1 问题 embedding

用户输入问题后，系统先把问题向量化：

```text
question -> embedding_model -> float32[d]  # 与建索引时的 d 一致
```

这一步必须和文档 chunk 使用同一个模型、同一个归一化策略。否则 LanceDB 返回的最近邻没有稳定语义意义。

问题 embedding 的成本通常远小于文档 ingest，因为一次请求只需要编码一个 query；但在高并发下，query embedding 仍然会成为共享模型服务的 QPS 压力来源。

### 3.2 LanceDB Top-K 召回

LanceDB 根据 query vector 在 indexed 文档范围内搜索相似 chunk：

```text
query_vector -> LanceDB chunk_vectors -> candidate chunks
```

返回内容包括：

- `chunk_id`
- `doc_id`
- `chunk_index`
- `lancedb_score`
- `lancedb_distance`

LanceDB 召回后，系统会用返回的 `chunk_id` 回 MySQL 读取 chunk 文本，再交给 reranker 或按召回顺序截断。也就是说，向量库负责“找候选”，MySQL 负责“存正文”。

如果直接把 LanceDB 的 `top_k` 结果送给 LLM，链路简单、延迟低，但排序质量完全依赖 embedding 模型。embedding 检索擅长“粗召回”，但不一定擅长精细排序，尤其在以下场景容易出错：

- 问题很短，但文档中相似表述很多。
- chunk 语义接近，但只有少数片段真正回答问题。
- 领域术语、数字、否定句、条件约束较多。
- top-1 相似度高，但实际是背景信息，不是答案依据。

因此建议把向量召回数量分成两层：

| 参数 | 建议含义 | 常见范围 |
| --- | --- | --- |
| `candidate_top_k` / `RETRIEVAL_DENSE_TOP_K` | LanceDB 粗召回候选数 | 20 到 50 |
| `final_top_k` | rerank 后送入 LLM 的 chunk 数 | 3 到 5 |

也就是说，不建议只检索 3 个再重排 3 个。更合理的是先召回 20 到 50 个候选，再用 reranker 选出最值得进入 prompt 的 3 到 5 个。

### 3.3 Rerank 重排序

重排序是本系统质量提升的关键点之一。

embedding 检索做的是双塔向量相似度：

```text
score = similarity(embed(question), embed(chunk))
```

它的优点是快，可以提前离线算好文档向量；缺点是 query 和 chunk 在编码后才比较，交互不充分。

reranker 通常使用 cross-encoder 或专门的排序模型，对每个候选 pair 做更细粒度判断：

```text
rerank_score = reranker(question, chunk)
```

它的优点是排序质量更高，因为模型能同时看到问题和候选 chunk；缺点是每次查询都要对多个候选重新计算，成本明显高于单纯向量召回。

推荐流程：

```text
1. LanceDB 召回 candidate_top_k = 30
2. 对 30 个候选构造 pair: (question, chunk)
3. reranker 批量打分
4. 按 rerank_score 从高到低排序
5. 去重、过滤低分片段
6. 取 final_top_k = 3 到 5
7. 拼接给 LLM
```

可以采用三类重排序策略：

| 策略 | 质量 | 延迟 | 适用场景 |
| --- | --- | --- | --- |
| 仅向量召回分数排序 | 中 | 最低 | demo、小文档、低成本优先 |
| Cross-encoder reranker | 高 | 中高 | 正式 RAG 问答、准确率优先 |
| LLM rerank | 高但成本大 | 高 | 少量高价值请求、复杂推理场景 |

如果引入 cross-encoder reranker，建议保留 LanceDB 召回分数用于观测，但最终排序以 reranker 分数为主。也可以做混合分数：

```text
final_score = alpha * normalized_vector_score + beta * rerank_score
```

在业务上更推荐先简单使用 reranker 分数排序，只有当 reranker 对某些场景不稳定时再引入融合分数。

当前实现已经新增 cross-encoder reranker：`top_k` / `RETRIEVAL_RERANK_TOP_K` 表示最终送入 prompt 的 chunk 数，`candidate_top_k` / `RETRIEVAL_DENSE_TOP_K` 表示 LanceDB 粗召回候选数。系统会保留 `lancedb_score`、`lancedb_rank`、`rerank_score` 和 `original_rank`，便于分析 rerank 前后的排序变化。

### 3.4 上下文拼接

重排序后，系统会把最终 chunk 拼接成 context block，再和用户问题组成 prompt。

当前系统有 `CHAT_MAX_CHUNK_CHARS=1000`，用于限制单个 chunk 进入 prompt 的最大字符数。假设 `final_top_k=5`：

```text
最大上下文字符数 ≈ 5 * 1000 = 5000 字符
```

最终 prompt 大致包含：

- system 指令
- 检索到的 chunk 内容
- chunk 元数据，例如 `doc_id`、`chunk_index`、`score`
- 用户问题

控制 `final_top_k` 的意义很大：它直接影响 LLM 的 prompt tokens、TTFT、显存占用和吞吐。

### 3.5 发送给 LLM / vLLM

当前默认配置走 OpenAI-compatible 远端 LLM；显式配置 `LLM_RUNTIME=local_vllm` 时可以使用本地 vLLM。对生成模型来说，主要负载来自：

- prompt tokens：上下文越长，prefill 越慢。
- completion tokens：回答越长，decode 越慢。
- 并发流式请求数：影响排队、TTFT；本地 vLLM 还会占用显存 KV cache。
- 模型大小和量化方式：模型越大，吞吐越低，显存需求越高。

RAG 系统里，检索阶段通常不是最大瓶颈。真正的线上瓶颈常常是：

```text
LLM / vLLM 推理 > reranker > embedding > LanceDB 召回
```

如果 LanceDB 表很大、过滤范围很宽，或回 MySQL 补齐 chunk 文本很慢，检索阶段也可能成为瓶颈。此时优先看 `retrieval_ms`、`lancedb_ms`、`rerank_ms`、候选数和 MySQL 查询耗时。

## 4. 以 512 维示例建立可缩放的容量基线

### 4.1 每个 chunk 的基础成本

按 `512` 维、`float32` 计算：

```text
vector_bytes_per_chunk = 512 * 4 = 2048 bytes ≈ 2 KB
```

更一般的公式是 `dimension * bytes_per_value`。例如维度翻倍到 1024，纯向量部分也近似翻倍；MySQL chunk 文本不会随维度变化。

但系统实际存储不只有向量：

| 存储项 | 估算 |
| --- | ---: |
| LanceDB 向量 | 约 2 KB / chunk（仅 512 维示例） |
| MySQL `doc_chunks.text` | 约 1 到 4 KB / chunk，取决于文本长度和编码 |
| LanceDB / MySQL 元数据、索引结构开销 | 数百 bytes 到数 KB / chunk |

因此，比较务实的总磁盘估算是：

```text
每个 chunk 约 4 到 10 KB
```

如果中文文本较多，UTF-8 下单字符可能占 3 bytes；再加上数据库行开销和向量表元数据，实际值会更接近上限。

### 4.2 按 chunk 数估算磁盘

| chunk 总数 | 纯向量大小 | 估算总磁盘 |
| ---: | ---: | ---: |
| 10,000 | 约 19.5 MB | 约 40 到 100 MB |
| 100,000 | 约 195 MB | 约 0.4 到 1 GB |
| 1,000,000 | 约 1.91 GB | 约 4 到 10 GB |
| 10,000,000 | 约 19.1 GB | 约 40 到 100 GB |

这些数字只用于容量规划。真实值要以 `data/lancedb`、MySQL 表大小和上传原文大小为准。

### 4.3 按文档规模估算

使用默认切片参数时：

```text
chunk_count ≈ 文档字符数 / 700
```

| 单文档纯文本字符数 | chunk 数 | 向量大小 | 估算总存储 |
| ---: | ---: | ---: | ---: |
| 10,000 | 约 15 | 约 30 KB | 小于 1 MB |
| 100,000 | 约 143 | 约 286 KB | 约 1 MB 级别 |
| 1,000,000 | 约 1,429 | 约 2.8 MB | 约 6 到 15 MB |
| 10,000,000 | 约 14,286 | 约 27.9 MB | 约 60 到 150 MB |

因此，512 维本身并不会让存储爆炸。真正需要关注的是：

- MySQL 中 chunk 文本是否过大。
- LanceDB 表是否持续增长且缺少清理、备份和恢复流程。
- 查询是否需要跨所有 indexed 文档检索。
- 是否需要按用户、租户或知识库做过滤和分片。

## 5. 检索计算量估算

向量召回的底层计算量可以按“候选向量数乘以维度”粗略理解：

```text
计算量 ≈ chunk_count * 512 次乘加
```

示例：

| 搜索范围 chunk 数 | 近似乘加次数 | 评价 |
| ---: | ---: | --- |
| 10,000 | 512 万 | 很轻 |
| 100,000 | 5120 万 | CPU 可接受，热缓存下通常不是主瓶颈 |
| 1,000,000 | 5.12 亿 | 开始明显，需要关注 LanceDB 查询、过滤和内存带宽 |
| 10,000,000 | 51.2 亿 | 需要更明确的分片、过滤、索引和容量设计 |

当前实现会先解析 indexed 文档范围，再把 `doc_ids` 作为 LanceDB 查询过滤条件。如果一次只查少量文档，即使系统总文档很多，只要过滤后的 chunk 数不大，向量召回压力仍然可控。

如果未来要频繁“跨所有文档检索”，总 chunk 数会直接影响搜索复杂度，此时需要考虑：

- LanceDB 表规模、索引类型和 compaction / cleanup 策略。
- IVF / HNSW / PQ 等近似索引或专门向量数据库。
- 按用户、租户、知识库分片。
- 热索引常驻内存。
- 高并发下的 MySQL 回表压力。

## 6. 系统负载规模评估

下面按当前实现和目标放大后的架构分别评估。

### 6.1 当前实现的实际上限

当前 ingest 建索引时已经显式读取该文档的全部 chunks，不再受到 `list_chunks_by_doc_id` 默认 `limit=200` 的限制。

在当前 LanceDB + MySQL chunk 存储设计下：

| 项 | 规模判断 |
| --- | --- |
| 单文档索引 chunk 数 | 主要受文档大小、embedding 吞吐、LanceDB 写入和磁盘限制 |
| 单文档向量大小 | 约 `chunk_count * 2 KB`，不含文本和表结构开销 |
| LanceDB 召回压力 | 中小文档较低，大文档取决于过滤后的 chunk 数和查询方式 |
| 主要瓶颈 | embedding、reranker、LLM / vLLM、任务队列、LanceDB 查询和 MySQL 回表 |

这意味着项目已经能完整索引大文档，但如果单文档 chunk 很多，后续仍应关注 LanceDB 表维护、查询过滤、MySQL 回表和 reranker 候选数。

### 6.2 推荐规模

以 `512` 维、LanceDB 本地向量表和 MySQL chunk 文本存储为示例时，可按以下规模估算；实际部署须按模型输出维度重新计算：

| 规模 | chunk 总量 | 适合程度 | 建议 |
| --- | ---: | --- | --- |
| 小规模 | 1 万以内 | 很适合 | 当前架构足够，重点优化 LLM / vLLM。 |
| 中小规模 | 1 万到 10 万 | 适合 | 保持 `candidate_top_k` 可控，重点看 rerank 和 LLM。 |
| 中规模 | 10 万到 100 万 | 可做但要优化 | 关注 LanceDB 查询、过滤、备份恢复和 MySQL 回表。 |
| 大规模 | 100 万到 1000 万 | 需要专项优化 | 推荐分片、ANN、冷热数据分层和更严格的租户/知识库过滤。 |
| 超大规模 | 1000 万以上 | 不建议只靠当前单机默认配置 | 需要专门检索架构和容量设计。 |

### 6.3 在线问答负载

一次普通 RAG 问答的耗时大致由以下部分组成：

```text
总延迟 ≈ query_embedding_ms
       + lancedb_ms
       + mysql_hydration_ms
       + rerank_ms
       + prompt_build_ms
       + llm_prefill_ms
       + llm_decode_ms
```

通常：

- `prompt_build_ms` 很小。
- `lancedb_ms` 在中小规模下通常较小。
- `mysql_hydration_ms` 取决于候选数和 chunk 行读取效率。
- `query_embedding_ms` 取决于 embedding 模型和部署方式。
- `rerank_ms` 取决于 candidate 数量和 reranker 模型。
- `llm_prefill_ms` 与 prompt 长度强相关。
- `llm_decode_ms` 与输出长度强相关。

如果 `candidate_top_k=30`、`final_top_k=5`，负载特征是：

| 阶段 | 输入规模 | 压力 |
| --- | --- | --- |
| query embedding | 1 个问题 | 低到中 |
| LanceDB | N 个候选 chunk 向量 | 中小规模下低 |
| MySQL 回表 | 约 candidate_top_k 个 chunk_id | 低到中 |
| rerank | 30 个 question/chunk pair | 中到高 |
| LLM / vLLM | 约 5 个 chunk 的上下文 + 问题 | 高 |

因此，实际并发能力一般由 LLM / vLLM 和 reranker 决定，而不是 512 维向量本身决定。

在单机部署、单个本地 vLLM 实例、普通 7B 到 14B 级模型的前提下，可以粗略按以下方式规划：

| 场景 | 粗略能力判断 |
| --- | --- |
| 演示 / 个人使用 | 1 到 5 个并发会话通常可承受。 |
| 小团队内部使用 | 5 到 20 个活跃并发需要关注 vLLM 显存、TTFT 和队列。远端 API 还要关注 provider 限流。 |
| 更高并发 | 需要多 LLM / vLLM 实例、请求排队、限流、缓存、缩短上下文和输出长度。 |

这个并发估算不能替代压测，因为模型大小、GPU、量化方式、上下文长度和输出长度都会显著改变结果。项目已有 [performance_test_guide.md](./performance_test_guide.md)，建议用固定文档、固定问题集和固定 `top_k` 做压测留档。

### 6.4 Ingest 负载

Ingest 的主要压力来自文档 embedding，而不是 LanceDB 写入。

单个文档的 ingest 时间大致为：

```text
ingest_time ≈ text_extract
            + chunk_count / embedding_throughput
            + lancedb_write
            + mysql_write
```

其中 embedding throughput 取决于：

- embedding 模型大小。
- CPU 还是 GPU。
- batch size。
- 是否远程 OpenAI-compatible embedding 服务。
- 文本长度和 tokenizer 成本。

512 维只影响输出向量大小，不直接代表 embedding 模型很轻。真正的计算量由 embedding 模型本身决定。

建议：

- ingest worker 并发不要盲目拉高，先从 `1` 到 `2` 开始压测。
- embedding 服务独立部署时，要限制 Celery 并发，避免同时提交太多大文档。
- 大文档 ingest 建议分批 embedding，并记录每批耗时。
- 切换 embedding 模型或 prefix 后，必须重建所有索引。

## 7. 重排序和向量化的工程建议

### 7.1 向量化建议

1. 固定 embedding 模型版本，并写入索引元数据。
2. 记录模型实际输出维度，查询前校验 query vector 维度和索引元数据维度一致。
3. 开启 normalize，让向量相似度更接近 cosine similarity 语义。
4. 文档和问题使用一致的 query/document prefix 策略。
5. 对 embedding 质量做离线评估：Recall@K、MRR、NDCG、Top-1 命中率。
6. 建立真实业务 QA 验证集，不只看向量相似度。

### 7.2 Rerank 建议

1. 将 `candidate_top_k` 和 `final_top_k` 拆开。
2. 建议初始配置：`candidate_top_k=30`，`final_top_k=5`。
3. reranker 使用 batch 推理，避免 30 个候选逐条串行请求。
4. reranker 输出分数和 LanceDB 召回分数都写入日志或 metrics，方便分析误召回。
5. 对低 rerank 分数结果设置阈值，必要时进入 low-confidence prompt。
6. 对相同内容、相邻 chunk、重复 chunk 做去重或合并。
7. 保留 citations，最终回答必须能追溯到具体 chunk。

### 7.3 Prompt 拼接建议

1. final chunk 数控制在 3 到 5。
2. 单 chunk 最大字符数控制在 800 到 1200。
3. chunk 中保留必要元数据，但不要把过多 JSON 结构塞进 prompt。
4. 如果答案经常跨 chunk，可以在 rerank 后补充相邻 chunk，而不是单纯提高 final_top_k。
5. 对长问题和长上下文分别记录 tokens，观察 TTFT 是否恶化。

## 8. 需要优先修正或增强的点

| 优先级 | 建议 | 原因 |
| --- | --- | --- |
| 已完成 | 修正 ingest 建索引只取 200 chunks 的限制 | 大文档可以完整进入向量索引。 |
| 已完成 | 增加 cross-encoder reranker | 支持基于 question/chunk pair 的语义重排序。 |
| 已完成 | 拆分 `candidate_top_k` 和 `final_top_k` | 粗召回和最终上下文使用不同数量。 |
| P1 | 持续验证 LanceDB 查询和 MySQL 回表耗时 | 大文档和跨文档检索时更容易暴露瓶颈。 |
| 已完成 | 记录 rerank_ms、candidate_top_k、final_top_k | 便于定位延迟和质量问题。 |
| P2 | 定期清理 orphan vectors 并建立 LanceDB 备份恢复流程 | 防止文档删除或重建后向量表膨胀。 |
| P2 | 大规模时引入 ANN、分片或专门向量检索架构 | 单机默认配置不适合千万级在线检索。 |

## 9. 最终判断：先测计算链，再扩向量库

按 `512` 维示例计算，纯向量约 `2 KB / chunk`；实际值取决于模型输出维度。如果按默认 `800` 字符切片、`100` overlap，一个百万字符文档大约会产生 `1429` 个 chunk，纯向量只有约 `2.8 MB`。因此，中小规模 RAG 系统的主要瓶颈不是向量存储，而是 embedding 模型吞吐、reranker 延迟、LLM / vLLM 上下文长度和生成吞吐。

当前 LanceDB 默认架构适合中小规模本地检索；到 10 万到 100 万 chunk 时，需要用真实 benchmark 验证 LanceDB 查询、MySQL 回表、reranker 候选数和备份恢复流程；超过百万级后，应认真考虑分片、ANN、冷热数据分层或专门向量检索架构。

质量上，最关键的是两点：第一，embedding 必须稳定、一致、可评估；第二，rerank 必须是真正基于 question/chunk pair 的语义重排序，而不是简单按向量召回分数或原顺序截断。一个更合理的生产配置是：LanceDB 先召回 `20` 到 `50` 个候选，reranker 重排后取 `3` 到 `5` 个 chunk 拼接给 LLM。
