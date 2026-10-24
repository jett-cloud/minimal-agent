# Minimal Agent 完整设计方案

- 文档版本：1.1
- 状态：步骤 11 Review 修订基线，后续调整必须经用户确认
- Python：3.12
- 架构原则：不使用 Agent 框架；领域逻辑与 OpenAI SDK、HTTP、SQLite 解耦

## 1. 目标与边界

### 1.1 目标

构建一个最小可用的单 Agent 服务，支持：

1. 独立 Session 和持久化聊天历史。
2. calculator、本地主板 search、Open-Meteo weather 三个工具。
3. 最多 8 步的 ReAct 工具循环。
4. 历史消息摘要和最近 5 轮完整对话保留。
5. Responses API 或 Chat Completions 兼容接口的流式正文、模型实际返回的可见推理和工具调用事件。
6. SSE 事件持久化、心跳和断线重连。
7. Run、Trace 和全部标准化流事件可审计。
8. Fake LLM 驱动的可重复单元测试和集成测试。

### 1.2 非目标

MVP 不包含用户认证、前端页面、多 Agent、向量数据库、分布式任务队列、并行工具
调用、多进程 worker 或可恢复的外部副作用工具。三个初始工具均为只读或纯计算。

### 1.3 部署约束

MVP 使用单个应用进程和单个 Uvicorn worker。SQLite 开启 WAL、外键和 busy timeout。
同一 Session 同时只允许一个 running Run，不同 Session 可以并发。

## 2. 总体架构

```text
HTTP API
  ├─ Session/Message API
  └─ Run SSE API
          │
          ▼
RunSupervisor ──启动并持有──> AgentLoop
                              ├─ ContextBuilder / Compressor
                              ├─ LLMClient Protocol
                              │    ├─ OpenAI Responses Adapter
                              │    ├─ OpenAI-compatible Chat Adapter
                              │    ├─ Reasoning Router / think 标签解析器
                              │    └─ Fake LLM
                              └─ ToolRegistry
                                   ├─ calculator
                                   ├─ search
                                   └─ weather
          │                         │
          └──────────┬──────────────┘
                     ▼
            SQLite Repositories
            sessions / messages / runs
            traces / stream_events
```

接口层只负责校验和状态码；AgentLoop 只依赖协议；存储层不依赖 FastAPI；OpenAI
事件在适配器边界转换成内部 Pydantic 事件。

## 3. 最终目录结构

```text
minimal-agent/
├─ pyproject.toml
├─ MANIFEST.in
├─ README.md
├─ .env.example
├─ .gitignore
├─ data/mainboards.json
├─ docs/
│  ├─ design.md
│  ├─ operations.md
│  └─ progress.md
├─ prompts/
│  ├─ system.md
│  └─ summarize.md
├─ src/minimal_agent/
│  ├─ __init__.py
│  ├─ main.py
│  ├─ config.py
│  ├─ dependencies.py
│  ├─ services.py
│  ├─ api/
│  │  ├─ errors.py
│  │  ├─ sessions.py
│  │  ├─ runs.py
│  │  └─ events.py
│  ├─ agent/
│  │  ├─ loop.py
│  │  ├─ context.py
│  │  ├─ compression.py
│  │  ├─ decisions.py
│  │  ├─ accumulators.py
│  │  ├─ supervisor.py
│  │  └─ errors.py
│  ├─ llm/
│  │  ├─ base.py
│  │  ├─ events.py
│  │  ├─ openai_client.py
│  │  ├─ openai_chat_client.py
│  │  ├─ reasoning.py
│  │  ├─ fake_client.py
│  │  └─ retry.py
│  ├─ tools/
│  │  ├─ base.py
│  │  ├─ registry.py
│  │  ├─ calculator.py
│  │  ├─ search.py
│  │  └─ weather.py
│  ├─ streaming/
│  │  ├─ events.py
│  │  ├─ buffer.py
│  │  └─ publisher.py
│  ├─ storage/
│  │  ├─ database.py
│  │  ├─ schema.sql
│  │  └─ repositories.py
│  └─ schemas/
│     ├─ api.py
│     ├─ session.py
│     ├─ message.py
│     ├─ run.py
│     └─ trace.py
└─ tests/
   ├─ conftest.py
   ├─ fixtures/
   ├─ contract/
   ├─ unit/
   │  ├─ llm/
   │  └─ tools/
   ├─ integration/
   │  └─ tools/
   └─ live/
```

目录按实施步骤逐步创建，不预先放置无意义的空文件。

## 4. 数据设计

### 4.1 sessions

- `id TEXT PRIMARY KEY`
- `title TEXT NULL`
- `summary TEXT NULL`
- `summary_from_seq INTEGER NULL`
- `summary_through_seq INTEGER NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

摘要覆盖区间用于防止重复摘要。时间统一存储为 UTC ISO 8601。

### 4.2 messages

- `id TEXT PRIMARY KEY`
- `session_id TEXT NOT NULL`
- `run_id TEXT NULL`
- `seq INTEGER NOT NULL`
- `role TEXT NOT NULL`：user、assistant、tool
- `kind TEXT NOT NULL`：message、tool_call、tool_result、system_error
- `content_json TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- 唯一约束：`(session_id, seq)`

原始消息不因压缩而删除；ContextBuilder 依据摘要覆盖序号决定是否加载。

### 4.3 runs

- `id TEXT PRIMARY KEY`
- `session_id TEXT NOT NULL`
- `status TEXT NOT NULL`：running、completed、failed、cancelled
- `loop_count INTEGER NOT NULL`
- `user_message_id TEXT NOT NULL`
- `final_message_id TEXT NULL`
- `error_code TEXT NULL`
- `error_message TEXT NULL`
- `started_at TEXT NOT NULL`
- `finished_at TEXT NULL`

使用部分唯一索引保证每个 Session 最多一个 running Run。

### 4.4 traces

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `run_id TEXT NOT NULL`
- `step_no INTEGER NOT NULL`
- `event_type TEXT NOT NULL`
- `payload_json TEXT NOT NULL`
- `duration_ms INTEGER NULL`
- `created_at TEXT NOT NULL`

Trace 不保存 API Key，不保存隐藏思维链，并限制错误正文和外部响应大小。

### 4.5 stream_events

- `id INTEGER PRIMARY KEY AUTOINCREMENT`：作为 SSE `id`
- `run_id TEXT NOT NULL`
- `seq INTEGER NOT NULL`：Run 内顺序
- `event_type TEXT NOT NULL`
- `payload_json TEXT NOT NULL`
- `source_seq INTEGER NULL`：上游事件序号，仅用于诊断
- `created_at TEXT NOT NULL`
- 唯一约束：`(run_id, seq)`
- 索引：`(run_id, id)`

`messages` 保存完整语义消息，用于后续 Context；`stream_events` 保存可重放的标准化
增量；`traces` 保存内部执行诊断，三者职责不混用。

## 5. 工具系统

`ToolDefinition` 包含名称、描述、Pydantic 参数模型、处理函数和超时。注册中心负责
名称唯一性、JSON Schema 导出、参数验证、执行超时和错误归一化。

统一结果：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "INVALID_ARGUMENTS",
    "message": "...",
    "retryable": false,
    "details": {}
  }
}
```

### 5.1 calculator

使用 Python AST 白名单解析算术表达式，不使用 `eval`。限制输入长度、AST 节点数、
指数和结果大小，支持基础运算和明确允许的数学函数。

### 5.2 search

只读取本地 `data/mainboards.json`。对品牌、型号、芯片组、插槽、内存类型等字段进行
大小写归一化、分词包含匹配和简单加权排序，并限制返回条数。

### 5.3 weather

通过 Open-Meteo Geocoding 把地点转换为经纬度，再调用 Forecast API。使用
`httpx.AsyncClient`，设置连接/读取超时，验证第三方响应并只返回 Agent 所需字段。

### 5.4 @工具名

应用层只识别注册表中存在的精确 `@tool_name`。命中后向模型强调用户指定工具，并可
将可用工具限制为该工具；缺少关键参数时要求用户补充，不让模型猜测。未知名称作为
普通文本处理并在必要时说明工具不存在。

## 6. LLM 适配与 ReAct 循环

优先使用 OpenAI Responses API 原生 Function Calling，也支持 OpenAI-compatible Chat
Completions 工具调用。工具调用始终使用 API 结构化字段，不解析模型手写的工具调用 JSON。
`LLMClient` 对 Agent 暴露异步事件迭代器；Fake LLM 使用相同协议。

OpenAI 事件先映射为内部 Pydantic 类型，AgentLoop 不直接依赖 SDK 类型。设置
`parallel_tool_calls=False`，MVP 每一步最多执行一个工具。

每个 Run：

1. 保存用户消息并创建 Run。
2. 装配当前 Session Context，必要时压缩。
3. 调用 LLM 并消费流事件。
4. 若产生完整工具调用，验证参数、执行工具、持久化结果并进入下一步。
5. 若产生最终正文，持久化 assistant 消息并完成 Run。
6. 每一步前检查 8 步限制、总运行时限和 Context 限制。

LLM 对超时、429、连接故障和 5xx 最多重试 3 次，使用带抖动的短指数退避。每次
失败均写 Trace；工具错误作为结构化结果返回 Agent，不在工具层无限重试。

## 7. Context 与压缩

Context 顺序：系统提示词、工具定义、Session 摘要、摘要覆盖范围之后的历史消息、
当前用户输入。SQLite 是会话真源，不依赖远端 `previous_response_id` 保存状态。

每次 LLM 调用前由 `TokenCounter` 估算输入。模型窗口大小通过配置明确提供；达到
80% 时只压缩已经完成的旧轮次，保留最近 5 个完整轮次和当前未完成 Run。

摘要必须保留用户偏好、事实、未完成任务、关键决定和关键工具结果。摘要完成后在同一
事务中更新正文和 `summary_through_seq`。若没有可压缩轮次但仍超限，Run 以
`CONTEXT_LIMIT_EXCEEDED` 失败。

## 8. SSE 流式设计

### 8.1 对外事件

- `run.started`、`turn.started`、`turn.completed`
- `reasoning.started`、`reasoning.delta`、`reasoning.done`
- `assistant.delta`、`assistant.done`
- `tool_call.delta`、`tool_call.ready`
- `tool.started`、`tool.completed`、`tool.failed`
- `run.completed`、`run.failed`

将供应商实际返回的 `reasoning_text`、`reasoning_summary_text`、`reasoning_content` 和
正文开头的 `<think>...</think>` 统一映射为 `reasoning.*`。适配器不生成不存在的推理；
模型只提供生命周期而不提供文本时，仅发送阶段开始/结束。`auto` 捕获模式由第一个产生
有效文本的结构化或标签来源获得输出权，避免同一内容重复发送；`both` 模式可用于诊断。

`<think>` 使用跨 chunk 状态机解析，只在正文首个非空白位置识别。推理文本不进入最终
assistant 消息。未闭合标签标记为 `completed=false`；若该步也没有合法正文或工具调用，
则以 `LLM_RESPONSE_INCOMPLETE` 终止。

### 8.2 工具参数组装

按 `item_id/call_id` 维护独立累积器。delta 只追加和展示，不执行。收到参数完成事件
后使用完整参数解析 JSON，再用工具 Pydantic 模型验证。流提前结束、JSON 非法或参数
非法时不执行工具，产生结构化错误并记录 Trace。

### 8.3 缓冲和持久化

Agent 将标准化事件放入有界 `asyncio.Queue`，单写入器保持顺序。累计 8 KiB、经过
100 ms、遇到屏障事件或回合结束时批量提交。只合并相邻且同类型、同 item 的 delta。
屏障事件包括工具 ready/完成/失败、assistant.done 和 Run 终态。

批量提交后才通知 SSE 消费端。强制杀进程最多可能丢失尚未刷新的约 100 ms 增量；
零丢失需要逐事件写入或外部持久队列，超出 MVP 范围。

### 8.4 断线恢复

消息提交接口返回 `202` 和 `run_id`；SSE 使用独立 GET 端点。客户端使用标准
`Last-Event-ID`，也支持 `after_id` 查询参数。服务端从事件表按 `id` 顺序补发，15 秒
无数据发送不落库的注释心跳。客户端断开不取消 Agent Run。

进程启动时将遗留 running Run 标记为 `PROCESS_RESTARTED`。已提交事件仍可重放，
但中断的模型流不尝试续传。

## 9. HTTP API

- `GET /health`
- `POST /v1/sessions`
- `GET /v1/sessions`
- `GET /v1/sessions/{session_id}`
- `GET /v1/sessions/{session_id}/messages`
- `POST /v1/sessions/{session_id}/messages`：返回 202、run_id 和 events_url
- `GET /v1/runs/{run_id}`
- `GET /v1/runs/{run_id}/trace`
- `GET /v1/runs/{run_id}/events`：SSE

同一 Session 已有 running Run 时返回 `409 SESSION_BUSY`。请求不存在返回 404；参数
问题返回 422；上游失败通过 Run 终态事件表达。所有错误使用稳定的 Pydantic envelope。

SSE 返回 `text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`。

## 10. 一致性、并发和恢复

网络调用期间不持有数据库事务。事件批次、工具完整结果、消息和状态更新采用短事务。
Run 完成时在同一事务写完整 assistant 消息、更新 Run 终态并插入终止事件，保证前端
不会先看到完成事件再发现消息缺失。

RunSupervisor 持有后台 Task 强引用并清理完成 Task。应用关闭时停止接收新 Run，等待
有限时间、刷新缓冲并标记未完成 Run。SSE 慢客户端只影响自身读取，不阻塞 LLM。

## 11. 配置和安全

配置来自环境变量和 `.env`，真实 `.env` 不提交。API Key 使用 `SecretStr`，不得进入
日志、Trace、SSE 或异常响应。真实 LLM 客户端构造时再校验 Key 和模型，健康检查无需
外部凭证。

所有输入、工具参数、第三方响应和事件 payload 均由 Pydantic 验证。限制用户消息、
表达式、搜索结果、外部响应、Trace 和 SSE payload 大小。日志使用 run_id、session_id
和 call_id 关联，不记录敏感头。

## 12. 测试策略

默认测试禁止真实网络。Fake LLM 提供脚本化事件；`httpx.MockTransport` 模拟天气服务；
每个测试使用临时 SQLite。

单元测试覆盖配置、Repository、工具安全、工具 Schema、事件映射、参数跨 chunk 拼接、
缓冲阈值、Context 装配、压缩边界、重试和错误映射。

集成测试覆盖普通回答、连续工具调用、非法工具、Session 隔离、摘要不重复、最大步骤、
总超时、LLM/工具失败、Trace、SSE 顺序、断线重连、心跳、终态刷新及客户端断开后
Run 继续执行。

每一步最低质量门禁：`pytest`、`ruff check`、`ruff format --check` 全部通过。

## 13. 冻结实施计划

除非 Review 明确批准，不改变步骤顺序、范围或验收条件。

### 步骤 1：项目基线

创建打包配置、环境变量样例、FastAPI 工厂、健康检查、最小测试、README、设计文档和
进度文档。验收：包可安装；健康测试通过；Ruff 检查和格式检查通过。

### 步骤 2：SQLite 持久化

实现连接管理、schema 和 Session/Message/Run/Trace/StreamEvent Repository。验收：
外键、WAL、序号、短事务、单 Session 活跃 Run 约束和 Session 隔离测试通过。

### 步骤 3：工具协议与 calculator

实现 ToolDefinition、注册中心、统一结果/错误和安全 AST calculator。验收：Schema、
参数验证、超时、危险表达式拒绝和计算正确性测试通过。

### 步骤 4：本地主板 search

添加可维护的 JSON 数据集和确定性匹配/排序。验收：型号、品牌和规格查询、limit、空
结果、坏数据测试通过。

### 步骤 5：Open-Meteo weather

实现地理编码和天气查询，验证外部响应。验收：正常响应、无地点、429、超时、畸形
响应均由 MockTransport 测试覆盖。

### 步骤 6：LLM 协议与流式适配器

实现内部事件模型、Fake LLM、OpenAI Responses 流适配、事件白名单和有限重试。验收：
正文、raw/summary/tagged reasoning、工具参数、完成、拒绝、中断和重试测试通过。

### 步骤 7：Session Context 与压缩

实现 system prompt、@工具解析、ContextBuilder、TokenCounter 和摘要覆盖逻辑。验收：
Session 隔离、最近 5 轮、只压缩已完成轮次、重复摘要保护和无法压缩终止测试通过。

### 步骤 8：持久化事件缓冲与 SSE

实现事件缓冲、批量写入、重放端点、Last-Event-ID、心跳和终态关闭。验收：8 KiB、
100 ms、顺序、断线补发、不重复、客户端断开不影响生产者测试通过。

### 步骤 9：AgentLoop 与 RunSupervisor

连接 LLM、工具、Context、存储和事件流，实现 8 步/总时限/Context 限制。验收：普通
回答、工具循环、参数错误回传、终态原子性、启动恢复和完整 Trace 测试通过。

### 步骤 10：完整 HTTP API

实现 Session、Message、Run、Trace 路由、依赖注入和统一错误响应。验收：API 契约、
404/409/422、并发 Session、生命周期和 OpenAPI 测试通过。

### 步骤 11：全链路验收与运行文档

补齐端到端 Fake LLM 测试、可选真实 API smoke test、故障场景、部署说明和维护说明。
验收：全部质量门禁通过，README 可让新环境从零启动，进度文档无未解释偏差。

步骤 11 Review 修订增加：Responses 与 Chat Completions 两种适配器、结构化推理与
`<think>` 双路捕获、供应商无关 live test、逐工具测试契约，以及写工具副作用/重试语义
约定。该修订不改变 SQLite 表结构，SSE 只增加向后兼容的事件和 payload 字段。

## 14. 计划变更控制

发现更优方案时先在 `docs/progress.md` 的“优化建议/待确认”中记录：建议、原因、影响、
涉及步骤和迁移成本。在用户确认前不修改冻结计划或扩大当前步骤范围。Review 反馈导致
的修复归属于当前步骤，不开始下一步；修复、验证结果和 Review 结论持续写入进度文档。

## 15. 版本 1.1 调整记录

- 原因：步骤 11 Review 要求展示模型实际返回的推理、支持不止 DeepSeek 的模型，并将
  真实测试和工具测试改为可扩展结构。
- 影响：修改 LLM 内部事件、SSE reasoning payload、模型配置、测试目录和运行文档；
  HTTP 路由、Session/Run 数据表与现有事件重放协议不变。
- 兼容：通用 `LLM_*` 为推荐配置，旧 `OPENAI_*`/`DEEPSEEK_*` 环境变量别名继续接受。
- 成本：真实 `full` profile 设置全局调用预算，默认测试仍不访问付费 API。
- 确认：2026-10-24 用户确认本计划并要求开始执行。

​	
